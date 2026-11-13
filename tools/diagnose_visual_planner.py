#!/usr/bin/env python3
"""Diagnose why an OpenAI-compatible provider struggles with visual planning.

The script is intentionally read-only with respect to project data: it reads the
existing .env, script/storyboard JSON and writes a diagnostic report under
output/diagnostics. It never modifies the source storyboard or production files.

It runs the same visual-planning contract against selected providers and records
transport, strict-JSON-Schema support, latency, token usage, raw output and local
Pydantic validation. It does not perform the production repair retry, so the
first model response can be inspected independently.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from dataclasses import replace
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kadr_farm.common import parse_json_response, read_json, write_json  # noqa: E402
from kadr_farm.config import load_settings  # noqa: E402
from kadr_farm.llm_structured import (  # noqa: E402
    _is_format_unsupported,
    _schema_for_provider,
)
from kadr_farm.models import VisualPlanBatch  # noqa: E402
from kadr_farm.visual_planner import SYSTEM_PROMPT as VISUAL_SYSTEM_PROMPT  # noqa: E402

log = logging.getLogger("mws_visual_diagnostic")

PING_SYSTEM = "Ты выполняешь короткую техническую проверку связи."
PING_USER = "Ответь ровно одним словом: OK"
TINY_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok"]},
        "echo": {"type": "string"},
    },
    "required": ["status", "echo"],
    "additionalProperties": False,
}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_error(exc: BaseException, api_key: str) -> str:
    message = f"{type(exc).__name__}: {exc}"
    if api_key:
        message = message.replace(api_key, "[REDACTED_API_KEY]")
    message = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,]+", r"\1[REDACTED]", message)
    return message[:2500]


def classify_error(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if "timeout" in name or "timed out" in message or "timeout" in message:
        return "timeout"
    if _is_format_unsupported(exc):
        return "format_unsupported"
    return "api_error"


def provider_host(base_url: str) -> str:
    try:
        return urlsplit(base_url).hostname or "unknown"
    except Exception:
        return "unknown"


def load_storyboard_scenes(path: Path, scene_ids: list[str]) -> list[dict[str, Any]]:
    storyboard = read_json(path)
    available = {str(s.get("id")): s for s in storyboard.get("scenes", [])}
    missing = [scene_id for scene_id in scene_ids if scene_id not in available]
    if missing:
        raise ValueError(f"В {path} отсутствуют сцены: {', '.join(missing)}")
    result: list[dict[str, Any]] = []
    for scene_id in scene_ids:
        scene = available[scene_id]
        required = ("id", "block_id", "voice_text", "meaning", "purpose")
        absent = [field for field in required if field not in scene]
        if absent:
            raise ValueError(f"Сцена {scene_id}: отсутствуют поля {absent}")
        # Send only the input contract of the planner; never feed already generated
        # visual decisions back into the diagnostic prompt.
        result.append({field: scene[field] for field in required})
    return result


def build_visual_prompt(scenes: list[dict[str, Any]]) -> str:
    scene_payload = [{
        "scene_id": scene["id"],
        "block_id": scene["block_id"],
        "voice_text": scene["voice_text"],
        "meaning": scene["meaning"],
        "purpose": scene["purpose"],
    } for scene in scenes]
    return (
        "Создай VisualPlanBatch для следующих сцен. Не меняй narration.\n"
        "Учитывай типы визуалов уже назначенных предыдущим сценам, "
        "избегай одинаковых визуальных решений подряд, если это не оправдано смыслом.\n"
        "ПРЕДЫДУЩИЙ ПЛАН: []\n"
        f"СЦЕНЫ: {json.dumps(scene_payload, ensure_ascii=False)}"
    )


def response_metadata(response: Any) -> dict[str, Any]:
    choice = response.choices[0] if response.choices else None
    usage = getattr(response, "usage", None)
    if usage is not None and hasattr(usage, "model_dump"):
        usage_data = usage.model_dump()
    elif usage is not None:
        usage_data = {
            key: getattr(usage, key, None)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
    else:
        usage_data = None
    return {
        "response_model": getattr(response, "model", None),
        "finish_reason": getattr(choice, "finish_reason", None) if choice else None,
        "usage": usage_data,
    }


def ollama_request_options(provider: Any, num_ctx: int) -> dict[str, Any]:
    """Apply a bounded Ollama context to avoid the model's advertised max context."""
    host = provider_host(getattr(provider, "base_url", "")).lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return {"extra_body": {"options": {"num_ctx": int(num_ctx)}}}
    return {}


