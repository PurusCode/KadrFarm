from __future__ import annotations

import argparse
import json
import wave
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Проверка совместимости Kokoro WAV/manifest с контрактом assemble.py."
    )
    parser.add_argument(
        "--script", default="data/kokoro_stress_audio_test_script.json",
        help="JSON-сценарий, переданный в production-команду tts.",
    )
    parser.add_argument(
        "--audio-dir", default="output/kokoro-production-smoke",
        help="Каталог с WAV и manifest.json, созданными production-командой tts.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    script_path = (root / args.script).resolve()
    audio_dir = (root / args.audio_dir).resolve()
    manifest_path = audio_dir / "manifest.json"

    for path in (script_path, manifest_path):
        if not path.is_file():
            print(f"ERROR: не найден файл: {path}", file=sys.stderr)
            return 2

    try:
        script = json.loads(script_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: не удалось прочитать JSON: {exc}", file=sys.stderr)
        return 3

    if manifest.get("status") != "complete":
        print(f"ERROR: manifest status={manifest.get('status')!r}, ожидался 'complete'.", file=sys.stderr)
        return 4
    if manifest.get("engine") != "Kokoro-ru v2 ONNX (CPU)":
        print(f"ERROR: неожиданный движок: {manifest.get('engine')!r}.", file=sys.stderr)
        return 5
    if manifest.get("engine_executable") != "CPUExecutionProvider":
        print(f"ERROR: неожиданный ONNX provider: {manifest.get('engine_executable')!r}.", file=sys.stderr)
        return 6

    # Call the same validation function that assemble_video uses before creating a timeline.
    try:
        from kadr_farm.assemble import _validate_audio_manifest
        rows_by_id = _validate_audio_manifest(script.get("blocks", []), manifest, audio_dir)
    except Exception as exc:
        print(f"ERROR: assemble.py rejected the Kokoro manifest: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 7

    if len(rows_by_id) != len(script.get("blocks", [])):
        print("ERROR: число проверенных блоков не совпадает со сценарием.", file=sys.stderr)
        return 8

    print("assemble.py manifest contract: PASS")
    print(f"Engine: {manifest['engine']} / {manifest['engine_executable']}")
    print(f"Revision: {manifest.get('revision')} / voice: {manifest.get('voice')}")
    print("WAV validation:")

    for block in script["blocks"]:
        block_id = str(block.get("id") or "")
        row = rows_by_id[block_id]
        wav_path = audio_dir / row["file"]
        with wave.open(str(wav_path), "rb") as wav:
            sample_rate = wav.getframerate()
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            frames = wav.getnframes()
            measured_duration = frames / sample_rate if sample_rate else 0.0

        if sample_rate != 24000 or channels != 1 or sample_width != 2:
            print(
                f"ERROR: {wav_path.name}: ожидался 24000 Hz / mono / PCM16, "
                f"получено {sample_rate} Hz / {channels} ch / {sample_width * 8} bit.",
                file=sys.stderr,
            )
            return 9
        declared_duration = float(row["duration_seconds"])
        if abs(declared_duration - measured_duration) > max(0.1, measured_duration * 0.01):
            print(f"ERROR: длительность {wav_path.name} не совпадает с manifest.", file=sys.stderr)
            return 10

        print(
            f"  PASS {block_id}: {wav_path.name}; {sample_rate} Hz; mono; PCM16; "
            f"{measured_duration:.3f} s"
        )

    print(f"PASS: проверено блоков {len(rows_by_id)}; manifest пригоден для входной валидации assemble.py.")
    print("Примечание: проверка не заменяет полный рендер MP4 со слайдами.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
