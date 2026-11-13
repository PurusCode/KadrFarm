"""Production Kokoro-ru v2 ONNX CPU backend.

The model and RUAccent assets are downloaded/stored outside the source tree.
No Piper fallback is implemented.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
import platform
import re
import sys
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REPO_ID = "zaakirio/kokoro-ru"
REPO_REVISION = "d649c57"
VOICE = "dima"
SAMPLE_RATE = 24_000
MAX_IPA = 510
CHUNK_PAUSE_SECONDS = 0.18


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _wav_info(path: Path) -> dict[str, Any]:
    try:
        with wave.open(str(path), "rb") as wav:
            info = {
                "sample_rate": wav.getframerate(),
                "channels": wav.getnchannels(),
                "sample_width_bytes": wav.getsampwidth(),
                "frames": wav.getnframes(),
            }
    except (wave.Error, OSError) as exc:
        raise RuntimeError(f"Созданный Kokoro WAV повреждён: {path}") from exc
    if min(info["sample_rate"], info["channels"], info["frames"]) <= 0:
        raise RuntimeError(f"Kokoro создал пустой или некорректный WAV: {path}")
    info["duration_seconds"] = round(info["frames"] / info["sample_rate"], 3)
    return info


def _phonemize_checked(g2p: Any, text: str, block_id: str) -> str:
    ipa, oov = g2p.phonemize(text)
    if oov:
        raise ValueError(f"Блок {block_id}: фонемы вне словаря Kokoro: {sorted(oov)}")
    if not ipa:
        raise ValueError(f"Блок {block_id}: G2P вернул пустую строку")
    return ipa


def _split_overlong_unit(g2p: Any, text: str, block_id: str, max_ipa: int) -> list[str]:
    parts = [part.strip() for part in re.split(r"(?<=[,;:—–])\s+", text.strip()) if part.strip()]
    if len(parts) <= 1:
        parts = text.split()
    expanded: list[str] = []
    for part in parts:
        ipa = _phonemize_checked(g2p, part, block_id)
        if len(ipa) <= max_ipa:
            expanded.append(part)
            continue
        current = ""
        for word in part.split():
            candidate = f"{current} {word}".strip()
            candidate_ipa = _phonemize_checked(g2p, candidate, block_id)
            if len(candidate_ipa) <= max_ipa:
                current = candidate
            else:
                if not current:
                    raise ValueError(f"Блок {block_id}: фрагмент слова превышает лимит IPA {max_ipa}")
                expanded.append(current)
                current = word
                if len(_phonemize_checked(g2p, current, block_id)) > max_ipa:
                    raise ValueError(f"Блок {block_id}: отдельное слово превышает лимит IPA {max_ipa}")
        if current:
            expanded.append(current)
    return expanded


def _phonemize_in_chunks(g2p: Any, text: str, block_id: str, max_ipa: int = MAX_IPA) -> list[tuple[str, str]]:
    full_ipa = _phonemize_checked(g2p, text, block_id)
    if len(full_ipa) <= max_ipa:
        return [(text, full_ipa)]

    sentence_units = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]
    units: list[str] = []
    for unit in sentence_units:
        if len(_phonemize_checked(g2p, unit, block_id)) <= max_ipa:
            units.append(unit)
        else:
            units.extend(_split_overlong_unit(g2p, unit, block_id, max_ipa))

    chunks: list[tuple[str, str]] = []
    current = ""
    current_ipa = ""
    for unit in units:
        candidate = f"{current} {unit}".strip()
        candidate_ipa = _phonemize_checked(g2p, candidate, block_id)
        if len(candidate_ipa) <= max_ipa:
            current, current_ipa = candidate, candidate_ipa
        else:
            if current:
                chunks.append((current, current_ipa))
            current = unit
            current_ipa = _phonemize_checked(g2p, current, block_id)
            if len(current_ipa) > max_ipa:
                raise ValueError(f"Блок {block_id}: IPA превышает лимит {max_ipa} после разбиения")
    if current:
        chunks.append((current, current_ipa))
    if not chunks:
        raise ValueError(f"Блок {block_id}: не удалось разбить текст для Kokoro")
    return chunks


@dataclass
class KokoroRuntime:
    session: Any
    g2p: Any
    vocab: dict[str, int]
    style: Any
    model_root: Path
    accent_model_root: Path
    model_path: Path
    voice_path: Path
    stress_config_path: Path
    stress_config: dict[str, Any]
    model_config_path: Path
    kokoro_config_path: Path
    g2p_path: Path


def load_runtime(settings: Any, threads: int) -> KokoroRuntime:
    """Load/download pinned model files and construct the CPU inference runtime."""
    try:
        import numpy as np
        import onnxruntime as ort
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "Не установлены production-зависимости Kokoro. Выполните install.ps1 "
            "или установите пакет проекта с extras [tts]."
        ) from exc

    from .kokoro_stress import build_configured_g2p, load_stress_config

    model_root = settings.data_dir / "models" / "kokoro-ru-v2-onnx"
    accent_root = settings.data_dir / "models" / "ruaccent-accentuator"
    stress_path = settings.root / "data" / "kokoro_stress_config.json"
    stress_config = load_stress_config(stress_path)

    model_rel = "onnx/model_dima_quantized.onnx"
    allowed = [
        "config.json", "kokoro-config.json", "ru_g2p.py",
        "espeak-data/**", model_rel, "voices/dima.bin",
    ]
    model_root.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=REPO_ID,
        revision=REPO_REVISION,
        local_dir=str(model_root),
        allow_patterns=allowed,
        max_workers=4,
    )

    model_path = model_root / model_rel
    voice_path = model_root / "voices" / "dima.bin"
    model_config_path = model_root / "config.json"
    kokoro_config_path = model_root / "kokoro-config.json"
    g2p_path = model_root / "ru_g2p.py"
    required = [
        model_path, voice_path, model_config_path, kokoro_config_path,
        g2p_path, model_root / "espeak-data" / "ru_dict",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("Не найдены файлы модели Kokoro: " + ", ".join(missing))

    accent_required = [
        accent_root / "dictionary",
        accent_root / "nn" / "nn_accent",
        accent_root / "nn" / "nn_stress_usage_predictor",
        accent_root / "nn" / "nn_yo_homograph_resolver",
    ]
    missing_accent = [str(path) for path in accent_required if not path.is_dir()]
    if missing_accent:
        raise FileNotFoundError(
            "Не найдены локальные данные RUAccent. Ожидаются каталоги "
            "data/models/ruaccent-accentuator/dictionary и data/models/ruaccent-accentuator/nn/*. "
            "Не запускаю синтез без проверенных локальных данных ударений. "
            "Сначала перенесите/подготовьте RUAccent assets из benchmark-окружения."
        )

    sys.path.insert(0, str(model_root))
    g2p = build_configured_g2p(
        model_root=model_root,
        accent_model_root=accent_root,
        stress_config=stress_config,
    )
    model_cfg = json.loads(model_config_path.read_text(encoding="utf-8"))
    vocab = model_cfg.get("vocab")
    if not isinstance(vocab, dict) or not vocab:
        raise ValueError("Некорректный config.json Kokoro: отсутствует vocab")

    style = np.fromfile(voice_path, dtype=np.float32)
    if style.size != 510 * 256:
        raise ValueError(
            f"Некорректный voice pack {voice_path}: {style.size} float32, ожидалось {510 * 256}"
        )
    style = style.reshape(510, 256)

    session_options = ort.SessionOptions()
    session_options.intra_op_num_threads = max(1, int(threads))
    session_options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        str(model_path),
        sess_options=session_options,
        providers=["CPUExecutionProvider"],
    )
    providers = session.get_providers()
    if not providers or providers[0] != "CPUExecutionProvider":
        raise RuntimeError(f"Kokoro запущен не на CPUExecutionProvider: {providers}")

    return KokoroRuntime(
        session=session, g2p=g2p, vocab=vocab, style=style,
        model_root=model_root, accent_model_root=accent_root,
        model_path=model_path, voice_path=voice_path,
        stress_config_path=stress_path, stress_config=stress_config,
        model_config_path=model_config_path, kokoro_config_path=kokoro_config_path,
        g2p_path=g2p_path,
    )


def synthesize_kokoro(settings: Any, prepared: list[tuple[str, dict[str, Any], str, str]],
                      out_dir: Path, speed: float) -> dict[str, Any]:
    from .kokoro_stress import stress_config_sha256
    from .text_normalization import NORMALIZER_VERSION, find_numeric_tokens, normalizer_backend

    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    manifest_path = out_dir / "manifest.json"
    manifest: dict[str, Any] = {
        "status": "running",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": "Kokoro-ru v2 ONNX (CPU)",
        "engine_package": "onnxruntime",
        "engine_executable": "CPUExecutionProvider",
        "repository": REPO_ID,
        "revision": REPO_REVISION,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "voice": VOICE,
        "voice_gender": "male",
        "speed": speed,
        "sample_rate": SAMPLE_RATE,
        "configured_sample_rate": SAMPLE_RATE,
        "blocks_expected": len(prepared),
        "blocks_completed": 0,
        "blocks": [],
        "quality_warnings": [],
        "total_duration_seconds": 0.0,
        "elapsed_seconds": None,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        threads = int(os.getenv("KOKORO_THREADS", "2"))
        if threads < 1 or threads > 32:
            raise ValueError("KOKORO_THREADS должен быть в диапазоне 1..32")
        import numpy as np
        import soundfile as sf

        runtime = load_runtime(settings, threads)
        manifest.update({
            "engine_version": package_version("onnxruntime"),
            "threads": threads,
            "numpy_version": np.__version__,
            "soundfile_version": package_version("soundfile"),
            "ruaccent_version": package_version("ruaccent"),
            "misaki_version": package_version("misaki"),
            "phonemizer_fork_version": package_version("phonemizer-fork"),
            "espeakng_loader_version": package_version("espeakng-loader"),
            "text_normalizer_version": NORMALIZER_VERSION,
            "text_normalizer_backend": normalizer_backend(),
            "stress_config_schema_version": runtime.stress_config["schema_version"],
            "stress_config_sha256": stress_config_sha256(runtime.stress_config_path),
            "stress_config_file": str(runtime.stress_config_path),
            "stress_rule_ids": [rule["id"] for rule in runtime.stress_config["context_rules"]],
            "model_file": str(runtime.model_path),
            "model_size_bytes": runtime.model_path.stat().st_size,
            "model_sha256": sha256_file(runtime.model_path),
            "voice_file": str(runtime.voice_path),
            "voice_size_bytes": runtime.voice_path.stat().st_size,
            "voice_sha256": sha256_file(runtime.voice_path),
            "model_config_sha256": sha256_file(runtime.model_config_path),
            "kokoro_config_sha256": sha256_file(runtime.kokoro_config_path),
            "g2p_sha256": sha256_file(runtime.g2p_path),
            "model_license": "OpenRAIL (zaakirio/kokoro-ru; verify pinned revision)",
            "stress_config_rule_count": len(runtime.stress_config["context_rules"]),
        })
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        raise

    try:
        safe_names: set[str] = set()
        for index, (block_id, block, source_text, normalized_text) in enumerate(prepared, start=1):
            block_started = time.perf_counter()
            chunks = _phonemize_in_chunks(runtime.g2p, normalized_text, block_id)
            audio_parts: list[Any] = []
            pause = np.zeros(int(SAMPLE_RATE * CHUNK_PAUSE_SECONDS), dtype=np.float32)
            ipa_lengths: list[int] = []
            for chunk_index, (_chunk_text, ipa) in enumerate(chunks, start=1):
                unknown = sorted({char for char in ipa if char not in runtime.vocab})
                if unknown:
                    raise ValueError(
                        f"Блок {block_id}, часть {chunk_index}: символы IPA отсутствуют в vocab: {unknown}"
                    )
                ids = [int(runtime.vocab[char]) for char in ipa]
                inputs = {
                    "input_ids": np.asarray([[0, *ids, 0]], dtype=np.int64),
                    "style": runtime.style[len(ipa) - 1][None, :].astype(np.float32),
                    "speed": np.asarray([speed], dtype=np.float32),
                }
                generated = np.asarray(runtime.session.run(None, inputs)[0], dtype=np.float32).squeeze()
                if generated.ndim != 1 or generated.size < SAMPLE_RATE // 10:
                    raise ValueError(
                        f"Блок {block_id}, часть {chunk_index}: некорректная форма аудио {generated.shape}"
                    )
                if not np.isfinite(generated).all():
                    raise ValueError(f"Блок {block_id}, часть {chunk_index}: аудио содержит NaN/Inf")
                audio_parts.append(generated)
                ipa_lengths.append(len(ipa))
                if chunk_index < len(chunks):
                    audio_parts.append(pause)

            audio = np.concatenate(audio_parts).astype(np.float32, copy=False)
            safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", block_id).strip("_") or f"block_{index}"
            if safe_id in safe_names:
                safe_id = f"{safe_id}_{index}"
            safe_names.add(safe_id)
            filename = f"{index:02d}_{safe_id}.wav"
            wav_path = out_dir / filename
            sf.write(str(wav_path), audio, SAMPLE_RATE, subtype="PCM_16")
            info = _wav_info(wav_path)
            if info["sample_rate"] != SAMPLE_RATE or info["channels"] != 1 or info["sample_width_bytes"] != 2:
                raise RuntimeError(f"Блок {block_id}: WAV должен быть mono PCM16 {SAMPLE_RATE} Hz")
            elapsed = round(time.perf_counter() - block_started, 3)
            word_count = len(re.findall(r"\b[\wЁёА-Яа-я]+\b", normalized_text))
            numeric_leftovers = find_numeric_tokens(normalized_text)
            if numeric_leftovers:
                manifest["quality_warnings"].append(
                    f"Блок {block_id!r}: после нормализации остались числовые токены {numeric_leftovers}; проверьте текст."
                )
            row = {
                "id": block_id,
                "heading": str(block.get("heading", "")),
                "file": filename,
                "duration_seconds": info["duration_seconds"],
                "sample_rate": info["sample_rate"],
                "channels": info["channels"],
                "sample_width_bytes": info["sample_width_bytes"],
                "frames": info["frames"],
                "word_count": word_count,
                "seconds_per_word": round(info["duration_seconds"] / max(word_count, 1), 3),
                "source_voice_text": source_text,
                "normalized_voice_text": normalized_text,
                "source_voice_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                "normalized_text_sha256": hashlib.sha256(normalized_text.encode("utf-8")).hexdigest(),
                "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                "text_sha256": hashlib.sha256(normalized_text.encode("utf-8")).hexdigest(),
                "unconverted_numeric_tokens": numeric_leftovers,
                "elapsed_seconds": elapsed,
                "real_time_factor": round(elapsed / info["duration_seconds"], 4),
                "synthesis_chunks": len(chunks),
                "ipa_chunk_lengths": ipa_lengths,
                "chunk_pause_seconds": CHUNK_PAUSE_SECONDS if len(chunks) > 1 else 0.0,
            }
            manifest["blocks"].append(row)
            manifest["blocks_completed"] = len(manifest["blocks"])
            manifest["total_duration_seconds"] = round(
                sum(float(item["duration_seconds"]) for item in manifest["blocks"]), 3
            )
            manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info(
                "Kokoro: озвучен блок %s: %.1f сек.; синтез %.1f сек.; RTF %.3f",
                block_id, info["duration_seconds"], elapsed,
                elapsed / max(info["duration_seconds"], 0.001),
            )

        if [row["id"] for row in manifest["blocks"]] != [item[0] for item in prepared]:
            raise RuntimeError("Внутренняя ошибка: manifest не соответствует порядку блоков сценария")
        manifest["status"] = "complete"
        manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
