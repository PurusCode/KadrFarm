"""Shot-level editorial planning for Autonomous Video Engine 2.0.

Subdivides approved director beats into visual shots while preserving speech.
No timestamps are generated: durations come from measured Kokoro WAV files.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .director_models import DirectorBeat
from .llm_structured import StructuredLLMRouter


class Shot(DirectorBeat):
    """A visual shot anchored to an exact fragment of an approved director beat."""

    shot_id: str = Field(min_length=1, max_length=40)
    source_beat_id: str = Field(min_length=1, max_length=40)
    cut_reason: str = Field(min_length=5, max_length=400)


class SceneShotPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_id: str = Field(min_length=1, max_length=30)
    shots: list[Shot] = Field(min_length=1, max_length=24)


class ShotPlanBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenes: list[SceneShotPlan] = Field(min_length=1)


SYSTEM_PROMPT = """Ты — монтажёр-режиссёр, создающий shot-level монтажный план.
На входе уже утверждённые режиссёром сцены и речевые beats. Разбей каждый исходный beat
на 1–4 самостоятельных визуальных shot только там, где есть реальная смена действия,
объекта, масштаба или визуального аргумента. Не дроби речь механически на равные интервалы.
Если один сильный кадр может удерживать мысль, оставь один shot.

КРИТИЧЕСКОЕ СОХРАНЕНИЕ РЕЧИ:
- Для каждого source_beat_id склей spoken_text всех его shots обычными пробелами.
  Результат должен дословно совпадать с исходным source beat, включая слова и пунктуацию.
- Каждый spoken_text — непрерывный фрагмент исходного текста. Никаких пересказов,
  исправлений, пропусков, повторов, перестановок.
- Не меняй scene_id и source_beat_id; shot_id должен иметь вид
  <source_beat_id>_s01, <source_beat_id>_s02 и т.д.
- Не назначай таймкоды и длительности. Их рассчитает система по реальным WAV.
- Один shot должен соответствовать одному ясному визуальному действию или состоянию.
  Не дроби предложение только ради частой смены кадров.
- Для реального действия предпочитай stock/video; для абстрактной причинно-следственной
  связи — локальную графику. Избегай статичных карточек, если мысль можно показать
  последовательностью действий, объектов или состояний.
- Используй только стратегии и типы, допустимые исходной схемой. Не придумывай новые.
- Не добавляй новые факты, цифры, статистику, надписи или утверждения.
- cut_reason кратко объясняет, почему здесь нужен отдельный монтажный план.
- visual_description — внутренняя инструкция движку, а не видимый текст.
  on_screen_text — только короткая надпись для зрителя.
