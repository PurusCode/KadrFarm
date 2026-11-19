"""Shot-level editorial planning for Autonomous Video Engine 2.0.

Subdivides approved director beats into visual shots while preserving speech.
No timestamps are generated: durations come from measured Kokoro WAV files.

v2.1: retry с корректирующим промптом + детерминированный fallback
(1 shot на beat), если модель не соблюдает контракт scene_id.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .director_models import DirectorBeat
from .llm_structured import StructuredLLMRouter
from .production_recovery import ProductionJournal

log = logging.getLogger("kadr_farm.director_shots")


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


SHOT_POLICY_VERSION = "semantic-shot-inheritance-2.0"

SYSTEM_PROMPT = """Ты — монтажёр-режиссёр, создающий shot-level монтажный план.
На входе утверждённые главным режиссёром сцены и beats. Разбей каждый source beat
на 1–4 самостоятельных визуальных shot только при реальной смене действия, объекта,
состояния или визуального аргумента. По умолчанию оставь ОДИН shot на source beat.

СОХРАНЕНИЕ РЕЧИ:
- Для каждого source_beat_id склей spoken_text shots обычными пробелами.
  Результат должен точно совпадать с исходным source beat, включая пунктуацию и порядок.
- Каждый spoken_text — точный непрерывный фрагмент оригинальной речи.
  Никаких пересказов, пропусков, перестановок, исправлений и повторов.
- Не меняй scene_id/source_beat_id. shot_id: <source_beat_id>_s01, _s02 и т.д.
- Не назначай timestamps или длительности: их рассчитает система по фактическим WAV.

СМЫСЛОВЫЕ ПЕРЕХОДЫ:
- Деление допустимо, когда речь описывает несколько самостоятельных действий/состояний
  либо действительно меняется визуальный аргумент и каждому нужен другой кадр.
- Запятая, пауза, длина предложения, перечисление, риторический вопрос, желание динамики
  или предполагаемая длительность фразы сами по себе не причина для cut.
- Если один сильный визуальный образ может сопровождать всю мысль, оставь один shot.
- Для реального физического действия предпочитай stock/video; для абстрактной причинной
  связи — объясняющую локальную графику.

НАСЛЕДОВАНИЕ УТВЕРЖДЁННОГО ВИЗУАЛЬНОГО РЕШЕНИЯ:
- Для каждого shot сохрани visual_type, asset_strategy и provider_id исходного source beat
  без изменений. Shot-планировщик делит монтаж, но не переизобретает выбор источника.
- Назначенное фото остаётся photo/stock/pixabay; video/broll остаётся stock/pixabay;
  rendered остаётся rendered/internal_renderer.
- Не дроби одну фотографию на несколько кадров без реальной смены визуального аргумента.
- Не добавляй новые факты, цифры, надписи или утверждения. Не создавай фальшивые интерфейсы,
  документы, URL, даты, логотипы, цитаты, имена, названия кнопок или результаты.
- on_screen_text — короткая подтверждённая подпись, visual_description — только инструкция движку.
- cut_reason должен назвать конкретное изменение действия, объекта, состояния или аргумента.
  «Для динамики», «для разнообразия» и «новая фраза» недостаточны.
Верни ровно один SceneShotPlan для каждой входной сцены, без пропусков и дубликатов.
Все shots одной сцены находятся внутри её единственного объекта SceneShotPlan."""

REPAIR_SUFFIX = """
ПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН ВАЛИДАТОРОМ. Исправь только структуру ответа:
ровно один SceneShotPlan на каждую входную сцену, scene_id копируй дословно,
без дубликатов. Содержимое shots пересобери заново по правилам выше."""


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
    source_by_id = {f"b{i:02d}": beat for i, beat in enumerate(source_beats, 1)}
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
        source_beat = source_by_id[source_id]
        for index, row in enumerate(rows, 1):
            expected_shot_id = f"{source_id}_s{index:02d}"
            if row["shot_id"] != expected_shot_id:
                raise ValueError(
                    f"{scene_id}/{source_id}: expected {expected_shot_id}, got {row['shot_id']}"
                )
            for field in ("visual_type", "asset_strategy", "provider_id"):
                if row.get(field) != source_beat.get(field):
                    raise ValueError(
                        f"{scene_id}/{source_id}: shot {row['shot_id']} must preserve "
                        "visual_type/asset_strategy/provider_id from the approved source beat"
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


def _accept_batch(
    result: ShotPlanBatch,
    batch: list[dict[str, Any]],
    plan_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]] | None, str | None]:
    received = [item.scene_id for item in result.scenes]
    expected = [scene["id"] for scene in batch]
    if (len(received) != len(expected) or set(received) != set(expected)
            or len(set(received)) != len(received)):
        return None, f"wrong scene IDs: expected {expected}, got {received}"
    rows_by: dict[str, list[dict[str, Any]]] = {}
    for item in result.scenes:
        scene = next(s for s in batch if s["id"] == item.scene_id)
        rows = [shot.model_dump(mode="json") for shot in item.shots]
        try:
            rows_by[item.scene_id] = _validate_cached_scene(
                scene["id"], plan_by_id[scene["id"]]["beats"], rows)
        except ValueError as exc:
            return None, str(exc)
    return rows_by, None


def _fallback_shots(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Детерминированный план: один shot на beat. Покрытие речи точное по построению."""
    rows = []
    for i, beat in enumerate(plan["beats"], 1):
        source_id = f"b{i:02d}"
        rows.append(Shot.model_validate({
            **beat,
            "shot_id": f"{source_id}_s01",
            "source_beat_id": source_id,
            "cut_reason": "fallback: один shot на beat, LLM shot-план отклонён валидатором",
        }).model_dump(mode="json"))
    return rows


