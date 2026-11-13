"""Semantic scene segmentation: narration text only, no visual planning."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .common import read_json, write_json
from .llm_structured import StructuredLLMRouter
from .models import StoryboardResponse


SYSTEM_PROMPT = """Ты — редактор-сценарист русскоязычного технологического видеоканала.
Задача — разбить текст диктора на последовательность смысловых сцен.

КРИТИЧЕСКИЕ ПРАВИЛА:
1. Деление только по смыслу. Одна сцена — одна законченная мысль/пример/переход.
2. voice_text каждой сцены — дословный непрерывный фрагмент исходного текста.
3. Запрещено перефразировать, сокращать, исправлять орфографию, менять пунктуацию,
   удалять повторы или переносить текст из одного блока в другой.
4. Сцены должны в точности покрывать весь входной текст по порядку, без пробелов,
   повторов и пропусков. Различия в пробелах и переносах строк допустимы.
5. Длина сцен должна быть естественной для речи. Обычно несколько предложений;
   не дели одно предложение искусственно без необходимости.
6. Не назначай длительность и таймкоды. Их рассчитает Python после TTS.
7. На этом этапе НЕ придумывай визуалы, графики, поисковые запросы и промпты.
8. meaning кратко объясняет смысл фрагмента; purpose — зачем этот фрагмент нужен
   в повествовании.
Верни только JSON по заданной схеме."""


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def script_blocks(script: dict[str, Any]) -> list[dict[str, str]]:
    result = []
    seen = set()
    for i, block in enumerate(script.get("blocks", []), 1):
        block_id = str(block.get("id") or f"block_{i:02d}")
        if block_id in seen:
            raise ValueError(f"Повторяется ID исходного блока: {block_id}")
        seen.add(block_id)
        text = str(block.get("voice_text") or "")
        if not normalize_whitespace(text):
            raise ValueError(f"Пустой voice_text в блоке {block_id}")
        result.append({"id": block_id, "heading": str(block.get("heading") or ""),
                       "voice_text": text})
    if not result:
        raise ValueError("В сценарии нет блоков с voice_text")
    return result


def validate_block_coverage(source_text: str, segments: list[str], block_id: str = "block") -> None:
    source = normalize_whitespace(source_text)
    cursor = 0
    for index, raw in enumerate(segments, 1):
        segment = normalize_whitespace(raw)
        if not segment:
            raise ValueError(f"{block_id}: пустой фрагмент сцены {index}")
        while cursor < len(source) and source[cursor] == " ":
            cursor += 1
        if not source.startswith(segment, cursor):
            raise ValueError(
                f"{block_id}: фрагмент {index} не является следующим дословным фрагментом. "
                "Возможны пропуск, дублирование, перестановка или перефразирование."
            )
        cursor += len(segment)
    while cursor < len(source) and source[cursor] == " ":
        cursor += 1
    if cursor != len(source):
        raise ValueError(f"{block_id}: не покрыт конец voice_text ({len(source)-cursor} символов)")


def validate_storyboard_coverage(script: dict[str, Any], storyboard: dict[str, Any]) -> list[str]:
    blocks = script_blocks(script)
    scenes = storyboard.get("scenes", [])
    grouped: dict[str, list[str]] = {b["id"]: [] for b in blocks}
    order = {b["id"]: i for i, b in enumerate(blocks)}
    previous_block_index = -1
    for scene in scenes:
        bid = str(scene.get("block_id") or "")
        if bid not in grouped:
            raise ValueError(f"Раскадровка содержит неизвестный block_id: {bid}")
        current_index = order[bid]
        if current_index < previous_block_index:
            raise ValueError("Сцены разных блоков переставлены относительно исходного сценария")
        previous_block_index = current_index
        grouped[bid].append(str(scene.get("voice_text") or ""))
    for block in blocks:
        if not grouped[block["id"]]:
            raise ValueError(f"Блок {block['id']} полностью отсутствует в раскадровке")
        validate_block_coverage(block["voice_text"], grouped[block["id"]], block["id"])
    return []


def create_storyboard(settings: Any, script_path: Path, output_path: Path) -> dict[str, Any]:
    script = read_json(script_path)
    blocks = script_blocks(script)
    llm = StructuredLLMRouter(settings)
    scenes: list[dict[str, Any]] = []
    counter = 1
    for block in blocks:
        prompt = (
            f"Разбей текст ниже на смысловые сцены. block_id={block['id']}\n"
            "Верни JSON с массивом segments. Сохрани весь исходный текст дословно.\n"
            f"ИСХОДНЫЙ voice_text:\n{block['voice_text']}"
        )
        draft = llm.complete(StoryboardResponse, SYSTEM_PROMPT, prompt,
                             temperature=0.18, max_tokens=5000)
        segments = [x.voice_text for x in draft.segments]
        validate_block_coverage(block["voice_text"], segments, block["id"])
        for segment in draft.segments:
            scenes.append({
                "id": f"s_{counter:03d}",
                "block_id": block["id"],
                "voice_text": segment.voice_text,
                "meaning": segment.meaning,
                "purpose": segment.purpose,
            })
            counter += 1

    payload = {
        "schema_version": "1.0-draft",
        "title": str(script.get("title") or script.get("topic") or "Без названия"),
        "source_script_sha256": hashlib.sha256(
            json.dumps(script, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "scenes": scenes,
        "visual_planning_status": "not_started",
    }
    validate_storyboard_coverage(script, payload)
    write_json(output_path, payload)
    return payload
