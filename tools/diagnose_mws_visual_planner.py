#!/usr/bin/env python3
"""Diagnostic-only MWS/Gemma visual-planner test.

Uses the project's VisualPlanBatch model and SYSTEM_PROMPT. It does not edit
.env, storyboard, checkpoints, or production source files. Reports are written
under output/diagnostics.

Version 1.4 changes:
- Adds explicit semantic guidance for visual_type versus asset_strategy.
- Forbids invented names, dates, DOI identifiers, and real-looking URLs in mock visuals.
- Reaffirms that visual-beat duration ratios must sum to 1.0.
- Preserves the v1.3 schema-in-prompt and v1.2 stream diagnostics.
- Remains diagnostic-only; no production settings are changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kadr_farm.common import parse_json_response, read_json  # noqa: E402
from kadr_farm.config import load_settings  # noqa: E402
from kadr_farm.llm_structured import _schema_for_provider  # noqa: E402
from kadr_farm.models import VisualPlanBatch  # noqa: E402
from kadr_farm.visual_planner import SYSTEM_PROMPT  # noqa: E402

LOG = logging.getLogger("mws_gemma_visual_diagnostic")
GEMMA_THINK_TOKEN = "<|think|>"

OUTPUT_JSON_INSTRUCTION = """\n\nОБЯЗАТЕЛЬНЫЙ ФОРМАТ ОТВЕТА:
Верни ровно один ПОЛНЫЙ JSON-объект VisualPlanBatch. Если JSON Schema приведена
в этом сообщении или передана отдельно через response_format, строго соблюдай её.
Не переименовывай ключи и не заменяй обязательные поля похожими по смыслу.
Начни ответ с открывающей фигурной скобки. Не добавляй Markdown, пояснения,
варианты черновика или текст до/после JSON. Не обрывай объект: закрой все строки,
массивы и фигурные скобки. Используй компактное форматирование: не создавай
пустые строки и длинные последовательности пробелов. После закрывающей скобки
JSON ничего не добавляй. Сохраняй обязательные поля и типы. Не переписывай voice_text.

КРИТИЧЕСКОЕ РАЗЛИЧИЕ МЕЖДУ visual_type И asset_strategy:
- visual_type — ТОЛЬКО тип визуального содержания из enum JSON Schema. Не используй
  значения rendered, manual, stock или provider_id в visual_type.
- asset_strategy — способ получения ассета; он не заменяет visual_type.
- Для текстовой карточки, титра, подписи или UI-плашки используй visual_type=text.
- Для самостоятельной нетекстовой графики/рисунка используй visual_type=illustration
  или diagram/scheme — по реальному содержанию.
- Для реальной записи интерфейса/экрана используй visual_type=screencast.
- Для отрисованной текстовой карточки используй asset_strategy=rendered и
  provider_id=internal_renderer, но visual_type оставляй text.
- Для записи экрана вручную используй asset_strategy=manual,
  provider_id=manual_input и visual_type=screencast.
Не придумывай новые значения enum, даже если они кажутся логичными.

ДЛИТЕЛЬНОСТЬ VISUAL BEATS:
Если у сцены несколько visual_beats, сумма всех sub_duration_ratio должна быть
ровно 1.0 (с допустимой погрешностью округления). Распредели длительность сцены
между фрагментами, а не назначай каждому фрагменту 1.0.