def clone_ollama_provider(secondary: Any, model_tag: str) -> tuple[str, Any]:
    """Reuse the configured local endpoint and credentials with another installed tag."""
    safe_slug = re.sub(r"[^a-zA-Z0-9]+", "_", model_tag).strip("_").lower()
    report_key = f"ollama_{safe_slug}"
    return report_key, replace(secondary, name=report_key, model=model_tag)


def create_client(provider: Any, timeout: float):
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("Не установлен пакет openai. Выполни pip install -e .") from exc
    return OpenAI(
        base_url=provider.base_url,
        api_key=provider.api_key,
        timeout=timeout,
        max_retries=0,
    )


def run_plain_probe(provider: Any, timeout: float, ollama_num_ctx: int = 8192) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = create_client(provider, timeout).chat.completions.create(
            model=provider.model,
            messages=[
                {"role": "system", "content": PING_SYSTEM},
                {"role": "user", "content": PING_USER},
            ],
            temperature=0,
            max_tokens=16,
            **ollama_request_options(provider, ollama_num_ctx),
        )
        content = response.choices[0].message.content or ""
        return {
            "status": "ok" if content.strip() else "empty_response",
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "response_text": content[:500],
            **response_metadata(response),
        }
    except Exception as exc:
        return {
            "status": classify_error(exc),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "error": safe_error(exc, provider.api_key),
        }


def run_tiny_schema_probe(provider: Any, timeout: float, ollama_num_ctx: int = 8192) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = create_client(provider, timeout).chat.completions.create(
            model=provider.model,
            messages=[
                {"role": "system", "content": "Верни JSON строго по заданной схеме."},
                {"role": "user", "content": 'Верни status="ok" и echo="schema works".'},
            ],
            temperature=0,
            max_tokens=80,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "diagnostic_probe",
                    "strict": True,
                    "schema": TINY_SCHEMA,
                },
            },
            **ollama_request_options(provider, ollama_num_ctx),
        )
        raw = response.choices[0].message.content or ""
        try:
            parsed = parse_json_response(raw)
        except Exception as exc:
            return {
                "status": "invalid_json",
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "raw_response": raw[:2000],
                "error": safe_error(exc, provider.api_key),
                **response_metadata(response),
            }
        valid = (
            isinstance(parsed, dict)
            and parsed.get("status") == "ok"
            and parsed.get("echo") == "schema works"
            and set(parsed) == {"status", "echo"}
        )
        return {
            "status": "ok" if valid else "invalid_schema_response",
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "raw_response": raw[:2000],
            "parsed_response": parsed,
            **response_metadata(response),
        }
    except Exception as exc:
        return {
            "status": classify_error(exc),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "error": safe_error(exc, provider.api_key),
        }


