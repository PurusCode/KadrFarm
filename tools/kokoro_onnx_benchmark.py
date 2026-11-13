"""Evaluation-only Kokoro-ru v2 ONNX runner; not wired into production CLI.

Downloads only the selected quantized ONNX model, its voice pack and Russian
G2P assets. Review the upstream model/code/license before running this script.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
import soundfile as sf
from huggingface_hub import snapshot_download

# Share the same pre-TTS text normalization as the production CLI; this keeps
# Piper/Kokoro A/B tests comparable. The module has no dependency on Piper.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
from kadr_farm.kokoro_stress import (  # noqa: E402
    build_configured_g2p,
    load_stress_config,
    stress_config_sha256,
)
from kadr_farm.text_normalization import (  # noqa: E402
    NORMALIZER_VERSION,
    find_numeric_tokens,
    normalizer_backend,
    normalize_for_speech,
)

REPO_ID = "zaakirio/kokoro-ru"
# Pin the observed Hugging Face snapshot so a future `main` update won't
# silently change the model or its frontend during this benchmark.
REPO_REVISION = "d649c57"
SAMPLE_RATE = 24000


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


def _phonemize_checked(g2p: Any, text: str, block_id: str) -> str:
    ipa, oov = g2p.phonemize(text)
    if oov:
        raise ValueError(f"Блок {block_id}: фонемы вне словаря Kokoro: {sorted(oov)}")
    if not ipa:
        raise ValueError(f"Блок {block_id}: G2P вернул пустую строку")
    return ipa


def _split_overlong_unit(g2p: Any, text: str, block_id: str, max_ipa: int) -> list[str]:
    """Split a too-long sentence at clause punctuation, then at spaces if needed."""
    parts = [x.strip() for x in re.split(r"(?<=[,;:—–])\s+", text.strip()) if x.strip()]
    if len(parts) <= 1:
        parts = text.split()
    expanded: list[str] = []
    for part in parts:
        ipa = _phonemize_checked(g2p, part, block_id)
        if len(ipa) <= max_ipa:
            expanded.append(part)
            continue
        # Last-resort split for an unusually long clause/token sequence.
        words = part.split()
        if len(words) <= 1:
            raise ValueError(
                f"Блок {block_id}: один фрагмент даёт IPA длиной {len(ipa)} "
                f"и не может быть разделён безопасно (лимит {max_ipa})"
            )
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            candidate_ipa = _phonemize_checked(g2p, candidate, block_id)
            if len(candidate_ipa) <= max_ipa:
                current = candidate
            else:
                if not current:
                    raise ValueError(
                        f"Блок {block_id}: фрагмент слова превышает лимит IPA {max_ipa}"
                    )
                expanded.append(current)
                current = word
                word_ipa = _phonemize_checked(g2p, current, block_id)
                if len(word_ipa) > max_ipa:
                    raise ValueError(
                        f"Блок {block_id}: фрагмент превышает лимит IPA {max_ipa}"
                    )
        if current:
            expanded.append(current)
    return expanded


def _phonemize_in_chunks(g2p: Any, text: str, block_id: str, max_ipa: int = 510) -> list[tuple[str, str]]:
    """Keep a block as intact as possible; split only when Kokoro's style limit requires it."""
    full_ipa = _phonemize_checked(g2p, text, block_id)
    if len(full_ipa) <= max_ipa:
        return [(text, full_ipa)]

    sentence_units = [x.strip() for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x.strip()]
    units: list[str] = []
    for unit in sentence_units:
        unit_ipa = _phonemize_checked(g2p, unit, block_id)
        if len(unit_ipa) <= max_ipa:
            units.append(unit)
        else:
            units.extend(_split_overlong_unit(g2p, unit, block_id, max_ipa))

    # Greedily combine adjacent sentences/clauses without crossing the IPA limit.
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
                raise ValueError(
                    f"Блок {block_id}: IPA длиной {len(current_ipa)} превышает лимит {max_ipa} "
                    "даже после разбиения на предложения/части"
                )
    if current:
        chunks.append((current, current_ipa))
    if not chunks:
        raise ValueError(f"Блок {block_id}: не удалось разбить текст для Kokoro")
    return chunks