ДОСТОВЕРНОСТЬ ДЕМОНСТРАЦИОННЫХ ДАННЫХ:
Не выдумывай реальные на вид имена авторов, точные даты публикаций, названия
статей, DOI, URL, статистику или источники. Если по сценарию нужно показать
пример интерфейса, используй явно условные заполнители: [ИМЯ АВТОРА], [ДАТА],
[ССЫЛКА] (в английском gen_prompt: [AUTHOR NAME], [PUBLICATION DATE], [LINK]).
Не создавай правдоподобные, но не проверенные библиографические сведения.
"""


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def openai_chat_endpoint(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def scene_inputs(storyboard_path: Path, scene_ids: list[str]) -> list[dict[str, Any]]:
    data = read_json(storyboard_path)
    scenes = {str(item.get("id")): item for item in data.get("scenes", [])}
    missing = [scene_id for scene_id in scene_ids if scene_id not in scenes]
    if missing:
        raise ValueError(f"Missing scenes in storyboard: {', '.join(missing)}")
    fields = ("id", "block_id", "voice_text", "meaning", "purpose")
    result: list[dict[str, Any]] = []
    for scene_id in scene_ids:
        scene = scenes[scene_id]
        absent = [field for field in fields if field not in scene]
        if absent:
            raise ValueError(f"Scene {scene_id} misses fields: {absent}")
        result.append({field: scene[field] for field in fields})
    return result


def visual_user_prompt(
    scenes: list[dict[str, Any]], *, include_json_instruction: bool = True,
    schema: dict[str, Any] | None = None, include_schema_in_prompt: bool = False,
) -> str:
    packed = [{
        "scene_id": scene["id"],
        "block_id": scene["block_id"],
        "voice_text": scene["voice_text"],
        "meaning": scene["meaning"],
        "purpose": scene["purpose"],
    } for scene in scenes]
    prompt = (
        "Создай VisualPlanBatch для следующих сцен. Не меняй narration.\n"
        "Учитывай типы визуалов уже назначенных предыдущим сценам, "
        "избегай одинаковых визуальных решений подряд, если это не оправдано смыслом.\n"
        "ПРЕДЫДУЩИЙ ПЛАН: []\n"
        f"СЦЕНЫ: {json.dumps(packed, ensure_ascii=False)}"
    )
    if include_schema_in_prompt:
        if schema is None:
            raise ValueError("schema is required when include_schema_in_prompt=True")
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = schema.get("required", []) if isinstance(schema, dict) else []
        property_names = list(properties) if isinstance(properties, dict) else []
        schema_header = [
            "\n\nТОЧНАЯ JSON SCHEMA ПРОЕКТА (обязательный контракт):",
            "Используй названия ключей и типы ровно как в схеме. Не придумывай "
            "альтернативные ключи верхнего уровня.",
        ]
        if property_names:
            schema_header.append("Допустимые ключи верхнего уровня: " + ", ".join(property_names) + ".")
        if required:
            schema_header.append("Обязательные ключи верхнего уровня: " + ", ".join(str(x) for x in required) + ".")
        compact_schema = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        schema_header.extend(("Схема в компактном JSON-виде:", compact_schema))
        prompt += "\n".join(schema_header)
    if include_json_instruction:
        prompt += OUTPUT_JSON_INSTRUCTION
    return prompt


def gemma_system_prompt(prompt: str, enable_thinking: bool = False) -> str:
    if enable_thinking and not prompt.lstrip().startswith(GEMMA_THINK_TOKEN):
        return GEMMA_THINK_TOKEN + "\n" + prompt
    return prompt


def response_format(
    schema: dict[str, Any], name: str, mode: str = "json_schema_strict"
) -> dict[str, Any] | None:
    if mode == "none":
        return None
    if mode == "json_object":
        return {"type": "json_object"}
    if mode != "json_schema_strict":
        raise ValueError(f"Unsupported response format mode: {mode}")
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:64] or "visual_plan_batch"
    return {
        "type": "json_schema",
        "json_schema": {
            "name": safe_name,
            "strict": True,
            "schema": schema,
        },
    }


def _extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(value)


def iter_sse_events(response: requests.Response) -> Iterable[dict[str, Any]]:
    """Yield parsed SSE objects. Decode event-stream bytes explicitly as UTF-8."""
    for raw_line in response.iter_lines(decode_unicode=False):
        if raw_line is None:
            continue
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="strict").strip()
        else:
            line = str(raw_line).strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            LOG.warning("Ignoring non-JSON SSE data line (%d chars)", len(payload))
            continue
        if isinstance(parsed, dict):
            yield parsed


def make_payload(
    *, model: str, system_prompt: str, user_prompt: str,
    schema: dict[str, Any], max_completion_tokens: int,
    temperature: float, reasoning_effort: str,
    include_think_token: bool,
    response_format_mode: str = "json_schema_strict",
) -> dict[str, Any]:
    effective_system = gemma_system_prompt(system_prompt, include_think_token)
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": effective_system},
            {"role": "user", "content": user_prompt},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_completion_tokens": max_completion_tokens,
        "temperature": temperature,
        "reasoning_effort": reasoning_effort,
    }
    fmt = response_format(schema, "visual_plan_batch", response_format_mode)
    if fmt is not None:
        payload["response_format"] = fmt
    return payload


def analyze_content(content: str) -> dict[str, Any]:
    """Summarize whitespace characteristics without dumping the response."""
    total = len(content)
    whitespace = sum(1 for char in content if char.isspace())
    non_whitespace = total - whitespace
    trailing = len(content) - len(content.rstrip())
    longest_run = max((len(match.group(0)) for match in re.finditer(r"\s+", content)), default=0)
    last_non_whitespace = max((i for i, char in enumerate(content) if not char.isspace()), default=-1)
    return {
        "total_chars": total,
        "whitespace_chars": whitespace,
        "non_whitespace_chars": non_whitespace,
        "whitespace_ratio": round(whitespace / total, 6) if total else 0.0,
        "trailing_whitespace_chars": trailing,
        "trailing_whitespace_ratio": round(trailing / total, 6) if total else 0.0,
        "longest_contiguous_whitespace_run_chars": longest_run,
        "last_non_whitespace_index": last_non_whitespace,
        "chars_after_last_non_whitespace": total - last_non_whitespace - 1 if last_non_whitespace >= 0 else total,
    }


class StreamAccumulator:
    def __init__(self) -> None:
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.usage: dict[str, Any] | None = None
        self.finish_reason: str | None = None
        self.response_model: str | None = None
        self.chunk_count = 0
        self.content_delta_count = 0
        self.whitespace_only_delta_count = 0
        self.whitespace_only_delta_chars = 0
        self.max_whitespace_only_delta_chars = 0
        self.empty_content_delta_count = 0
        self.last_event: dict[str, Any] | None = None

    @staticmethod
    def _delta_text(delta: dict[str, Any], keys: tuple[str, ...]) -> str:
        for key in keys:
            if key in delta and delta[key] is not None:
                text = _extract_text(delta[key])
                if text:
                    return text
        return ""

    def add(self, event: dict[str, Any]) -> None:
        self.last_event = event
        self.chunk_count += 1
        if isinstance(event.get("model"), str):
            self.response_model = event["model"]
        if isinstance(event.get("usage"), dict):
            self.usage = event["usage"]
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            content = self._delta_text(delta, ("content", "text"))
            reasoning = self._delta_text(
                delta, ("reasoning_content", "reasoning", "thinking", "analysis")
            )
            if content:
                self.content.append(content)
                self.content_delta_count += 1
                if content.isspace():
                    self.whitespace_only_delta_count += 1
                    self.whitespace_only_delta_chars += len(content)
                    self.max_whitespace_only_delta_chars = max(
                        self.max_whitespace_only_delta_chars, len(content)
                    )
            else:
                self.empty_content_delta_count += 1
            if reasoning:
                self.reasoning.append(reasoning)
            if choice.get("finish_reason") is not None:
                self.finish_reason = str(choice["finish_reason"])

    @property
    def content_text(self) -> str:
        return "".join(self.content)

    @property
    def reasoning_text(self) -> str:
        return "".join(self.reasoning)

    def content_stream_stats(self) -> dict[str, Any]:
        return {
            "content_delta_count": self.content_delta_count,
            "empty_content_delta_count": self.empty_content_delta_count,
            "whitespace_only_delta_count": self.whitespace_only_delta_count,
            "whitespace_only_delta_chars": self.whitespace_only_delta_chars,
            "max_whitespace_only_delta_chars": self.max_whitespace_only_delta_chars,
        }


def run_streaming_request(
    *, endpoint: str, api_key: str, payload: dict[str, Any],
    connect_timeout: float, progress_callback: Any,
) -> tuple[StreamAccumulator, int]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    accumulator = StreamAccumulator()
    with requests.Session() as session:
        with session.post(
            endpoint,
            headers=headers,
            json=payload,
            stream=True,
            timeout=(connect_timeout, None),
        ) as response:
            status_code = response.status_code
            if not response.ok:
                body = response.text[:5000]
                raise RuntimeError(f"MWS HTTP {status_code}: {body}")
            for event in iter_sse_events(response):
                accumulator.add(event)
                progress_callback(accumulator)
    return accumulator, status_code


def validate_visual(content: str, expected_scene_ids: list[str]) -> dict[str, Any]:
    try:
        parsed = parse_json_response(content)
    except Exception as exc:
        result: dict[str, Any] = {
            "status": "invalid_json",
            "validation_error": f"{type(exc).__name__}: {exc}",
        }
        if isinstance(exc, json.JSONDecodeError):
            result["json_error"] = {
                "line": exc.lineno,
                "column": exc.colno,
                "position": exc.pos,
                "remaining_chars_from_error": max(0, len(content) - exc.pos),
                "remaining_non_whitespace_chars_from_error": sum(
                    1 for char in content[exc.pos:] if not char.isspace()
                ),
            }
        return result
    try:
        validated = VisualPlanBatch.model_validate(parsed)
    except Exception as exc:
        return {"status": "pydantic_invalid", "validation_error": str(exc)[:10000]}
    actual_ids = [plan.scene_id for plan in validated.plans]
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected_scene_ids):
        return {
            "status": "wrong_scene_ids",
            "validation_error": {"expected": expected_scene_ids, "actual": actual_ids},
            "validated_plan": validated.model_dump(mode="json"),
        }
    return {"status": "pydantic_valid", "validated_plan": validated.model_dump(mode="json")}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose Gemma 4 31B visual planning via MWS Chat Completions."
    )
    parser.add_argument(
        "--storyboard",
        default="output/semantic_runs/first-semantic-e2e/storyboard_draft.json",
    )
    parser.add_argument("--scene-ids", default="s_001", help="Comma-separated scene IDs")
    parser.add_argument("--output-dir", default="output/diagnostics/mws-gemma-visual")
    parser.add_argument("--reasoning-effort", choices=("none", "medium"), default="medium")
    parser.add_argument("--max-completion-tokens", type=int, default=8192)
    parser.add_argument("--temperature", type=float, default=0.35)
    parser.add_argument("--connect-timeout", type=float, default=30.0)
    parser.add_argument(
        "--response-format",
        choices=("json_schema_strict", "json_object", "none"),
        default="json_schema_strict",
        help="Requested response format. MWS Chat Completions docs do not list this parameter; treat as experimental.",
    )
    parser.add_argument(
        "--no-explicit-json-instruction",
        action="store_true",
        help="Omit the additional JSON-only instruction (for controlled comparisons).",
    )
    parser.add_argument(
        "--schema-in-prompt",
        action="store_true",
        help="Embed the exact VisualPlanBatch JSON Schema in the user message (recommended for json_object tests).",
    )
    parser.add_argument(
        "--gemma-think-token",
        action="store_true",
        help="Prefix SYSTEM_PROMPT with Google's <|think|> control token (off by default).",
    )
    args = parser.parse_args()

    if args.max_completion_tokens < 1:
        parser.error("--max-completion-tokens должен быть больше 0")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    settings = load_settings()
    providers = {provider.name: provider for provider in settings.providers}
    primary = providers.get("primary")
    if primary is None:
        raise SystemExit("Не настроен LLM primary в .env. Не публикуй API-ключ.")

    storyboard = Path(args.storyboard)
    if not storyboard.is_absolute():
        storyboard = ROOT / storyboard
    if not storyboard.exists():
        raise SystemExit(f"Не найден storyboard: {storyboard}")
    scene_ids = [item.strip() for item in args.scene_ids.split(",") if item.strip()]
    if not scene_ids:
        raise SystemExit("Не указаны --scene-ids")
    scenes = scene_inputs(storyboard, scene_ids)
    schema = _schema_for_provider(VisualPlanBatch)
    base_user_prompt = visual_user_prompt(scenes, include_json_instruction=False)
    user_prompt = visual_user_prompt(
        scenes,
        include_json_instruction=not args.no_explicit_json_instruction,
        schema=schema,
        include_schema_in_prompt=args.schema_in_prompt,
    )
    schema_text = json.dumps(schema, sort_keys=True, ensure_ascii=False)
    schema_prompt_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":")) if args.schema_in_prompt else ""
    effective_system = gemma_system_prompt(SYSTEM_PROMPT, args.gemma_think_token)
    endpoint = openai_chat_endpoint(primary.base_url)

    out_dir = Path(args.output_dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "_", primary.model)
    stem = f"mws_gemma_{stamp}_{safe_model}"
    report_path = out_dir / f"{stem}.json"
    progress_path = out_dir / f"{stem}.progress.json"
    response_path = out_dir / f"{stem}.response.txt"
    reasoning_path = out_dir / f"{stem}.reasoning.txt"
    markdown_path = out_dir / f"{stem}.md"

    response_format_labels = {
        "json_schema_strict": "json_schema_strict",
        "json_object": "json_object",
        "none": "omitted",
    }
    report: dict[str, Any] = {
        "report_version": "1.4",
        "status": "in_progress",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "endpoint_mode": "mws_openai_compatible_chat_completions_streaming",
        "base_url_host": urlsplit(primary.base_url).hostname,
        "model": primary.model,
        "scene_ids": scene_ids,
        "storyboard": str(storyboard),
        "reasoning_effort_requested": args.reasoning_effort,
        "gemma_think_token_prefix_sent": args.gemma_think_token,
        "connect_timeout_seconds": args.connect_timeout,
        "read_timeout_seconds": None,
        "max_completion_tokens": args.max_completion_tokens,
        "temperature": args.temperature,
        "response_format_requested": response_format_labels[args.response_format],
        "explicit_json_instruction": not args.no_explicit_json_instruction,
        "schema_in_prompt_requested": args.schema_in_prompt,
        "schema_in_prompt_chars": len(schema_prompt_text),
        "schema_in_prompt_sha256": sha256_text(schema_prompt_text) if schema_prompt_text else None,
        "base_user_prompt_chars": len(base_user_prompt),
        "user_prompt_chars": len(user_prompt),
        "base_user_prompt_sha256": sha256_text(base_user_prompt),
        "user_prompt_sha256": sha256_text(user_prompt),
        "base_system_prompt_sha256": sha256_text(SYSTEM_PROMPT),
        "effective_system_prompt_sha256": sha256_text(effective_system),
        "schema_sha256": sha256_text(schema_text),
        "tests": {},
    }

    lock = threading.Lock()
    start = time.monotonic()
    acc = StreamAccumulator()
    last_log = [0.0]
    stop_heartbeat = threading.Event()

    def snapshot(current: StreamAccumulator, status: str = "in_progress") -> dict[str, Any]:
        content_text = current.content_text
        return {
            "report_version": "1.4-progress",
            "status": status,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "model": primary.model,
            "scene_ids": scene_ids,
            "reasoning_effort_requested": args.reasoning_effort,
            "gemma_think_token_prefix_sent": args.gemma_think_token,
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "content_chars": len(content_text),
            "reasoning_chars": len(current.reasoning_text),
            "content_whitespace_analysis": analyze_content(content_text),
            "content_stream_stats": current.content_stream_stats(),
            "chunk_count": current.chunk_count,
            "content": content_text,
            "reasoning": current.reasoning_text,
            "usage": current.usage,
            "finish_reason": current.finish_reason,
            "last_event_keys": sorted((current.last_event or {}).keys()),
        }

    def persist_progress(current: StreamAccumulator, status: str = "in_progress") -> None:
        with lock:
            atomic_write_json(progress_path, snapshot(current, status))
            response_path.write_text(current.content_text, encoding="utf-8")
            reasoning_path.write_text(current.reasoning_text, encoding="utf-8")

    def heartbeat() -> None:
        while not stop_heartbeat.wait(30):
            persist_progress(acc)
            LOG.info(
                "Gemma: %ss; reasoning=%s chars; final=%s chars; SSE chunks=%s",
                int(time.monotonic() - start), len(acc.reasoning_text),
                len(acc.content_text), acc.chunk_count,
            )

    def on_event(current: StreamAccumulator) -> None:
        nonlocal acc
        acc = current
        now = time.monotonic()
        if now - last_log[0] >= 10 or current.finish_reason:
            last_log[0] = now
            LOG.info(
                "Gemma: %ss; reasoning=%s chars; final=%s chars; SSE chunks=%s; whitespace-only deltas=%s",
                int(now - start), len(current.reasoning_text),
                len(current.content_text), current.chunk_count,
                current.whitespace_only_delta_count,
            )
            persist_progress(current)

    LOG.info("MWS model: %s", primary.model)
    LOG.info("Reasoning: %s; <|think|> prefix: %s", args.reasoning_effort, args.gemma_think_token)
    LOG.info("Visual scenes: %s", ", ".join(scene_ids))
    LOG.info("Response format requested: %s", response_format_labels[args.response_format])
    LOG.info("Explicit JSON instruction: %s", not args.no_explicit_json_instruction)
    LOG.info("Project JSON Schema embedded in prompt: %s", args.schema_in_prompt)
    LOG.info("Completion cap: %s; read timeout: unlimited", args.max_completion_tokens)
    atomic_write_json(report_path, report)
    persist_progress(acc)
    heartbeat_thread = threading.Thread(
        target=heartbeat, name="mws-gemma-heartbeat", daemon=True
    )
    heartbeat_thread.start()

    payload = make_payload(
        model=primary.model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        schema=schema,
        max_completion_tokens=args.max_completion_tokens,
        temperature=args.temperature,
        reasoning_effort=args.reasoning_effort,
        include_think_token=args.gemma_think_token,
        response_format_mode=args.response_format,
    )

    try:
        acc, status_code = run_streaming_request(
            endpoint=endpoint,
            api_key=primary.api_key,
            payload=payload,
            connect_timeout=args.connect_timeout,
            progress_callback=on_event,
        )
        stop_heartbeat.set()
        persist_progress(acc, "complete")
        response_path.write_text(acc.content_text, encoding="utf-8")
        reasoning_path.write_text(acc.reasoning_text, encoding="utf-8")
        validation = validate_visual(acc.content_text, scene_ids)
        content_analysis = analyze_content(acc.content_text)
        report["status"] = "complete"
        report["http_status"] = status_code
        report["elapsed_seconds"] = round(time.monotonic() - start, 2)
        report["response_model"] = acc.response_model
        report["stream_chunks"] = acc.chunk_count
        report["finish_reason"] = acc.finish_reason
        report["usage"] = acc.usage
        report["content_chars"] = len(acc.content_text)
        report["reasoning_chars"] = len(acc.reasoning_text)
        report["reasoning_returned_separately"] = bool(acc.reasoning_text)
        report["content_whitespace_analysis"] = content_analysis
        report["content_stream_stats"] = acc.content_stream_stats()
        report["response_file"] = str(response_path)
        report["reasoning_file"] = str(reasoning_path)
        report["progress_file"] = str(progress_path)
        report["validation"] = validation
        report["raw_response"] = acc.content_text[:100000]
        report["reasoning_preview"] = acc.reasoning_text[:10000]
        atomic_write_json(report_path, report)

        lines = [
            "# MWS / Gemma 4 visual planner diagnostic", "",
            f"UTC: {report['created_at_utc']}",
            f"Model: `{primary.model}`",
            f"Reasoning effort: `{args.reasoning_effort}`",
            f"Gemma `<|think|>` token sent: `{args.gemma_think_token}`",
            f"Response format requested: `{response_format_labels[args.response_format]}`",
            f"Explicit JSON instruction: `{not args.no_explicit_json_instruction}`",
            f"Project JSON Schema embedded in prompt: `{args.schema_in_prompt}`",
            "Read timeout: `unlimited`",
            f"Scenes: `{', '.join(scene_ids)}`", "",
            "| Check | Result |", "|---|---|",
            f"| HTTP | {status_code} |",
            f"| Finish reason | `{acc.finish_reason}` |",
            f"| Max completion tokens | {args.max_completion_tokens} (includes reasoning where reported by MWS) |",
            f"| Elapsed seconds | {report['elapsed_seconds']} |",
            f"| Reasoning chars (if exposed) | {len(acc.reasoning_text)} |",
            f"| Content chars | {len(acc.content_text)} |",
            f"| Whitespace chars | {content_analysis['whitespace_chars']} |",
            f"| Trailing whitespace chars | {content_analysis['trailing_whitespace_chars']} |",
            f"| Whitespace-only SSE deltas | {acc.whitespace_only_delta_count} |",
            f"| SSE chunks | {acc.chunk_count} |",
            f"| Pydantic | `{validation.get('status')}` |", "",
            "## Whitespace diagnostics", "",
            f"- Whitespace ratio: `{content_analysis['whitespace_ratio']}`",
            f"- Longest contiguous whitespace run: `{content_analysis['longest_contiguous_whitespace_run_chars']}` chars",
            f"- Trailing whitespace: `{content_analysis['trailing_whitespace_chars']}` chars",
            "", "## Files", "",
            f"- Response: `{response_path}`",
            f"- Reasoning metadata/text, if exposed: `{reasoning_path}`",
            f"- Progress: `{progress_path}`", "",
            "Note: This script records the response_format requested by the client; HTTP 200 alone does not prove that the provider/model enforced it.",
            "",
        ]
        markdown_path.write_text("\n".join(lines), encoding="utf-8")
        LOG.info(
            "Completed: validation=%s, finish_reason=%s, whitespace_ratio=%.4f, elapsed=%ss",
            validation.get("status"), acc.finish_reason,
            content_analysis["whitespace_ratio"], report["elapsed_seconds"],
        )
        print(
            f"JSON: {report_path}\nMarkdown: {markdown_path}\n"
            f"Response: {response_path}\nProgress: {progress_path}"
        )
        return 0 if validation.get("status") == "pydantic_valid" else 2

    except KeyboardInterrupt:
        stop_heartbeat.set()
        persist_progress(acc, "interrupted")
        report["status"] = "interrupted"
        report["elapsed_seconds"] = round(time.monotonic() - start, 2)
        report["content_chars"] = len(acc.content_text)
        report["reasoning_chars"] = len(acc.reasoning_text)
        report["content_whitespace_analysis"] = analyze_content(acc.content_text)
        report["content_stream_stats"] = acc.content_stream_stats()
        report["progress_file"] = str(progress_path)
        atomic_write_json(report_path, report)
        LOG.warning("Interrupted by user. Partial response/progress saved.")
        print(f"Partial progress: {progress_path}")
        return 130
    except Exception as exc:
        stop_heartbeat.set()
        persist_progress(acc, "error")
        report["status"] = "error"
        report["elapsed_seconds"] = round(time.monotonic() - start, 2)
        report["error"] = f"{type(exc).__name__}: {exc}"[:10000]
        report["content_chars"] = len(acc.content_text)
        report["reasoning_chars"] = len(acc.reasoning_text)
        report["content_whitespace_analysis"] = analyze_content(acc.content_text)
        report["content_stream_stats"] = acc.content_stream_stats()
        report["progress_file"] = str(progress_path)
        atomic_write_json(report_path, report)
        raise
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=1)


if __name__ == "__main__":
    raise SystemExit(main())
