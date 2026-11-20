"""Visual audit and contact sheets for KadrFarm Visual Story V2."""

from __future__ import annotations

import json

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .visual_scene_compiler_v2 import ShotJob
from .visual_story_v2 import VisualStoryPlan


def create_contact_sheet(
    frame_paths: list[Path],
    output_path: str | Path,
    *,
    columns: int = 4,
    thumbnail_width: int = 480,
    thumbnail_height: int = 270,
) -> Path:
    if not frame_paths:
        raise ValueError("No frames supplied for contact sheet")

    rows = (
        len(frame_paths) + columns - 1
    ) // columns

    label_height = 30

    sheet = Image.new(
        "RGB",
        (
            columns * thumbnail_width,
            rows * (thumbnail_height + label_height),
        ),
        "#101827",
    )

    draw = ImageDraw.Draw(sheet)

    for index, path in enumerate(frame_paths):
        image = Image.open(path).convert("RGB")

        image.thumbnail(
            (
                thumbnail_width,
                thumbnail_height,
            ),
            Image.Resampling.LANCZOS,
        )

        x = (index % columns) * thumbnail_width
        y = (index // columns) * (
            thumbnail_height + label_height
        )

        sheet.paste(
            image,
            (x, y),
        )

        draw.text(
            (x + 8, y + thumbnail_height + 5),
            Path(path).name,
            fill="#FFFFFF",
        )

    output_path = Path(output_path)
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    sheet.save(
        output_path,
        quality=92,
    )

    return output_path


def write_audit(
    plan: VisualStoryPlan,
    jobs: list[ShotJob],
    measured_durations: dict[str, float],
    speech_hashes: dict[str, str],
    output_path: str | Path,
    *,
    video_probe: dict[str, Any] | None = None,
) -> Path:
    report = {
        "schema_version": "2.0",
        "title": plan.title,
        "resolution": [
            plan.width,
            plan.height,
        ],
        "fps": plan.fps,
        "speech_units": [
            {
                "unit_id": unit.unit_id,
                "text": unit.text,
                "duration_sec": round(
                    measured_durations[unit.unit_id],
                    4,
                ),
                "sha256": speech_hashes[unit.unit_id],
            }
            for unit in plan.speech_units
        ],
        "shots": [],
        "objects": [],
        "video_probe": video_probe,
    }

    for job in jobs:
        report["shots"].append(
            {
                "shot_id": job.shot_id,
                "start_sec": round(job.start_sec, 4),
                "end_sec": round(job.end_sec, 4),
                "duration_sec": round(job.duration_sec, 4),
                "frame_start": job.frame_start,
                "frame_end": job.frame_end,
                "event_count": len(job.events),
            }
        )

    for shot in plan.shots:
        for obj in shot.objects:
            report["objects"].append(
                {
                    "shot_id": shot.shot_id,
                    "object_id": obj.object_id,
                    "kind": obj.kind,
                    "role": obj.role,
                    "initial_state": obj.initial_state,
                    "continuity_id": obj.continuity_id,
                }
            )

    output_path = Path(output_path)

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return output_path