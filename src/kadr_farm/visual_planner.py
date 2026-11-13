"""Adds visual direction to semantically verified narration scenes."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from copy import copy
from pathlib import Path
from typing import Any

from .common import read_json, write_json
from .llm_structured import StructuredLLMRouter
from .models import VisualPlanBatch, VisualPlanDraft

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — режиссёр монтажа и визуальный редактор технологического YouTube-ролика.
Получаешь уже размеченные дословные фрагменты речи. Не переписывай voice_text и не меняй
порядок сцен. Для каждой сцены придумай конкретный визуальный способ усилить именно эту
мысль. Не используй одинаковые карточки подряд: чередуй реальные фотографии, короткий
B-roll, собственные схемы, сравнения, инфографику, кинетический текст и уместные шутки.

РАЗРЕШЁННЫЕ СТРАТЕГИИ ПЕРВОЙ ВЕРСИИ:
- stock: искать только через Pixabay (provider_id="pixabay"), без Pexels;
- rendered: оригинальная графика, схема, типографика и диаграммы силами локального renderer
  (provider_id="internal_renderer");
- manual или screencast: только если требуется материал оператора
  (provider_id="manual_input").
Не выдумывай другие provider_id: текущий реестр закрыт этими тремя адаптерами.
Не выбирай generated: платные image/video generation API пока не подключены.
Промпты gen_prompt — на английском, конкретно описывают композицию и объекты.
Для chart обязательно создай chart_data с реальными labels, values, unit, source_url,
methodology. Если не располагаешь проверенными данными — НЕ выбирай chart.
Фактические утверждения перечисляй в claims с источниками-кандидатами. Не утверждай,
что факты проверены: Python сохранит статус verify_before_publish.
Шутка должна быть отмечена humor_note и не подменять фактическое утверждение.
Экранные надписи короткие, до 40 символов каждая.
speaker_emotion, pacing, camera_move — режиссёрские заметки, не гарантии Kokoro.
music_cue только описывает пожелание; музыка автоматически не скачивается.
Для sound effects перечисляй только name и purpose, не придумывай URL и лицензию.
В visual_beats добавляй несколько планов только когда нужна смена визуала при непрерывной
речи. sub_duration_ratio — положительный относительный вес длительности каждого плана;
можно указывать 1.0 для каждого равного плана. Python автоматически нормализует веса,
чтобы их сумма стала 1.0. В остальных случаях верни [].
Верни планы для всех указанных scene_id, каждый ровно один раз."""


def _storyboard_fingerprint(storyboard: dict[str, Any]) -> str:
    encoded = json.dumps(
        storyboard, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _checkpoint_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.progress.json")


def _empty_checkpoint(fingerprint: str, batch_size: int) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "source_fingerprint": fingerprint,
        "batch_size": batch_size,
        "batch_sizes_used": [batch_size],
        "status": "in_progress",
        "completed": {},
        "last_error": None,
    }


def _load_checkpoint(path: Path, fingerprint: str, batch_size: int,
                     expected_ids: set[str]) -> dict[str, Any]:
    if not path.exists():
        return _empty_checkpoint(fingerprint, batch_size)
    checkpoint = read_json(path)
    if checkpoint.get("source_fingerprint") != fingerprint:
        raise ValueError(
            f"Найден checkpoint от другой версии раскадровки: {path}. "
            "Не удаляй его вслепую: укажи новый --out для изменённой раскадровки "
            "или сохрани старый файл отдельно."
        )
    completed = checkpoint.get("completed")
    if not isinstance(completed, dict):
        raise ValueError(f"Повреждён checkpoint визуального планирования: {path}")
    unexpected = set(completed) - expected_ids
    if unexpected:
        raise ValueError(
            f"Checkpoint содержит неизвестные scene_id: {sorted(unexpected)}"
        )
    # Re-validate checkpoint records; never trust cached JSON without the same contract.
    for scene_id, plan in completed.items():
        if not isinstance(plan, dict):
            raise ValueError(f"Checkpoint {path}: план {scene_id} должен быть объектом")
        validated_plan = VisualPlanDraft.model_validate({"scene_id": scene_id, **plan})
        normalized_plan = validated_plan.model_dump(mode="json")
        normalized_plan.pop("scene_id", None)
        completed[scene_id] = normalized_plan
    history = checkpoint.get("batch_sizes_used")
    if not isinstance(history, list) or not history:
        history = [int(checkpoint.get("batch_size", batch_size))]
    if int(history[-1]) != batch_size:
        history.append(batch_size)
    checkpoint["batch_sizes_used"] = history
    checkpoint["batch_size"] = batch_size
    checkpoint["status"] = "in_progress"
    checkpoint["last_error"] = None
    return checkpoint


def _visual_planner_settings(settings: Any) -> Any:
    """Apply a visual-planner-only provider policy without changing other stages.

    SEMANTIC_VISUAL_PLANNER_PROVIDER accepts:
      - auto (default): configured provider order, normally MWS -> Ollama;
      - primary: only the configured primary provider;
      - secondary: only the configured secondary provider.

    This is useful when a provider handles storyboard segmentation well but
    consistently times out on the more expensive visual-direction task.
    """
    policy = os.getenv("SEMANTIC_VISUAL_PLANNER_PROVIDER", "auto").strip().lower()
    if policy not in {"auto", "primary", "secondary"}:
        raise ValueError(
            "SEMANTIC_VISUAL_PLANNER_PROVIDER должен быть auto, primary или secondary; "
            f"получено: {policy!r}"
        )
    if policy == "auto":
        log.info("Visual planner provider policy: auto (configured fallback order)")
        return settings

    providers = list(getattr(settings, "providers", []))
    selected = [p for p in providers if str(getattr(p, "name", "")).lower() == policy]
    if not selected:
        raise ValueError(
            f"Для visual planner выбран провайдер {policy!r}, но он не настроен в .env. "
            "Проверь соответствующие LLM_PRIMARY_* или LLM_SECONDARY_* параметры."
        )

    # Settings is a dataclass in production. A shallow copy avoids mutating the
    # shared settings object used by storyboard and any later pipeline stages.
    planner_settings = copy(settings)
    planner_settings.providers = selected
    log.info(
        "Visual planner provider policy: %s only (%s)",
        policy, ", ".join(str(getattr(p, "model", "unknown")) for p in selected),
    )
    return planner_settings