def plan_shots(
    settings: Any,
    scenes: list[dict[str, Any]],
    director_plans: list[dict[str, Any]],
    output_path: Path,
    batch_size: int = 2,
    journal: ProductionJournal | None = None,
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
            {"scenes": scenes, "plans": director_plans,
             "shot_policy_version": SHOT_POLICY_VERSION,
             "provider": [{"name": getattr(p, "name", None),
                           "model": getattr(p, "model", None),
                           "base_url": getattr(p, "base_url", None)}
                          for p in getattr(settings, "providers", [])]},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    progress_path = output_path.with_suffix(".progress.json")
    completed: dict[str, list[dict[str, Any]]] = {}
    fallback_scenes: list[str] = []
    if progress_path.is_file():
        checkpoint = json.loads(progress_path.read_text(encoding="utf-8-sig"))
        if checkpoint.get("source_sha256") != source_fingerprint:
            log.warning(
                "Shot planner checkpoint belongs to different input; "
                "discarding stale checkpoint and re-planning shots")
            checkpoint = {}
        cached = checkpoint.get("completed", {})
        if not isinstance(cached, dict):
            raise ValueError("Shot planner checkpoint has invalid completed section")
        unknown = set(cached) - set(scene_by_id)
        if unknown:
            raise ValueError(f"Shot planner checkpoint contains unknown scenes: {sorted(unknown)}")
        try:
            for scene_id, rows in cached.items():
                completed[scene_id] = _validate_cached_scene(
                    scene_id, plan_by_id[scene_id]["beats"], rows)
            fallback_scenes = list(checkpoint.get("fallback_scenes", []))
        except ValueError as exc:
            log.warning("Discarding shot-plan checkpoint that violates current speech/source inheritance: %s", exc)
            completed = {}
            fallback_scenes = []

    router = StructuredLLMRouter(settings, journal=journal)
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

        base_prompt = ("Составь монтажный shot-план для следующих сцен.\n"
                       + json.dumps(payload, ensure_ascii=False))
        rows_by: dict[str, list[dict[str, Any]]] | None = None
        error: str | None = None
        for attempt in range(2):
            task_id = (
                f"shot-plan:{','.join(s['id'] for s in batch)}:"
                f"attempt-{attempt + 1}"
            )
            result = router.complete(
                ShotPlanBatch,
                SYSTEM_PROMPT if attempt == 0 else SYSTEM_PROMPT + REPAIR_SUFFIX,
                base_prompt if attempt == 0 else
                ("ОШИБКА ВАЛИДАЦИИ: " + str(error) + "\n" + base_prompt),
                temperature=0.2 if attempt == 0 else 0.05,
                max_tokens=min(8500, 2200 + len(batch) * 2600),
                task_id=task_id,
                stage="shot_planning",
            )
            rows_by, error = _accept_batch(result, batch, plan_by_id)
            if rows_by is not None:
                break

            if journal is not None:
                journal.reject_task(
                    task_id,
                    error or "Shot planner semantic validation failed",
                )

            log.warning("Shot planner attempt %d rejected: %s", attempt + 1, error)

        if rows_by is None:
            ids = [s["id"] for s in batch]
            log.warning("Shot planner fallback (1 shot/beat) for scenes %s: %s", ids, error)
            rows_by = {s["id"]: _fallback_shots(plan_by_id[s["id"]]) for s in batch}
            fallback_scenes.extend(ids)

        completed.update(rows_by)
        _atomic_json(progress_path, {
            "source_sha256": source_fingerprint,
            "completed": completed,
            "completed_scene_count": len(completed),
            "fallback_scenes": sorted(set(fallback_scenes)),
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

    payload_out = {
        "schema_version": "2.0-shot-edl",
        "status": "validated",
        "source_sha256": source_fingerprint,
        "policy_version": SHOT_POLICY_VERSION,
        "scene_count": len(flattened),
        "source_beat_count": sum(len(plan["beats"]) for plan in director_plans),
        "shot_count": sum(len(plan["beats"]) for plan in flattened),
        "fallback_scenes": sorted(set(fallback_scenes)),
        "timing_policy": "measured_kokoro_wav_duration; no model-generated timestamps",
        "plans": flattened,
    }
    _atomic_json(output_path, payload_out)
    _atomic_json(progress_path, {
        "source_sha256": source_fingerprint,
        "completed": completed,
        "completed_scene_count": len(completed),
        "fallback_scenes": sorted(set(fallback_scenes)),
        "status": "complete",
    })
    return payload_out