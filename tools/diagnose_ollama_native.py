#!/usr/bin/env python3
"""Native Ollama visual-planner diagnostic with thinking and streaming.

For canonical qwen3.5:* tags, --think auto explicitly sends think=true. For
other models auto omits the field and preserves the model's own default.
The visual request has no HTTP timeout by default. Streaming progress and final
thinking/answer artifacts are saved under output/diagnostics.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kadr_farm.common import parse_json_response, read_json, write_json  # noqa: E402
from kadr_farm.config import load_settings  # noqa: E402
from kadr_farm.llm_structured import _schema_for_provider  # noqa: E402
from kadr_farm.models import VisualPlanBatch  # noqa: E402
from kadr_farm.visual_planner import SYSTEM_PROMPT  # noqa: E402

LOG = logging.getLogger("ollama_native_diagnostic")
PING_SYSTEM = "Ты выполняешь короткую техническую проверку связи."
PING_USER = "Ответь ровно одним словом: OK"
TINY_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["ok"]}, "echo": {"type": "string"}},
    "required": ["status", "echo"],
    "additionalProperties": False,
}
ProgressCallback = Callable[[dict[str, Any]], None]


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def native_base_url(openai_base_url: str) -> str:
    parsed = urlsplit(openai_base_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def resolve_think_setting(model_tag: str, requested: str) -> bool | str | None:
    """Resolve the native Ollama think field without silently lowering effort.

    Qwen3.8 advertises [false, low, medium, xhigh] with medium as its default;
    auto therefore selects the exact strongest advertised value, xhigh.
    Qwen3.5 auto sends True because its tags expose a boolean thinking switch.
    """
    if requested == "true":
        return True
    if requested == "false":
        return False
    if requested == "default":
        return None
    if requested in {"low", "medium", "xhigh"}:
        return requested
    if requested != "auto":
        raise ValueError(f"Unsupported thinking mode: {requested}")
    normalized = model_tag.lower().strip()
    if normalized.startswith("qwen3.8:") or normalized == "qwen3.8":
        return "xhigh"
    if normalized.startswith("qwen3.5:"):
        return True
    return None


def describe_think_setting(value: bool | str | None) -> str:
    if value is None:
        return "model_default (think field omitted)"
    return str(value).lower() if isinstance(value, bool) else value


def parse_timeout(value: str) -> float | None:
    if value.strip().lower() in {"none", "unlimited", "off", "no"}:
        return None
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use seconds or 'none'") from exc
    if seconds <= 0:
        raise argparse.ArgumentTypeError("Timeout must be positive or 'none'")
    return seconds


def build_native_payload(
    model: str,
    messages: list[dict[str, str]],
    *,
    num_ctx: int,
    num_predict: int,
    temperature: float = 0.0,
    format_schema: dict[str, Any] | None = None,
    think: bool | str | None = None,
    keep_alive: str = "30m",
) -> dict[str, Any]:
    if num_ctx < 1024:
        raise ValueError("num_ctx must be >= 1024")
    if num_predict < 1:
        raise ValueError("num_predict must be >= 1")
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "keep_alive": keep_alive,
        "options": {
            "num_ctx": int(num_ctx),
            "num_predict": int(num_predict),
            "temperature": float(temperature),
        },
    }
    if format_schema is not None:
        payload["format"] = format_schema
    # None means omit this field; it does not mean think=false.
    if think is not None:
        payload["think"] = think
    return payload


def scene_inputs(storyboard_path: Path, scene_ids: list[str]) -> list[dict[str, Any]]:
    data = read_json(storyboard_path)
    scenes = {str(item.get("id")): item for item in data.get("scenes", [])}
    missing = [sid for sid in scene_ids if sid not in scenes]
    if missing:
        raise ValueError(f"Missing scenes in storyboard: {', '.join(missing)}")
    fields = ("id", "block_id", "voice_text", "meaning", "purpose")
    result = []
    for sid in scene_ids:
        scene = scenes[sid]
        absent = [key for key in fields if key not in scene]
        if absent:
            raise ValueError(f"Scene {sid} misses fields: {absent}")
        result.append({key: scene[key] for key in fields})
    return result


def visual_user_prompt(scenes: list[dict[str, Any]]) -> str:
    packed = [{
        "scene_id": scene["id"], "block_id": scene["block_id"],
        "voice_text": scene["voice_text"], "meaning": scene["meaning"],
        "purpose": scene["purpose"],
    } for scene in scenes]
    return (
        "Создай VisualPlanBatch для следующих сцен. Не меняй narration.\n"
        "Учитывай типы визуалов уже назначенных предыдущим сценам, "
        "избегай одинаковых визуальных решений подряд, если это не оправдано смыслом.\n"
        "ПРЕДЫДУЩИЙ ПЛАН: []\n"
        f"СЦЕНЫ: {json.dumps(packed, ensure_ascii=False)}"
    )


def response_metrics(data: dict[str, Any]) -> dict[str, Any]:
    def seconds(key: str) -> float | None:
        value = data.get(key)
        return round(float(value) / 1_000_000_000, 3) if isinstance(value, (int, float)) else None
    return {
        "done": data.get("done"), "done_reason": data.get("done_reason"),
        "prompt_eval_count": data.get("prompt_eval_count"), "eval_count": data.get("eval_count"),
        "total_duration_seconds": seconds("total_duration"),
        "load_duration_seconds": seconds("load_duration"),
        "prompt_eval_duration_seconds": seconds("prompt_eval_duration"),
        "eval_duration_seconds": seconds("eval_duration"),
    }


def call_ollama(
    session: requests.Session,
    endpoint: str,
    payload: dict[str, Any],
    timeout: float | None,
    *,
    progress_callback: ProgressCallback | None = None,
    progress_interval: float = 15.0,
) -> dict[str, Any]:
    """Collect Ollama's NDJSON /api/chat stream. timeout=None means unlimited."""
    request_payload = dict(payload)
    request_payload["stream"] = True
    started = time.monotonic()
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    last_event: dict[str, Any] = {}
    last_progress = started
    response = session.post(endpoint, json=request_payload, timeout=timeout, stream=True)

    def snapshot() -> dict[str, Any]:
        return {
            "content": "".join(content_parts),
            "thinking": "".join(thinking_parts),
            "last_event": last_event,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }

    def notify(force: bool = False) -> None:
        nonlocal last_progress
        now = time.monotonic()
        if progress_callback and (force or now - last_progress >= progress_interval):
            progress_callback(snapshot())
            last_progress = now

    try:
        response.raise_for_status()
        for raw_line in response.iter_lines(decode_unicode=False):
            if not raw_line:
                continue
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else str(raw_line)
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]":
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                continue
            last_event.update({key: val for key, val in event.items() if key != "message"})
            message = event.get("message") or {}
            if message.get("role"):
                last_event["_message_role"] = message["role"]
            if message.get("content"):
                content_parts.append(str(message["content"]))
            if message.get("thinking"):
                thinking_parts.append(str(message["thinking"]))
            notify()
            if event.get("done") is True:
                break
        notify(force=True)
        if last_event.get("done") is not True:
            raise RuntimeError("Ollama stream ended before a final done=true event")
        final = dict(last_event)
        role = final.pop("_message_role", "assistant")
        final["message"] = {
            "role": role,
            "content": "".join(content_parts),
            "thinking": "".join(thinking_parts),
        }
        return final
    except BaseException:
        if progress_callback:
            try:
                state = snapshot()
                state["interrupted_or_failed"] = True
                progress_callback(state)
            except Exception:
                LOG.exception("Could not save partial Ollama stream")
        raise
    finally:
        response.close()