def plan_visuals(settings: Any, storyboard_path: Path, output_path: Path,
                 batch_size: int = 2) -> dict[str, Any]:
    """Plan visuals in small resumable batches, checkpointing each accepted batch."""
    if not 1 <= int(batch_size) <= 4:
        raise ValueError("batch_size должен быть в диапазоне 1..4; рекомендуемое значение — 2")
    batch_size = int(batch_size)
    storyboard = read_json(storyboard_path)
    scenes = storyboard.get("scenes", [])
    if not scenes:
        raise ValueError("Раскадровка пуста; сначала выполните storyboard")
    scene_ids = [str(scene.get("id") or "") for scene in scenes]
    if any(not scene_id for scene_id in scene_ids):
        raise ValueError("Каждая сцена должна иметь непустой id")
    if len(scene_ids) != len(set(scene_ids)):
        raise ValueError("В раскадровке повторяются scene_id")

    fingerprint = _storyboard_fingerprint(storyboard)
    checkpoint_path = _checkpoint_path(output_path)
    checkpoint = _load_checkpoint(
        checkpoint_path, fingerprint, batch_size, set(scene_ids)
    )
    completed: dict[str, dict[str, Any]] = checkpoint["completed"]
    write_json(checkpoint_path, checkpoint)
    pending = [scene for scene in scenes if scene["id"] not in completed]
    if pending:
        llm = StructuredLLMRouter(_visual_planner_settings(settings))
        total_batches = (len(pending) + batch_size - 1) // batch_size
        for batch_number, offset in enumerate(range(0, len(pending), batch_size), 1):
            batch = pending[offset:offset + batch_size]
            scene_payload = [{
                "scene_id": scene["id"], "block_id": scene["block_id"],
                "voice_text": scene["voice_text"], "meaning": scene["meaning"],
                "purpose": scene["purpose"],
            } for scene in batch]
            current_index = scenes.index(batch[0])
            previous_summary = []
            for previous in scenes[max(0, current_index - 8):current_index]:
                previous_plan = completed.get(previous["id"])
                if previous_plan:
                    previous_summary.append({
                        "scene_id": previous["id"],
                        "visual_type": previous_plan.get("visual_type"),
                        "asset_strategy": previous_plan.get("asset_strategy"),
                    })
            prompt = (
                "Создай VisualPlanBatch для следующих сцен. Не меняй narration.\n"
                "Учитывай типы визуалов уже назначенных предыдущим сценам, "
                "избегай одинаковых визуальных решений подряд, если это не оправдано смыслом.\n"
                f"ПРЕДЫДУЩИЙ ПЛАН: {json.dumps(previous_summary, ensure_ascii=False)}\n"
                f"СЦЕНЫ: {json.dumps(scene_payload, ensure_ascii=False)}"
            )
            expected = {scene["id"] for scene in batch}
            try:
                log.info(
                    "Visual planning batch %s/%s: %s", batch_number, total_batches,
                    ", ".join(sorted(expected)),
                )
                plans = llm.complete(
                    VisualPlanBatch, SYSTEM_PROMPT, prompt,
                    temperature=0.35, max_tokens=5000 if batch_size <= 2 else 7000,
                ).plans
                received = [plan.scene_id for plan in plans]
                if len(received) != len(set(received)) or set(received) != expected:
                    raise ValueError(
                        f"Visual planner вернул неверный набор сцен: ожидается "
                        f"{sorted(expected)}, получено {received}"
                    )
                for plan in plans:
                    plan_data = plan.model_dump(mode="json")
                    plan_data.pop("scene_id", None)
                    completed[plan.scene_id] = plan_data
                checkpoint["completed"] = completed
                checkpoint["status"] = "in_progress"
                checkpoint["last_error"] = None
                write_json(checkpoint_path, checkpoint)
                log.info(
                    "Visual planning checkpoint saved: %s/%s scenes",
                    len(completed), len(scenes),
                )
            except Exception as exc:
                checkpoint["completed"] = completed
                checkpoint["status"] = "incomplete"
                checkpoint["last_error"] = f"{type(exc).__name__}: {exc}"[:4000]
                write_json(checkpoint_path, checkpoint)
                raise

    for scene in scenes:
        scene.update(completed[scene["id"]])
    storyboard["schema_version"] = "1.0"
    storyboard["visual_planning_status"] = "complete"
    storyboard["visual_planning"] = {
        "status": "complete",
        "scene_count": len(scenes),
        "batch_size": batch_size,
        "batch_size_history": checkpoint.get("batch_sizes_used", [batch_size]),
        "checkpoint": str(checkpoint_path.resolve()),
    }
    write_json(output_path, storyboard)
    checkpoint["status"] = "complete"
    checkpoint["last_error"] = None
    write_json(checkpoint_path, checkpoint)
    return storyboard
