"""RUAccent configuration and explicit stress corrections for Kokoro-ru v2.

This module is shared by the production Kokoro TTS engine and its focused
regression checks. It validates an existing local RUAccent asset directory;
it does not download missing RUAccent assets itself.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


SUPPORTED_SCHEMA_VERSION = 1


def load_stress_config(path: Path | str) -> dict[str, Any]:
    """Load and validate the user-editable Kokoro/RUAccent stress config."""
    config_path = Path(path)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Не найден файл настроек ударений Kokoro: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Некорректный JSON в файле ударений Kokoro {config_path}: {exc}") from exc

    if not isinstance(config, dict):
        raise ValueError("Корень файла ударений Kokoro должен быть JSON-объектом")
    if config.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"Неподдерживаемая schema_version в файле ударений: "
            f"{config.get('schema_version')!r}; ожидалась {SUPPORTED_SCHEMA_VERSION}"
        )

    ruaccent = config.get("ruaccent")
    if not isinstance(ruaccent, dict):
        raise ValueError("В файле ударений должен быть объект 'ruaccent'")

    for key in ("custom_dict", "custom_homographs"):
        value = ruaccent.get(key, {})
        if not isinstance(value, dict):
            raise ValueError(f"ruaccent.{key} должен быть JSON-объектом")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()) and key == "custom_dict":
            raise ValueError("Все ключи и значения ruaccent.custom_dict должны быть строками")
        if key == "custom_homographs":
            for word, variants in value.items():
                if not isinstance(word, str) or not isinstance(variants, list) or not all(isinstance(x, str) for x in variants):
                    raise ValueError("ruaccent.custom_homographs должен содержать списки строк")

    rules = config.get("context_rules", [])
    if not isinstance(rules, list):
        raise ValueError("context_rules должен быть JSON-массивом")

    seen_ids: set[str] = set()
    for index, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict):
            raise ValueError(f"Правило context_rules #{index} должно быть JSON-объектом")
        rule_id = rule.get("id")
        pattern = rule.get("pattern")
        replacement = rule.get("replacement")
        flags = rule.get("flags", [])
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise ValueError(f"У правила context_rules #{index} отсутствует строковый id")
        if rule_id in seen_ids:
            raise ValueError(f"Повторяется id правила ударений: {rule_id}")
        seen_ids.add(rule_id)
        if not isinstance(pattern, str) or not isinstance(replacement, str):
            raise ValueError(f"У правила {rule_id} должны быть строковые pattern и replacement")
        if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
            raise ValueError(f"flags правила {rule_id} должен быть массивом строк")
        re_flags = 0
        for flag in flags:
            if flag not in {"IGNORECASE", "MULTILINE", "DOTALL"}:
                raise ValueError(f"Неподдерживаемый флаг {flag!r} у правила {rule_id}")
            re_flags |= getattr(re, flag)
        try:
            re.compile(pattern, re_flags)
        except re.error as exc:
            raise ValueError(f"Некорректное регулярное выражение правила {rule_id}: {exc}") from exc

    # Store defaults explicitly so callers do not need defensive lookups.
    ruaccent.setdefault("custom_dict", {})
    ruaccent.setdefault("custom_homographs", {})
    config["context_rules"] = rules
    return config


def apply_contextual_stress(text: str, rules: list[dict[str, Any]]) -> str:
    """Apply explicit phrase-level corrections to RUAccent '+' stress output."""
    result = text
    for rule in rules:
        re_flags = 0
        for flag in rule.get("flags", []):
            re_flags |= getattr(re, flag)
        pattern = re.compile(rule["pattern"], re_flags)
        result = pattern.sub(rule["replacement"], result)
    return result


def stress_config_sha256(path: Path | str) -> str:
    """Return the SHA-256 of the exact UTF-8 config file used by a benchmark run."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_configured_g2p(
    *,
    model_root: Path | str,
    accent_model_root: Path | str,
    stress_config: dict[str, Any],
) -> Any:
    """Create upstream RuG2P with pinned local RUAccent data and custom rules.

    The pinned upstream RuG2P constructor creates RUAccent() internally and
    calls load() without exposing RUAccent's workdir/custom_dict parameters.
    Required local RUAccent model/dictionary directories must already exist;
    this helper validates them before construction and intentionally does not
    bootstrap or download assets. We temporarily wrap RUAccent.load only during
    RuG2P construction, then restore the class method immediately. The created
    instance retains its loaded local assets and dictionary. Context rules are
    applied to the RUAccent marked text before RuG2P converts stress marks to IPA.
    """
    model_root = Path(model_root).resolve()
    accent_model_root = Path(accent_model_root).resolve()

    if not (model_root / "ru_g2p.py").is_file():
        raise FileNotFoundError(f"Не найден Kokoro G2P frontend: {model_root / 'ru_g2p.py'}")

    required_accent_dirs = (
        "dictionary",
        "nn/nn_accent",
        "nn/nn_stress_usage_predictor",
        "nn/nn_yo_homograph_resolver",
    )
    missing = [str(accent_model_root / item) for item in required_accent_dirs if not (accent_model_root / item).is_dir()]
    if missing:
        raise FileNotFoundError(
            "Не найдены локальные каталоги RUAccent. Проверьте data/models/ruaccent-accentuator: "
            + ", ".join(missing)
        )

    # Imported only after the benchmark adds model_root to sys.path.
    import ruaccent
    from ruaccent import RUAccent
    from ru_g2p import RuG2P  # type: ignore[import-not-found]

    original_load = RUAccent.load
    ruaccent_config = stress_config["ruaccent"]

    def configured_load(self: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs["workdir"] = str(accent_model_root)
        kwargs["custom_dict"] = dict(ruaccent_config["custom_dict"])
        kwargs["custom_homographs"] = dict(ruaccent_config["custom_homographs"])
        return original_load(self, *args, **kwargs)

    RUAccent.load = configured_load
    try:
        g2p = RuG2P(
            espeak_data=model_root / "espeak-data",
            vocab_path=model_root / "kokoro-config.json",
        )
    finally:
        RUAccent.load = original_load

    original_process_all = g2p._accent.process_all
    context_rules = stress_config["context_rules"]

    def process_all_with_context(text: str, skip_regex: str | None = None) -> str:
        marked = original_process_all(text, skip_regex=skip_regex)
        return apply_contextual_stress(marked, context_rules)

    # RuG2P.accentuate() calls this method before converting '+' to U+0301.
    # Only this RUAccent instance is wrapped; the RUAccent class is restored.
    g2p._accent.process_all = process_all_with_context
    return g2p