def get_message_text(data: dict[str, Any]) -> tuple[str, str]:
    message = data.get("message") or {}
    return str(message.get("content") or ""), str(message.get("thinking") or "")


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    return slug or "model"


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def run_model(
    *,
    model: str,
    base_url: str,
    storyboard: Path,
    num_ctx: int,
    max_tokens: int,
    warmup_timeout: float | None,
    visual_timeout: float | None,
    scene_ids: list[str],
    think_setting: bool | str | None,
    progress_path: Path,
    artifact_prefix: Path,
    progress_interval: float,
) -> dict[str, Any]:
    endpoint = f"{base_url}/api/chat"
    session = requests.Session()
    report: dict[str, Any] = {
        "model": model,
        "api_endpoint": endpoint,
        "num_ctx_requested": num_ctx,
        "num_predict_visual": max_tokens,
        "think_setting": describe_think_setting(think_setting),
        "think_field_sent": think_setting is not None,
        "think_false_sent": think_setting is False,
        "visual_timeout_seconds": visual_timeout,
        "visual_timeout_unlimited": visual_timeout is None,
        "tests": {},
    }

    # Warm-up: use the same thinking setting as the main test; reserve enough
    # generation budget for a short answer after the reasoning preamble.
    ping_payload = build_native_payload(
        model,
        [
            {"role": "system", "content": PING_SYSTEM},
            {"role": "user", "content": PING_USER},
        ],
        num_ctx=num_ctx,
        num_predict=512,
        temperature=0,
        think=think_setting,
    )
    started = time.monotonic()
    try:
        data = call_ollama(session, endpoint, ping_payload, warmup_timeout)
        content, thinking = get_message_text(data)
        report["tests"]["plain_ping"] = {
            "status": "ok" if content.strip() else "empty_content",
            "content": content[:500],
            "thinking_chars": len(thinking),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            **response_metrics(data),
        }
    except Exception as exc:
        report["tests"]["plain_ping"] = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}"[:2000],
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
        report["tests"]["tiny_json_schema"] = {"status": "skipped", "reason": "warmup failed"}
        report["tests"]["visual_single"] = {"status": "skipped", "reason": "warmup failed"}
        return report

    schema_payload = build_native_payload(
        model,
        [
            {"role": "system", "content": "Верни JSON строго по заданной схеме."},
            {"role": "user", "content": 'Верни status="ok" и echo="schema works".'},
        ],
        num_ctx=num_ctx,
        num_predict=512,
        temperature=0,
        format_schema=TINY_SCHEMA,
        think=think_setting,
    )
    started = time.monotonic()
    try:
        data = call_ollama(session, endpoint, schema_payload, warmup_timeout)
        content, thinking = get_message_text(data)
        try:
            parsed = parse_json_response(content)
            valid = isinstance(parsed, dict) and parsed == {"status": "ok", "echo": "schema works"}
            status = "ok" if valid else "invalid_schema_response"
            parse_error = None
        except Exception as exc:
            parsed = None
            status = "invalid_json"
            parse_error = f"{type(exc).__name__}: {exc}"
        result = {
            "status": status,
            "raw_response": content[:4000],
            "parsed_response": parsed,
            "thinking_chars": len(thinking),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            **response_metrics(data),
        }
        if parse_error:
            result["validation_error"] = parse_error
        report["tests"]["tiny_json_schema"] = result
    except Exception as exc:
        report["tests"]["tiny_json_schema"] = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}"[:2000],
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }

    scenes = scene_inputs(storyboard, scene_ids)
    user_prompt = visual_user_prompt(scenes)
    schema = _schema_for_provider(VisualPlanBatch)
    visual_payload = build_native_payload(
        model,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        num_ctx=num_ctx,
        num_predict=max_tokens,
        temperature=0.35,
        format_schema=schema,
        think=think_setting,
    )
    started = time.monotonic()
    visual: dict[str, Any] = {
        "scene_ids": scene_ids,
        "prompt_chars": len(user_prompt),
        "prompt_sha256": sha256_text(user_prompt),
        "system_prompt_sha256": sha256_text(SYSTEM_PROMPT),
        "schema_sha256": sha256_text(json.dumps(schema, sort_keys=True, ensure_ascii=False)),
        "format": "native_json_schema",
        "num_ctx_requested": num_ctx,
        "num_predict_requested": max_tokens,
        "think_setting": describe_think_setting(think_setting),
        "think_field_sent": think_setting is not None,
        "timeout_seconds": visual_timeout,
        "timeout_unlimited": visual_timeout is None,
        "streaming": True,
        "progress_file": str(progress_path),
    }
    last_log_at = 0.0

    def save_progress(snapshot: dict[str, Any]) -> None:
        nonlocal last_log_at
        elapsed = float(snapshot.get("elapsed_seconds") or 0.0)
        content = str(snapshot.get("content") or "")
        thinking = str(snapshot.get("thinking") or "")
        state = {
            "report_version": "1.1-progress",
            "status": "in_progress",
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "scene_ids": scene_ids,
            "thinking_setting": describe_think_setting(think_setting),
            "elapsed_seconds": elapsed,
            "thinking_chars": len(thinking),
            "content_chars": len(content),
            "thinking": thinking,
            "content": content,
            "metrics": response_metrics(snapshot.get("last_event") or {}),
            "interrupted_or_failed": bool(snapshot.get("interrupted_or_failed", False)),
        }
        write_json(progress_path, state)
        if elapsed - last_log_at >= 30:
            LOG.info(
                "%s: %.0fs; thinking=%d chars; final answer=%d chars",
                model, elapsed, len(thinking), len(content),
            )
            last_log_at = elapsed

    try:
        data = call_ollama(
            session, endpoint, visual_payload, visual_timeout,
            progress_callback=save_progress,
            progress_interval=progress_interval,
        )
        content, thinking = get_message_text(data)
        response_file = artifact_prefix.with_suffix(".response.txt")
        thinking_file = artifact_prefix.with_suffix(".thinking.txt")
        write_text(response_file, content)
        write_text(thinking_file, thinking)
        visual["raw_response"] = content[:50000]
        visual["response_file"] = str(response_file)
        visual["thinking_file"] = str(thinking_file)
        visual["thinking_chars"] = len(thinking)
        visual["thinking_present"] = bool(thinking.strip())
        visual.update(response_metrics(data))

        try:
            parsed = parse_json_response(content)
        except Exception as exc:
            visual["status"] = "invalid_json"
            visual["validation_error"] = f"{type(exc).__name__}: {exc}"
        else:
            try:
                validated = VisualPlanBatch.model_validate(parsed)
                actual_ids = [plan.scene_id for plan in validated.plans]
                if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(scene_ids):
                    visual["status"] = "wrong_scene_ids"
                    visual["validation_error"] = {"expected": scene_ids, "actual": actual_ids}
                else:
                    visual["status"] = "pydantic_valid"
                visual["validated_plan"] = validated.model_dump(mode="json")
            except Exception as exc:
                visual["status"] = "pydantic_invalid"
                visual["validation_error"] = str(exc)[:8000]

        save_progress({
            "content": content,
            "thinking": thinking,
            "last_event": data,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        })
        progress_state = read_json(progress_path)
        progress_state["status"] = "complete"
        progress_state["final_status"] = visual["status"]
        write_json(progress_path, progress_state)
    except Exception as exc:
        visual["status"] = "error"
        visual["error"] = f"{type(exc).__name__}: {exc}"[:2500]
        if progress_path.exists():
            visual["partial_progress_file"] = str(progress_path)
        raise
    finally:
        visual["elapsed_seconds"] = round(time.monotonic() - started, 2)

    report["tests"]["visual_single"] = visual
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare local Ollama models via native streaming /api/chat.")
    parser.add_argument("--storyboard", default="output/semantic_runs/first-semantic-e2e/storyboard_draft.json")
    parser.add_argument("--models", required=True, help="Comma-separated Ollama model tags")
    parser.add_argument("--ollama-num-ctx", type=int, default=16384)
    parser.add_argument("--visual-max-tokens", type=int, default=12000)
    parser.add_argument("--warmup-timeout", type=parse_timeout, default=600.0,
                        help="Warm-up timeout in seconds; use 'none' for unlimited")
    parser.add_argument("--visual-timeout", type=parse_timeout, default=None,
                        help="Visual request timeout in seconds; default is none (unlimited)")
    parser.add_argument("--think", choices=("auto", "true", "false", "default", "low", "medium", "xhigh"), default="auto",
                        help="auto selects xhigh for qwen3.8 and enables thinking for qwen3.5; explicit levels are passed unchanged")
    parser.add_argument("--progress-interval", type=float, default=15.0,
                        help="Seconds between progress-file writes")
    parser.add_argument("--scene-ids", default="s_001")
    parser.add_argument("--output-dir", default="output/diagnostics/ollama-native-visual")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args()
    if args.progress_interval <= 0:
        raise SystemExit("--progress-interval должен быть больше нуля")
    settings = load_settings()
    providers = {provider.name: provider for provider in settings.providers}
    secondary = providers.get("secondary")
    if secondary is None:
        raise SystemExit("Не настроен LLM secondary в .env; секреты не публикуй.")
    base_url = native_base_url(secondary.base_url)

    storyboard = Path(args.storyboard)
    if not storyboard.is_absolute():
        storyboard = ROOT / storyboard
    if not storyboard.exists():
        raise SystemExit(f"Не найден storyboard: {storyboard}")
    scene_ids = [part.strip() for part in args.scene_ids.split(",") if part.strip()]
    models = [part.strip() for part in args.models.split(",") if part.strip()]
    if not models or not scene_ids:
        raise SystemExit("Нужно указать --models и --scene-ids")

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"ollama_native_{stamp}.json"
    md_path = output_dir / f"ollama_native_{stamp}.md"
    report: dict[str, Any] = {
        "report_version": "1.1",
        "status": "in_progress",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "endpoint_mode": "native_ollama_api_chat_streaming",
        "base_url_host": urlsplit(base_url).hostname,
        "storyboard": str(storyboard),
        "scene_ids": scene_ids,
        "num_ctx_requested": args.ollama_num_ctx,
        "visual_max_tokens": args.visual_max_tokens,
        "visual_timeout_seconds": args.visual_timeout,
        "visual_timeout_unlimited": args.visual_timeout is None,
        "think_mode_requested": args.think,
        "system_prompt_sha256": sha256_text(SYSTEM_PROMPT),
        "schema_sha256": sha256_text(json.dumps(_schema_for_provider(VisualPlanBatch), sort_keys=True, ensure_ascii=False)),
        "models": {},
    }

    def persist() -> None:
        write_json(json_path, report)
        lines = [
            "# Ollama native API: сравнение локальных моделей",
            "",
            f"Дата UTC: {report['created_at_utc']}",
            "",
            f"Thinking mode: `{args.think}`; visual timeout: `{args.visual_timeout if args.visual_timeout is not None else 'unlimited'}`.",
            "",
            "| Модель | Ping | JSON schema | Визуальный план | Время, с | Thinking, символов | eval tokens |",
            "|---|---|---|---|---:|---:|---:|",
        ]
        for name, item in report["models"].items():
            tests = item.get("tests", {})
            visual = tests.get("visual_single", {})
            lines.append(
                f"| `{name}` | {tests.get('plain_ping', {}).get('status', '—')} "
                f"| {tests.get('tiny_json_schema', {}).get('status', '—')} "
                f"| {visual.get('status', '—')} | {visual.get('elapsed_seconds', '—')} "
                f"| {visual.get('thinking_chars', '—')} | {visual.get('eval_count', '—')} |"
            )
        lines.extend([
            "",
            "## Примечания",
            "Нативный `/api/chat` вызывается с `stream=true`. В режиме `--think auto` для `qwen3.8:*` отправляется `think: xhigh` (максимальный уровень, объявленный `/api/show`), для `qwen3.5:*` отправляется `think=true`; для остальных моделей поле `think` не отправляется. Уровни `low`, `medium`, `xhigh` можно задавать явно. Тайм-аут визуального запроса по умолчанию отсутствует (`requests timeout=None`). Промпт и Pydantic-схема совпадают с production визуальным планировщиком.",
            "Промежуточные данные сохраняются в `.progress.json`; полный итоговый ответ и thinking — в отдельных `.response.txt` и `.thinking.txt` файлах.",
            "",
        ])
        md_path.write_text("\n".join(lines), encoding="utf-8")

    persist()
    for model in models:
        think_setting = resolve_think_setting(model, args.think)
        LOG.info("=== %s ===", model)
        LOG.info("Thinking setting: %s", describe_think_setting(think_setting))
        slug = safe_slug(model)
        progress_path = output_dir / f"ollama_native_{stamp}_{slug}_visual.progress.json"
        artifact_prefix = output_dir / f"ollama_native_{stamp}_{slug}_visual"
        try:
            item = run_model(
                model=model,
                base_url=base_url,
                storyboard=storyboard,
                num_ctx=args.ollama_num_ctx,
                max_tokens=args.visual_max_tokens,
                warmup_timeout=args.warmup_timeout,
                visual_timeout=args.visual_timeout,
                scene_ids=scene_ids,
                think_setting=think_setting,
                progress_path=progress_path,
                artifact_prefix=artifact_prefix,
                progress_interval=args.progress_interval,
            )
        except KeyboardInterrupt:
            report["models"][model] = {
                "model": model,
                "think_setting": describe_think_setting(think_setting),
                "tests": {
                    "visual_single": {
                        "status": "interrupted_by_user",
                        "progress_file": str(progress_path) if progress_path.exists() else None,
                    }
                },
            }
            report["status"] = "interrupted"
            persist()
            LOG.warning("Остановлено пользователем. Промежуточный файл: %s", progress_path)
            print(f"JSON: {json_path}\nMarkdown: {md_path}")
            return 130
        except Exception as exc:
            item = {
                "model": model,
                "think_setting": describe_think_setting(think_setting),
                "tests": {},
                "status": "script_error",
                "error": f"{type(exc).__name__}: {exc}"[:4000],
                "progress_file": str(progress_path) if progress_path.exists() else None,
            }
        report["models"][model] = item
        persist()
        LOG.info(
            "%s: ping=%s schema=%s visual=%s",
            model,
            item.get("tests", {}).get("plain_ping", {}).get("status"),
            item.get("tests", {}).get("tiny_json_schema", {}).get("status"),
            item.get("tests", {}).get("visual_single", {}).get("status"),
        )
    report["status"] = "complete"
    persist()
    print(f"JSON: {json_path}\nMarkdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