def download_assets(root: Path, voice: str) -> Path:
    voice = voice.lower()
    if voice not in {"dima", "sveta"}:
        raise ValueError("--voice должен быть dima или sveta")

    model_rel = "onnx/model_dima_quantized.onnx" if voice == "dima" else "onnx/model_quantized.onnx"
    allowed = [
        "config.json",
        "kokoro-config.json",
        "ru_g2p.py",
        "espeak-data/**",
        model_rel,
        f"voices/{voice}.bin",
    ]
    root.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=REPO_ID,
        revision=REPO_REVISION,
        local_dir=str(root),
        allow_patterns=allowed,
        max_workers=4,
    )
    required = [root / model_rel, root / f"voices/{voice}.bin", root / "config.json", root / "kokoro-config.json", root / "ru_g2p.py", root / "espeak-data/ru_dict"]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("Не найдены необходимые файлы Kokoro-ru: " + ", ".join(missing))
    return root


def run(args: argparse.Namespace) -> int:
    project_root = Path(__file__).resolve().parents[1]
    model_root = Path(args.model_dir).resolve() if args.model_dir else project_root / "data" / "models" / "kokoro-ru-v2-onnx"
    accent_model_root = (
        Path(args.accent_model_dir).resolve()
        if args.accent_model_dir
        else project_root / "data" / "models" / "ruaccent-accentuator"
    )
    stress_config_path = (
        Path(args.stress_config).resolve()
        if args.stress_config
        else project_root / "data" / "kokoro_stress_config.json"
    )
    stress_config = load_stress_config(stress_config_path)

    script_path = Path(args.script).resolve()
    output_dir = Path(args.out).resolve()
    if not script_path.is_file():
        raise FileNotFoundError(f"Не найден сценарий: {script_path}")

    with script_path.open("r", encoding="utf-8-sig") as stream:
        script: dict[str, Any] = json.load(stream)
    blocks = script.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("В сценарии должен быть непустой список blocks")

    download_assets(model_root, args.voice)
    sys.path.insert(0, str(model_root))
    # Use the explicitly pinned local RUAccent model data. The upstream RuG2P
    # constructor does not expose RUAccent.load(workdir/custom_dict), so the
    # helper wraps RUAccent.load only while constructing this G2P instance.
    g2p = build_configured_g2p(
        model_root=model_root,
        accent_model_root=accent_model_root,
        stress_config=stress_config,
    )
    vocab = json.loads((model_root / "config.json").read_text(encoding="utf-8"))["vocab"]
    if not isinstance(vocab, dict):
        raise ValueError("Неожиданный формат config.json: vocab должен быть объектом")

    model_rel = "onnx/model_dima_quantized.onnx" if args.voice == "dima" else "onnx/model_quantized.onnx"
    model_path = model_root / model_rel
    voice_path = model_root / "voices" / f"{args.voice}.bin"
    style = np.fromfile(voice_path, dtype=np.float32)
    if style.size != 510 * 256:
        raise ValueError(f"Неожиданный размер voice pack {voice_path}: {style.size} float32, ожидалось {510*256}")
    style = style.reshape(510, 256)

    session_options = ort.SessionOptions()
    session_options.intra_op_num_threads = max(1, args.threads)
    session_options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(model_path), sess_options=session_options, providers=["CPUExecutionProvider"])

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "status": "running",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "engine": "Kokoro-ru v2 ONNX (evaluation only)",
        "repository": REPO_ID,
        "revision": REPO_REVISION,
        "voice": args.voice,
        "text_normalizer_version": NORMALIZER_VERSION,
        "text_normalizer_backend": normalizer_backend(),
        "stress_config_schema_version": stress_config["schema_version"],
        "stress_config_sha256": stress_config_sha256(stress_config_path),
        "stress_config_file": str(stress_config_path),
        "stress_rule_ids": [rule["id"] for rule in stress_config["context_rules"]],
        "ruaccent_model_dir": str(accent_model_root),
        "python_version": sys.version,
        "onnxruntime_version": ort.__version__,
        "numpy_version": np.__version__,
        "ruaccent_version": package_version("ruaccent"),
        "misaki_version": package_version("misaki"),
        "phonemizer_fork_version": package_version("phonemizer-fork"),
        "model_file": str(model_path),
        "model_sha256": sha256_file(model_path),
        "voice_file": str(voice_path),
        "voice_sha256": sha256_file(voice_path),
        "script_file": str(script_path),
        "blocks": [],
    }
    total_start = time.perf_counter()
    try:
        for index, block in enumerate(blocks, start=1):
            block_id = str(block.get("id") or f"block_{index}")
            source_text = str(block.get("voice_text") or "").strip()
            if not source_text:
                raise ValueError(f"В блоке {block_id} пустой voice_text")
            text = normalize_for_speech(source_text)
            block_start = time.perf_counter()
            ipa_chunks = _phonemize_in_chunks(g2p, text, block_id, max_ipa=510)
            audio_chunks: list[np.ndarray] = []
            pause = np.zeros(int(SAMPLE_RATE * 0.18), dtype=np.float32)
            chunk_lengths: list[int] = []
            for chunk_index, (chunk_text, ipa) in enumerate(ipa_chunks, start=1):
                unknown = sorted({char for char in ipa if char not in vocab})
                if unknown:
                    raise ValueError(
                        f"Блок {block_id}, часть {chunk_index}: символы IPA отсутствуют в config vocab: {unknown}"
                    )
                ids = [int(vocab[char]) for char in ipa]
                inputs = {
                    "input_ids": np.asarray([[0, *ids, 0]], dtype=np.int64),
                    "style": style[len(ipa) - 1][None, :].astype(np.float32),
                    "speed": np.asarray([args.speed], dtype=np.float32),
                }
                generated = session.run(None, inputs)[0]
                generated = np.asarray(generated, dtype=np.float32).squeeze()
                if generated.ndim != 1 or generated.size < SAMPLE_RATE // 10:
                    raise ValueError(
                        f"Блок {block_id}, часть {chunk_index}: некорректная форма аудио {generated.shape}"
                    )
                audio_chunks.append(generated)
                chunk_lengths.append(len(ipa))
                if chunk_index < len(ipa_chunks):
                    audio_chunks.append(pause)

            wave = np.concatenate(audio_chunks).astype(np.float32, copy=False)
            wav_path = output_dir / f"{index:02d}_{block_id}.wav"
            sf.write(str(wav_path), wave, SAMPLE_RATE, subtype="PCM_16")
            elapsed = time.perf_counter() - block_start
            duration = float(wave.size / SAMPLE_RATE)
            item = {
                "id": block_id,
                "heading": str(block.get("heading", "")),
                "file": wav_path.name,
                "duration_seconds": round(duration, 3),
                "sample_rate": SAMPLE_RATE,
                "channels": 1,
                "word_count": len(text.split()),
                "source_voice_text": source_text,
                "normalized_voice_text": text,
                "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "unconverted_numeric_tokens": find_numeric_tokens(text),
                "elapsed_seconds": round(elapsed, 3),
                "real_time_factor": round(elapsed / duration, 4),
                "synthesis_chunks": len(ipa_chunks),
                "ipa_chunk_lengths": chunk_lengths,
                "chunk_pause_seconds": 0.18 if len(ipa_chunks) > 1 else 0.0,
            }
            manifest["blocks"].append(item)
            print(
                f"{block_id}: {duration:.1f} сек.; синтез {elapsed:.1f} сек.; "
                f"RTF {elapsed/duration:.3f}; частей: {len(ipa_chunks)} "
                f"(длины IPA: {chunk_lengths})"
            )
        manifest["status"] = "complete"
    except Exception:
        manifest["status"] = "failed"
        manifest["elapsed_seconds"] = round(time.perf_counter() - total_start, 3)
        (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        raise

    manifest["elapsed_seconds"] = round(time.perf_counter() - total_start, 3)
    manifest["total_duration_seconds"] = round(sum(x["duration_seconds"] for x in manifest["blocks"]), 3)
    manifest["blocks_expected"] = len(blocks)
    manifest["blocks_completed"] = len(manifest["blocks"])
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Готово: {len(blocks)} блока(ов), {manifest['total_duration_seconds']:.1f} сек.; manifest: {output_dir / 'manifest.json'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Изолированный тест Kokoro-ru v2 ONNX; не production TTS")
    parser.add_argument("--script", default="data/tts_quality_benchmark_script.json")
    parser.add_argument("--out", default="output/kokoro-dima-benchmark")
    parser.add_argument("--voice", choices=["dima", "sveta"], default="dima")
    parser.add_argument("--model-dir", default=None, help="Каталог кеша модели; по умолчанию data/models/kokoro-ru-v2-onnx")
    parser.add_argument("--accent-model-dir", default=None, help="Локальные данные RUAccent; по умолчанию data/models/ruaccent-accentuator")
    parser.add_argument("--stress-config", default=None, help="JSON с пользовательскими ударениями; по умолчанию data/kokoro_stress_config.json")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Остановлено пользователем.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"ОШИБКА Kokoro benchmark: {exc}", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(1)
