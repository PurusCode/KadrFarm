"""Scene sync quality-control checks for the semantic pipeline."""
from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path
from typing import Any

from .common import read_json, write_json
from .scene_validator import validate_timing


def _probe(ffprobe: str, path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,duration,width,height,codec_name",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return json.loads(proc.stdout)


def run_scene_qc(ffprobe: str, storyboard_path: Path,
                 video_path: Path, output_path: Path) -> dict[str, Any]:
    storyboard = read_json(storyboard_path)
    errors: list[str] = []
    warnings: list[str] = []
    scenes = storyboard.get("scenes", [])
    cursor = 0.0
    expected_total = 0.0
    for scene in scenes:
        sid = scene["id"]
        duration = float(scene.get("duration_sec") or 0)
        start, end = scene.get("start_sec"), scene.get("end_sec")
        if duration <= 0 or start is None or end is None:
            errors.append(f"{sid}: нет рассчитанной длительности/таймкодов")
            continue
        if abs(float(start) - cursor) > 0.05:
            errors.append(f"{sid}: временная шкала не является непрерывной")
        if abs((float(end) - float(start)) - duration) > 0.08:
            errors.append(f"{sid}: end-start не совпадает с duration_sec")
        cursor = float(end)
        expected_total += duration
        warnings.extend(validate_timing(scene))

        refs = scene.get("media_refs") or {}
        audio = refs.get("audio") or {}
        audio_path = Path(audio.get("path") or "")
        if not audio_path.is_file():
            errors.append(f"{sid}: WAV отсутствует")
        else:
            try:
                with wave.open(str(audio_path), "rb") as wav:
                    measured = wav.getnframes() / wav.getframerate()
                if abs(measured - duration) > max(0.12, duration * 0.01):
                    errors.append(f"{sid}: длительность WAV не совпадает с manifest")
            except (wave.Error, OSError) as exc:
                errors.append(f"{sid}: WAV не читается: {exc}")
        visuals = refs.get("visuals") or []
        if not visuals:
            errors.append(f"{sid}: нет визуального материала")
        for visual in visuals:
            if not Path(visual.get("path") or "").is_file():
                errors.append(f"{sid}: отсутствует визуал {visual.get('path')}")

    if not video_path.is_file():
        errors.append("Итоговый MP4 не найден")
        actual = 0.0
        probe = {}
    else:
        try:
            probe = _probe(ffprobe, video_path)
            actual = float(probe.get("format", {}).get("duration") or 0)
            streams = probe.get("streams", [])
            if not any(s.get("codec_type") == "video" for s in streams):
                errors.append("В MP4 отсутствует видеопоток")
            if not any(s.get("codec_type") == "audio" for s in streams):
                errors.append("В MP4 отсутствует аудиопоток")
            if abs(actual - expected_total) > max(1.5, len(scenes) * 0.08):
                errors.append(
                    f"Длительность видео {actual:.2f}s не совпадает с суммой сцен {expected_total:.2f}s"
                )
        except Exception as exc:
            actual, probe = 0.0, {}
            errors.append(f"ffprobe не смог проверить MP4: {exc}")

    assemble_report_path = video_path.with_suffix(".assemble.json")
    if assemble_report_path.is_file():
        try:
            assemble_report = read_json(assemble_report_path)
            warnings.extend(assemble_report.get("warnings", []))
        except Exception as exc:
            warnings.append(f"Не удалось прочитать assemble report: {exc}")

    report = {
        "status": "failed" if errors else ("passed_with_warnings" if warnings else "passed"),
        "scene_count": len(scenes), "expected_duration_seconds": round(expected_total, 3),
        "video_duration_seconds": round(actual, 3), "errors": errors,
        "warnings": warnings, "video": str(video_path.resolve()) if video_path.exists() else None,
        "ffprobe": probe,
    }
    write_json(output_path, report)
    return report