def build_visual_request(
    provider: Any,
    prompt: str,
    response_mode: str,
    max_tokens: int,
    ollama_num_ctx: int = 8192,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Build an inspectable request for one explicit output-format mode."""
    messages = [
        {"role": "system", "content": VISUAL_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    kwargs: dict[str, Any] = {
        "model": provider.model,
        "messages": messages,
        "temperature": 0.35,
        "max_tokens": max_tokens,
    }
    kwargs.update(ollama_request_options(provider, ollama_num_ctx))
    if response_mode == "json_schema_strict":
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "semantic_pipeline_output",
                "strict": True,
                "schema": _schema_for_provider(VisualPlanBatch),
            },
        }
    elif response_mode == "json_object":
        kwargs["response_format"] = {"type": "json_object"}
    elif response_mode == "prompt_only":
        messages[0] = {
            "role": "system",
            "content": VISUAL_SYSTEM_PROMPT + "\nВерни только один корректный JSON-объект без Markdown и пояснений.",
        }
    else:
        raise ValueError(f"Неизвестный response_mode: {response_mode}")
    return messages, kwargs


def request_visual_plan(
    provider: Any,
    prompt: str,
    timeout: float,
    response_mode: str = "json_schema_strict",
    max_tokens: int = 5000,
    ollama_num_ctx: int = 8192,
) -> tuple[str, str, dict[str, Any]]:
    """Run one explicitly selected output mode; do not silently fall back.

    Keeping the mode explicit is important for the diagnostic matrix: otherwise
    a fallback could hide whether strict schema handling is the bottleneck.
    """
    _, kwargs = build_visual_request(
        provider, prompt, response_mode, max_tokens, ollama_num_ctx=ollama_num_ctx
    )
    client = create_client(provider, timeout)
    response = client.chat.completions.create(**kwargs)
    raw = response.choices[0].message.content or ""
    return raw, response_mode, response_metadata(response)


def run_visual_probe(
    provider: Any,
    scenes: list[dict[str, Any]],
    timeout: float,
    response_mode: str = "json_schema_strict",
    max_tokens: int = 5000,
    ollama_num_ctx: int = 8192,
) -> dict[str, Any]:
    prompt = build_visual_prompt(scenes)
    started = time.monotonic()
    result: dict[str, Any] = {
        "scene_ids": [scene["id"] for scene in scenes],
        "scene_count": len(scenes),
        "prompt_chars": len(prompt),
        "prompt_sha256": sha256_text(prompt),
        "system_prompt_chars": len(VISUAL_SYSTEM_PROMPT),
        "system_prompt_sha256": sha256_text(VISUAL_SYSTEM_PROMPT),
        "timeout_seconds": timeout,
        "requested_response_mode": response_mode,
        "response_format": {
            "json_schema_strict": "json_schema_strict",
            "json_object": "json_object",
            "prompt_only": None,
        }[response_mode],
        "max_tokens": max_tokens,
        "schema_chars": len(json.dumps(_schema_for_provider(VisualPlanBatch), ensure_ascii=False)),
    }
    try:
        raw, mode, metadata = request_visual_plan(
            provider, prompt, timeout, response_mode=response_mode, max_tokens=max_tokens,
            ollama_num_ctx=ollama_num_ctx,
        )
        result["response_mode_used"] = mode
        result["raw_response"] = raw
        result.update(metadata)
        try:
            parsed = parse_json_response(raw)
        except Exception as exc:
            result["status"] = "invalid_json"
            result["validation_error"] = safe_error(exc, provider.api_key)
            result["elapsed_seconds"] = round(time.monotonic() - started, 2)
            return result
        try:
            validated = VisualPlanBatch.model_validate(parsed)
            actual_ids = [plan.scene_id for plan in validated.plans]
            expected_ids = [scene["id"] for scene in scenes]
            if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected_ids):
                result["status"] = "wrong_scene_ids"
                result["validation_error"] = {
                    "expected_scene_ids": expected_ids,
                    "actual_scene_ids": actual_ids,
                }
            else:
                result["status"] = "pydantic_valid"
            result["validated_plan"] = validated.model_dump(mode="json")
        except Exception as exc:
            result["status"] = "pydantic_invalid"
            if hasattr(exc, "errors"):
                try:
                    result["validation_error"] = json.dumps(
                        exc.errors(include_url=False), ensure_ascii=False, indent=2,
                        default=lambda value: f"{type(value).__name__}: {value}",
                    )[:8000]
                except Exception:
                    result["validation_error"] = safe_error(exc, provider.api_key)
            else:
                result["validation_error"] = safe_error(exc, provider.api_key)
    except Exception as exc:
        result["status"] = classify_error(exc)
        result["error"] = safe_error(exc, provider.api_key)
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return result


def summarize(report: dict[str, Any]) -> str:
    lines = [
        "# Диагностика MWS / Qwen: визуальный планировщик",
        "",
        f"Дата UTC: {report['created_at_utc']}",
        f"Входная раскадровка: `{report['inputs']['storyboard']}`",
        "",
        "## Итоги",
        "",
        "| Провайдер | Проверка | Статус | Время, с |",
        "|---|---|---:|---:|",
    ]
    for name, provider in report["providers"].items():
        for test_name, result in provider["tests"].items():
            elapsed = result.get("elapsed_seconds", "—")
            status = result.get("status", "not_run")
            lines.append(f"| {name} (`{provider['model']}`) | {test_name} | {status} | {elapsed} |")
    lines += [
        "",
        "## Как читать результат",
        "",
        "- `plain_ping=ok`: адрес, ключ и базовый chat completion работают.",
        "- `tiny_json_schema=ok`: сервер принимает строгий JSON Schema на простой задаче.",
        "- `visual_single_<format>=pydantic_valid`: модель справилась с одной сценой в указанном режиме JSON.",
        "- `visual_pair_<format>=pydantic_valid`: модель справилась с парой сцен в указанном режиме JSON.",
        "- Сравнение `json_schema_strict`, `json_object` и `prompt_only` помогает отделить нагрузку strict schema от самой генерации визуального плана.",
        "- `timeout`: запрос не завершился в заданный диагностический лимит; это не означает, что модель не умеет решать задачу.",
        "- `pydantic_valid`: ответ прошёл локальную Pydantic-схему VisualPlanBatch и проверку набора scene_id.",
        "- `pydantic_invalid`: ответ пришёл, но не соответствует локальному контракту проекта.",
        "- `ollama_*` записи используют тот же локальный Ollama endpoint из SECONDARY, но отдельный model tag; .env не изменяется.",
        "- Для localhost-запросов явно задаётся ограничение контекста Ollama (`ollama_num_ctx`), чтобы не пытаться использовать рекламируемое максимальное окно модели.",
        "",
        "Подробные ошибки, raw-ответы, finish_reason, usage и хэши идентичных промптов находятся в JSON-отчёте.",
        "Тест не выполняет production repair retry: он фиксирует именно первый ответ модели.",
        "",
    ]
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Изолированно проверить MWS и/или Ollama на задаче visual_planner."
    )
    parser.add_argument(
        "--storyboard",
        default="output/semantic_runs/first-semantic-e2e/storyboard_draft.json",
        help="Путь к storyboard_draft.json; можно передать storyboard_planned.json.",
    )
    parser.add_argument(
        "--providers", default="primary,secondary",
        help="Какие провайдеры проверить: primary, secondary или primary,secondary.",
    )
    parser.add_argument("--probe-timeout", type=float, default=30.0)
    parser.add_argument("--schema-probe-timeout", type=float, default=45.0)
    parser.add_argument("--primary-visual-timeout", type=float, default=300.0)
    parser.add_argument("--secondary-visual-timeout", type=float, default=420.0)
    parser.add_argument(
        "--visual-cases", default="single,pair",
        help="Какие визуальные кейсы проверить: single, pair или single,pair.",
    )
    parser.add_argument(
        "--visual-formats", default="json_schema_strict",
        help="Режимы через запятую: json_schema_strict, json_object, prompt_only.",
    )
    parser.add_argument("--visual-max-tokens", type=int, default=5000)
    parser.add_argument(
        "--ollama-models", default="",
        help=("Дополнительные теги моделей Ollama через запятую. "
              "Используется BASE_URL и API key secondary, меняется только model tag."),
    )
    parser.add_argument(
        "--ollama-visual-timeout", type=float, default=900.0,
        help="Таймаут на один визуальный запрос для каждой дополнительной Ollama-модели.",
    )
    parser.add_argument(
        "--ollama-num-ctx", type=int, default=8192,
        help="Ограничение контекста Ollama для всех localhost-моделей в этом тесте.",
    )
    parser.add_argument(
        "--output-dir", default="output/diagnostics/visual-planner-local-models",
        help="Каталог для отчётов; исходная раскадровка не изменяется.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args()
    settings = load_settings()
    configured = {provider.name: provider for provider in settings.providers}
    requested = [item.strip().lower() for item in args.providers.split(",") if item.strip()]
    invalid = [name for name in requested if name not in {"primary", "secondary"}]
    if invalid:
        raise SystemExit(f"Неизвестные провайдеры: {invalid}")
    if not requested:
        raise SystemExit("Не выбраны провайдеры")

    selected: list[tuple[str, Any, float]] = []
    for name in requested:
        provider = configured.get(name)
        if provider is None:
            log.warning("Провайдер %s не настроен в .env — пропускаю", name)
        else:
            timeout = args.primary_visual_timeout if name == "primary" else args.secondary_visual_timeout
            selected.append((name, provider, timeout))

    # Additional Ollama tags reuse only the configured local endpoint/key.
    # No .env mutation is needed; each model is addressed explicitly per request.
    ollama_models = [item.strip() for item in args.ollama_models.split(",") if item.strip()]
    if ollama_models:
        secondary = configured.get("secondary")
        if secondary is None:
            raise SystemExit(
                "Для --ollama-models нужен настроенный LLM_SECONDARY_BASE_URL и "
                "LLM_SECONDARY_API_KEY в .env. Не публикуй ключ."
            )
        for model_tag in ollama_models:
            report_key, candidate = clone_ollama_provider(secondary, model_tag)
            selected.append((report_key, candidate, args.ollama_visual_timeout))

    if not selected:
        raise SystemExit("Ни один выбранный провайдер не настроен. Проверь .env, не публикуя ключи.")

    storyboard_path = Path(args.storyboard)
    if not storyboard_path.is_absolute():
        storyboard_path = ROOT / storyboard_path
    if not storyboard_path.exists():
        alternatives = [
            ROOT / "output/semantic_runs/first-semantic-e2e/storyboard_planned.json",
            ROOT / "output/semantic_runs/first-semantic-e2e/storyboard_draft.json",
        ]
        storyboard_path = next((p for p in alternatives if p.exists()), storyboard_path)
    if not storyboard_path.exists():
        raise SystemExit(
            f"Не найден файл раскадровки: {storyboard_path}. Передай --storyboard с правильным путём."
        )

    valid_cases = {"single", "pair"}
    selected_cases = [item.strip().lower() for item in args.visual_cases.split(",") if item.strip()]
    invalid_cases = [item for item in selected_cases if item not in valid_cases]
    if invalid_cases or not selected_cases:
        raise SystemExit(f"Некорректный --visual-cases: {invalid_cases or args.visual_cases}")
    valid_modes = {"json_schema_strict", "json_object", "prompt_only"}
    selected_modes = [item.strip().lower() for item in args.visual_formats.split(",") if item.strip()]
    invalid_modes = [item for item in selected_modes if item not in valid_modes]
    if invalid_modes or not selected_modes:
        raise SystemExit(f"Некорректный --visual-formats: {invalid_modes or args.visual_formats}")
    if args.visual_max_tokens < 256:
        raise SystemExit("--visual-max-tokens должен быть не меньше 256")
    if args.ollama_num_ctx < 1024:
        raise SystemExit("--ollama-num-ctx должен быть не меньше 1024")

    visual_cases: dict[str, list[dict[str, Any]]] = {}
    if "single" in selected_cases:
        visual_cases["single"] = load_storyboard_scenes(storyboard_path, ["s_001"])
    if "pair" in selected_cases:
        visual_cases["pair"] = load_storyboard_scenes(storyboard_path, ["s_001", "s_002"])

    report: dict[str, Any] = {
        "report_version": "1.2",
        "status": "in_progress",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "storyboard": str(storyboard_path),
            "visual_cases": {name: [scene["id"] for scene in scenes] for name, scenes in visual_cases.items()},
            "visual_formats": selected_modes,
            "visual_max_tokens": args.visual_max_tokens,
            "ollama_model_tags": [
                model.strip() for model in args.ollama_models.split(",") if model.strip()
            ],
            "ollama_num_ctx": args.ollama_num_ctx,
            "visual_system_prompt_sha256": sha256_text(VISUAL_SYSTEM_PROMPT),
            "visual_schema_sha256": sha256_text(json.dumps(
                _schema_for_provider(VisualPlanBatch), sort_keys=True, ensure_ascii=False
            )),
        },
        "providers": {},
    }

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"visual_planner_diagnostic_{stamp}.json"
    md_path = output_dir / f"visual_planner_diagnostic_{stamp}.md"

    def persist_report() -> None:
        write_json(json_path, report)
        md_path.write_text(summarize(report), encoding="utf-8")

    persist_report()

    try:
        for name, provider, visual_timeout in selected:
            details: dict[str, Any] = {
                "model": provider.model,
                "base_url_host": provider_host(provider.base_url),
                "tests": {},
            }
            report["providers"][name] = details
            log.info("=== Диагностика провайдера %s / %s ===", name, provider.model)

            log.info("[%s] 1/2: короткий запрос без response_format", name)
            ping = run_plain_probe(provider, args.probe_timeout, args.ollama_num_ctx)
            details["tests"]["plain_ping"] = ping
            persist_report()
            log.info("[%s] plain_ping: %s (%ss)", name, ping["status"], ping["elapsed_seconds"])
            if ping["status"] != "ok":
                details["tests"]["tiny_json_schema"] = {"status": "skipped", "reason": "plain_ping failed"}
                for case_name in visual_cases:
                    for response_mode in selected_modes:
                        details["tests"][f"visual_{case_name}_{response_mode}"] = {
                            "status": "skipped", "reason": "plain_ping failed"
                        }
                continue

            log.info("[%s] 2/2: проверка строгого JSON Schema на минимальной задаче", name)
            schema_probe = run_tiny_schema_probe(provider, args.schema_probe_timeout, args.ollama_num_ctx)
            details["tests"]["tiny_json_schema"] = schema_probe
            persist_report()
            log.info("[%s] tiny_json_schema: %s (%ss)", name, schema_probe["status"], schema_probe["elapsed_seconds"])

            total_visual = len(visual_cases) * len(selected_modes)
            completed_visual = 0
            for case_name, scenes in visual_cases.items():
                for response_mode in selected_modes:
                    completed_visual += 1
                    test_name = f"visual_{case_name}_{response_mode}"
                    log.info(
                        "[%s] visual %s/%s: case=%s, format=%s, scenes=%s",
                        name, completed_visual, total_visual, case_name, response_mode,
                        ",".join(scene["id"] for scene in scenes),
                    )
                    visual = run_visual_probe(
                        provider, scenes, visual_timeout,
                        response_mode=response_mode,
                        max_tokens=args.visual_max_tokens,
                        ollama_num_ctx=args.ollama_num_ctx,
                    )
                    details["tests"][test_name] = visual
                    persist_report()
                    log.info(
                        "[%s] %s: %s (%ss)", name, test_name,
                        visual["status"], visual["elapsed_seconds"],
                    )

    except KeyboardInterrupt:
        report["status"] = "interrupted_by_user"
        persist_report()
        print(f"\nДиагностика прервана. Промежуточный отчёт сохранён: {json_path}")
        return 130

    report["status"] = "complete"
    persist_report()

    print("\nДиагностика завершена.")
    print(f"JSON: {json_path}")
    print(f"Отчёт: {md_path}")
    print("API-ключи в отчёт не записываются.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
