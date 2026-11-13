"""Scene-level media production using existing Kokoro and Pixabay-only stock."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .common import read_json, write_json
from .text_normalization import normalize_for_speech
from .kokoro_engine import synthesize_kokoro
from .pixabay_media import fetch_pixabay, PixabayError
from .diagram_renderer import render_scene_visual
from .scene_validator import validate_timing


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _visual_job(settings: Any, scene: dict[str, Any], spec: dict[str, Any],
                output_dir: Path, warnings: list[str], index: int) -> dict[str, Any]:
    strategy = spec.get("asset_strategy", scene.get("asset_strategy"))
    visual_type = spec.get("visual_type", scene.get("visual_type"))
    queries = spec.get("search_queries", scene.get("search_queries", []))
    target = output_dir / "visuals"
    if strategy == "stock":
        query = (queries or [scene.get("visual_description", "")])[0]
        kind = "video" if visual_type in ("video", "broll") else "photo"
        try:
            asset = fetch_pixabay(settings, str(query), kind, target)
        except PixabayError as exc:
            warnings.append(f"{scene['id']}: {exc}; использован внутренний renderer")
            asset = None
        if asset:
            path = Path(asset["path"])
            asset["sha256"] = _sha256(path)
            asset["duration_ratio"] = float(spec.get("sub_duration_ratio", 1.0))
            return asset
        warnings.append(f"{scene['id']}: Pixabay не вернул {kind}; использован внутренний renderer")
    elif strategy == "generated":
        raise RuntimeError(
            f"{scene['id']}: generated provider не подключён. Платная генерация не выполняется."
        )
    elif strategy == "manual":
        raise RuntimeError(
            f"{scene['id']}: требуется ручной визуал. Добавьте файл в media_refs до produce."
        )
    elif strategy == "screencast":
        raise RuntimeError(
            f"{scene['id']}: screencast должен быть предоставлен оператором вручную."
        )

    rendered_scene = dict(scene)
    rendered_scene["visual_type"] = visual_type
    rendered_scene["visual_description"] = spec.get("visual_description", scene.get("visual_description", ""))
    rendered_scene["on_screen_text"] = spec.get("on_screen_text", scene.get("on_screen_text", []))
    rendered_scene["gen_prompt"] = spec.get("gen_prompt")
    rendered_scene["chart_data"] = scene.get("chart_data")
    path = render_scene_visual(rendered_scene, target / f"{scene['id']}_{index:02d}.png")
    return {
        "path": str(path.resolve()), "media_type": "image",
        "sha256": _sha256(path), "source": "Internal renderer",
        "source_url": None, "creator": None, "license_name": "Project original rendering",
        "rights_status": "project_generated", "duration_ratio":
            float(spec.get("sub_duration_ratio", 1.0)),
    }


def produce_media(settings: Any, storyboard_path: Path, output_dir: Path,
                  *, speed: float = 0.9) -> dict[str, Any]:
    if str(getattr(settings, "tts_engine", "kokoro")).lower() != "kokoro":
        raise RuntimeError("Semantic production requires TTS_ENGINE=kokoro; no engine fallback is allowed")
    if str(getattr(settings, "tts_speaker", "dima")).lower() != "dima":
        raise RuntimeError("Semantic production is pinned to Kokoro voice Dima")
    if not (0.5 <= float(speed) <= 2.0):
        raise ValueError("Скорость TTS должна быть в диапазоне 0.5..2.0")
    storyboard = read_json(storyboard_path)
    scenes = storyboard.get("scenes", [])
    if not scenes:
        raise ValueError("Раскадровка не содержит сцен")
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = output_dir / "audio"
    prepared = []
    for scene in scenes:
        raw = str(scene["voice_text"]).strip()
        normalized = normalize_for_speech(raw)
        if not normalized:
            raise ValueError(f"{scene['id']}: пустой текст после нормализации TTS")
        prepared.append((scene["id"], {"heading": scene.get("meaning", "")}, raw, normalized))

    # One Kokoro runtime, one WAV per scene. Existing production engine remains unchanged.
    audio_manifest = synthesize_kokoro(settings, prepared, audio_dir, speed)
    if audio_manifest.get("status") != "complete":
        raise RuntimeError("Kokoro не завершил синтез всех сцен")
    audio_by_id = {row["id"]: row for row in audio_manifest["blocks"]}

    warnings: list[str] = []
    cursor = 0.0
    produced_scenes = []
    for scene in scenes:
        row = audio_by_id.get(scene["id"])
        if not row:
            raise RuntimeError(f"В audio manifest отсутствует сцена {scene['id']}")
        wav_path = audio_dir / row["file"]
        duration = float(row["duration_seconds"])
        scene["duration_sec"] = round(duration, 3)
        scene["start_sec"] = round(cursor, 3)
        cursor += duration
        scene["end_sec"] = round(cursor, 3)
        scene["media_refs"] = {
            "audio": {
                "path": str(wav_path.resolve()), "media_type": "audio/wav",
                "sha256": _sha256(wav_path), "source": "Kokoro-ru v2 ONNX CPU",
                "source_url": None, "creator": None, "license_name": "See model/voice license decision",
                "rights_status": "review_required", "duration_ratio": 1.0,
            },
            "visuals": [],
        }
        specs = scene.get("visual_beats") or [{
            "visual_type": scene["visual_type"],
            "visual_description": scene["visual_description"],
            "asset_strategy": scene["asset_strategy"],
            "provider_id": scene["provider_id"],
            "gen_prompt": scene.get("gen_prompt"),
            "search_queries": scene.get("search_queries", []),
            "on_screen_text": scene.get("on_screen_text", []),
            "sub_duration_ratio": 1.0,
        }]
        if abs(sum(float(x["sub_duration_ratio"]) for x in specs) - 1.0) > 0.01:
            raise ValueError(f"{scene['id']}: visual_beats не суммируются в 1.0")
        for index, spec in enumerate(specs, 1):
            asset = _visual_job(settings, scene, spec, output_dir, warnings, index)
            scene["media_refs"]["visuals"].append(asset)
        warnings.extend(validate_timing(scene))
        produced_scenes.append(scene)

    storyboard["scenes"] = produced_scenes
    storyboard["production"] = {
        "status": "complete",
        "audio_engine": audio_manifest.get("engine"),
        "audio_manifest": str((audio_dir / "manifest.json").resolve()),
        "total_duration_seconds": round(cursor, 3),
        "visual_provider_policy": "Pixabay only; no Pexels calls; no paid generation providers",
        "warnings": warnings,
    }
    scene_path = output_dir / "storyboard_produced.json"
    write_json(scene_path, storyboard)
    manifest = {
        "status": "complete", "scene_count": len(produced_scenes),
        "total_duration_seconds": round(cursor, 3),
        "audio_manifest": str((audio_dir / "manifest.json").resolve()),
        "storyboard": str(scene_path.resolve()),
        "scenes": [{
            "id": s["id"], "start_sec": s["start_sec"], "end_sec": s["end_sec"],
            "duration_sec": s["duration_sec"], "audio": s["media_refs"]["audio"],
            "visuals": s["media_refs"]["visuals"],
        } for s in produced_scenes],
        "warnings": warnings,
    }
    write_json(output_dir / "media_manifest.json", manifest)
    return manifest