Верни ровно один SceneShotPlan на каждую входную сцену."""


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _check_group_coverage(
    source_beats: list[dict[str, Any]],
    shots: list[dict[str, Any]],
    scene_id: str,
) -> None:
    expected = {
        f"b{i:02d}": str(beat["spoken_text"])
        for i, beat in enumerate(source_beats, 1)
    }
    actual: dict[str, list[dict[str, Any]]] = {key: [] for key in expected}
    seen_ids: set[str] = set()

    for shot in shots:
        source_id = shot["source_beat_id"]
        if source_id not in actual:
            raise ValueError(f"{scene_id}: unknown source_beat_id {source_id}")
        shot_id = shot["shot_id"]
        if shot_id in seen_ids:
            raise ValueError(f"{scene_id}: duplicate shot_id {shot_id}")
        seen_ids.add(shot_id)
        actual[source_id].append(shot)

    for source_id, source_text in expected.items():
        rows = sorted(actual[source_id], key=lambda row: row["shot_id"])
        if not rows:
            raise ValueError(f"{scene_id}/{source_id}: source beat has no shots")
        if len(rows) > 4:
            raise ValueError(f"{scene_id}/{source_id}: more than four shots for one source beat")
        for index, row in enumerate(rows, 1):
            expected_shot_id = f"{source_id}_s{index:02d}"
            if row["shot_id"] != expected_shot_id:
                raise ValueError(
                    f"{scene_id}/{source_id}: expected {expected_shot_id}, got {row['shot_id']}"
                )
        combined = " ".join(row["spoken_text"] for row in rows)
        if _norm(combined) != _norm(source_text):
            raise ValueError(
                f"{scene_id}/{source_id}: shots do not exactly preserve source narration"
            )


def _validate_cached_scene(
    scene_id: str,
    source_beats: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    validated = [Shot.model_validate(row).model_dump(mode="json") for row in rows]
    _check_group_coverage(source_beats, validated, scene_id)
    return validated


def plan_shots(
    settings: Any,
    scenes: list[dict[str, Any]],
    director_plans: list[dict[str, Any]],
    output_path: Path,
    batch_size: int = 2,
) -> dict[str, Any]:
    """Create a validated, resumable shot plan; never accept model-made timestamps."""
    if not 1 <= int(batch_size) <= 4:
        raise ValueError("shot planner batch_size must be 1..4")

    scene_by_id = {scene["id"]: scene for scene in scenes}
    plan_by_id = {plan["scene_id"]: plan for plan in director_plans}
    if len(scene_by_id) != len(scenes) or len(plan_by_id) != len(director_plans):
        raise ValueError("Shot planner input contains duplicate scene IDs")
    if not scenes or set(scene_by_id) != set(plan_by_id):
        raise ValueError("Shot planner input scene IDs do not match director plan")
    for scene_id, plan in plan_by_id.items():
        if not plan.get("beats"):
            raise ValueError(f"{scene_id}: director plan has no beats")

    source_fingerprint = hashlib.sha256(
        json.dumps(
            {"scenes": scenes, "plans": director_plans},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    progress_path = output_path.with_suffix(".progress.json")
    completed: dict[str, list[dict[str, Any]]] = {}
    if progress_path.is_file():
        checkpoint = json.loads(progress_path.read_text(encoding="utf-8-sig"))
        if checkpoint.get("source_sha256") != source_fingerprint:
            raise ValueError(
                "Shot planner checkpoint belongs to different input; "
                "choose a fresh output directory"
            )
        cached = checkpoint.get("completed", {})
        if not isinstance(cached, dict):
            raise ValueError("Shot planner checkpoint has invalid completed section")
        unknown = set(cached) - set(scene_by_id)
        if unknown:
            raise ValueError(f"Shot planner checkpoint contains unknown scenes: {sorted(unknown)}")
        for scene_id, rows in cached.items():
            completed[scene_id] = _validate_cached_scene(
                scene_id, plan_by_id[scene_id]["beats"], rows
            )

    router = StructuredLLMRouter(settings)
    pending = [scene for scene in scenes if scene["id"] not in completed]
    for offset in range(0, len(pending), int(batch_size)):
        batch = pending[offset : offset + int(batch_size)]
        payload = []
        for scene in batch:
            plan = plan_by_id[scene["id"]]
            payload.append({
                "scene_id": scene["id"],
                "meaning": scene.get("meaning", ""),
                "editorial_intent": plan.get("editorial_intent", ""),
                "source_beats": [
                    {"source_beat_id": f"b{i:02d}", **beat}
                    for i, beat in enumerate(plan["beats"], 1)
                ],
            })

        result = router.complete(
            ShotPlanBatch,
            SYSTEM_PROMPT,
            "Составь монтажный shot-план для следующих сцен.\n"
            + json.dumps(payload, ensure_ascii=False),
            temperature=0.2,
            max_tokens=min(8500, 2200 + len(batch) * 2600),
        )
        received = [item.scene_id for item in result.scenes]
        expected_ids = [scene["id"] for scene in batch]
        if len(received) != len(set(received)) or set(received) != set(expected_ids):
            raise ValueError(
                f"Shot planner returned wrong scene IDs: expected {expected_ids}, got {received}"
            )

        for item in result.scenes:
            rows = [shot.model_dump(mode="json") for shot in item.shots]
            completed[item.scene_id] = _validate_cached_scene(
                item.scene_id, plan_by_id[item.scene_id]["beats"], rows
            )

        _atomic_json(progress_path, {
            "source_sha256": source_fingerprint,
            "completed": completed,
            "completed_scene_count": len(completed),
            "status": "in_progress",
        })

    if set(completed) != set(scene_by_id):
        raise RuntimeError("Shot planner ended without a complete scene set")

    flattened = []
    for scene in scenes:
        original = plan_by_id[scene["id"]]
        rows = sorted(
            completed[scene["id"]],
            key=lambda row: (row["source_beat_id"], row["shot_id"]),
        )
        flattened.append({
            "scene_id": scene["id"],
            "editorial_intent": original.get("editorial_intent", ""),
            "claims": original.get("claims", []),
            "music_cue": original.get("music_cue", ""),
            "beats": rows,
        })

    payload = {
        "schema_version": "2.0-shot-edl",
        "status": "validated",
        "source_sha256": source_fingerprint,
        "scene_count": len(flattened),
        "source_beat_count": sum(len(plan["beats"]) for plan in director_plans),
        "shot_count": sum(len(plan["beats"]) for plan in flattened),
        "timing_policy": "measured_kokoro_wav_duration; no model-generated timestamps",
        "plans": flattened,
    }
    _atomic_json(output_path, payload)
    _atomic_json(progress_path, {
        "source_sha256": source_fingerprint,
        "completed": completed,
        "completed_scene_count": len(completed),
        "status": "complete",
    })
    return payload
