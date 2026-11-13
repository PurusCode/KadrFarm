from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

from .config import Settings
from .common import ensure_dir, read_json
from .text_normalization import normalize_for_speech

log = logging.getLogger(__name__)


def synthesize(
    settings: Settings,
    script_path: Path,
    out_dir: Path,
    *,
    speed: float | None = None,
) -> dict[str, Any]:
    """Synthesize every script block with the selected production engine.

    Kokoro is the only supported production engine. There is deliberately no
    fallback to Piper: a missing model/runtime must fail loudly.
    """
    script = read_json(script_path)
    blocks = script.get("blocks", [])
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("В сценарии нет блоков")

    selected_speed = settings.tts_speed if speed is None else speed
    if not math.isfinite(float(selected_speed)) or not 0.5 <= float(selected_speed) <= 2.0:
        raise ValueError("Скорость TTS должна быть в диапазоне от 0.5 до 2.0.")
    selected_speed = float(selected_speed)

    engine = str(settings.tts_engine or "").strip().lower()
    if engine != "kokoro":
        raise RuntimeError(
            f"Неподдерживаемый TTS_ENGINE={settings.tts_engine!r}. "
            "Production-движок проекта — Kokoro-ru v2 Dima. Автоматического fallback на Piper нет."
        )

    voice = str(settings.tts_speaker or "").strip().lower()
    if voice != "dima":
        raise RuntimeError(
            f"Неподдерживаемый TTS_SPEAKER={settings.tts_speaker!r}. "
            "В production зафиксирован проверенный голос Kokoro Dima."
        )

    prepared: list[tuple[str, dict[str, Any], str, str]] = []
    seen_ids: set[str] = set()
    for index, block in enumerate(blocks, start=1):
        if not isinstance(block, dict):
            raise ValueError(f"Блок сценария №{index} должен быть JSON-объектом.")
        block_id = str(block.get("id") or f"block_{index}")
        if block_id in seen_ids:
            raise ValueError(f"Повторяется ID блока сценария: {block_id!r}.")
        seen_ids.add(block_id)
        source_text = str(block.get("voice_text", "")).strip()
        normalized_text = normalize_for_speech(source_text)
        if not normalized_text:
            raise ValueError(f"Пустой voice_text в блоке {block_id!r}; каждый блок должен иметь озвучку.")
        prepared.append((block_id, block, source_text, normalized_text))

    out_dir = ensure_dir(out_dir)
    from .kokoro_engine import synthesize_kokoro

    manifest = synthesize_kokoro(settings, prepared, out_dir, selected_speed)
    if manifest.get("status") != "complete":
        raise RuntimeError("Kokoro вернул manifest без статуса complete.")
    if manifest.get("blocks_expected") != len(prepared):
        raise RuntimeError("Количество блоков в Kokoro manifest не совпадает со сценарием.")
    actual_ids = [str(row.get("id", "")) for row in manifest.get("blocks", [])]
    expected_ids = [item[0] for item in prepared]
    if actual_ids != expected_ids:
        raise RuntimeError("Kokoro manifest не соответствует порядку блоков сценария.")
    return manifest
