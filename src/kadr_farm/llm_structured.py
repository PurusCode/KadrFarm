"""Strict structured-output adapter with auditable raw responses and resumable tasks."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, TypeVar
from pydantic import BaseModel, ValidationError

from .llm import LLMRouter
from .common import parse_json_response
from .production_recovery import ProductionJournal, fingerprint

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class StructuredLLMError(RuntimeError):
    pass


def _schema_for_provider(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and isinstance(node.get("properties"), dict):
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(schema)
    return schema


def _is_format_unsupported(exc: Exception) -> bool:
    message = str(exc).lower()
    terms = ("response_format", "json_schema", "unsupported", "not support",
             "unknown parameter", "invalid schema", "extra_forbidden")
    return any(term in message for term in terms)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, dict)):
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except Exception:
            return str(value)
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:
            pass
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return str(value)


class StructuredLLMRouter:
    def __init__(self, settings: Any, *, journal: ProductionJournal | None = None):
        self.router = LLMRouter(settings, honor_provider_timeouts=True)
        self.journal = journal

    def _request(self, client: Any, model_name: str, system_prompt: str,
                 user_prompt: str, *, temperature: float, max_tokens: int,
                 schema_model: type[BaseModel]) -> tuple[str, dict[str, Any]]:
        schema = _schema_for_provider(schema_model)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        format_errors: list[str] = []
        format_mode = "json_schema"
        try:
            response = client.chat.completions.create(
                model=model_name, messages=messages, temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_schema", "json_schema": {
                    "name": "semantic_pipeline_output", "strict": True, "schema": schema
                }},
            )
        except Exception as exc:
            if not _is_format_unsupported(exc):
                raise
            format_errors.append(f"json_schema: {type(exc).__name__}: {exc}")
            format_mode = "json_object"
            try:
                response = client.chat.completions.create(
                    model=model_name, messages=messages, temperature=temperature,
                    max_tokens=max_tokens, response_format={"type": "json_object"},
                )
            except Exception as json_object_error:
                if not _is_format_unsupported(json_object_error):
                    raise
                format_errors.append(
                    f"json_object: {type(json_object_error).__name__}: {json_object_error}"
                )
                format_mode = "prompt_only"
                messages[0] = {"role": "system", "content": system_prompt +
                               "\nReturn only one valid JSON object. No Markdown."}
                response = client.chat.completions.create(
                    model=model_name, messages=messages,
                    temperature=temperature, max_tokens=max_tokens,
                )

        choice = response.choices[0]
        message = choice.message
        content = getattr(message, "content", None)
        if content is None:
            raw = ""
        elif isinstance(content, str):
            raw = content
        else:
            raw = json.dumps(content, ensure_ascii=False, default=str)
        metadata = {
            "format_mode": format_mode,
            "format_fallback_errors": format_errors,
            "response_id": getattr(response, "id", None),
            "created": getattr(response, "created", None),
            "finish_reason": getattr(choice, "finish_reason", None),
            "usage": _jsonable(getattr(response, "usage", None)),
            "refusal": _jsonable(getattr(message, "refusal", None)),
            "reasoning_content": _jsonable(getattr(message, "reasoning_content", None)),
            "thinking": _jsonable(getattr(message, "thinking", None)),
        }
        return raw, metadata

    def complete(self, schema_model: type[T], system_prompt: str, user_prompt: str,
                 *, temperature: float = 0.25, max_tokens: int = 7000,
                 task_id: str | None = None, stage: str = "structured_llm") -> T:
        # Backward compatibility for callers/tests that instantiate via __new__.
        journal = getattr(self, "journal", None)
        if not self.router.clients:
            raise StructuredLLMError(
                "LLM не настроена: проверьте LLM_PRIMARY_* и LLM_SECONDARY_* в .env"
            )
        schema = _schema_for_provider(schema_model)
        request_fingerprint = fingerprint({
            "schema_name": schema_model.__name__, "schema": schema,
            "system_prompt": system_prompt, "user_prompt": user_prompt,
            "temperature": temperature, "max_tokens": max_tokens,
            "providers": [{
                "name": x[0], "model": x[1],
                "base_url": str(getattr(x[2], "base_url", "")),
            } for x in self.router.clients],
        })
        effective_task_id = task_id or f"unkeyed:{schema_model.__name__}:{time.time_ns()}"

        if journal is not None and task_id:
            cached = journal.cached_result(task_id, request_fingerprint)
            if cached is not None:
                try:
                    result = schema_model.model_validate(cached)
                    log.info("Structured LLM checkpoint reused: %s", task_id)
                    return result
                except (ValidationError, ValueError, TypeError) as exc:
                    log.warning("Cached LLM result failed current schema; requesting a new result: %s", exc)

        failures: list[str] = []
        last_provider = "unknown"
        last_model = "unknown"
        for provider_name, model_name, client in self.router.clients:
            last_provider, last_model = provider_name, model_name
            started = time.monotonic()
            if journal is not None:
                journal.start_task(
                    effective_task_id, stage=stage, input_fingerprint=request_fingerprint,
                    provider=provider_name, model=model_name,
                )
            request_record = {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "schema_name": schema_model.__name__,
                "json_schema": schema,
            }
            raw_artifact: str | None = None
            try:
                raw, metadata = self._request(
                    client, model_name, system_prompt, user_prompt,
                    temperature=temperature, max_tokens=max_tokens,
                    schema_model=schema_model,
                )
                if journal is not None:
                    raw_artifact = journal.record_model_call(
                        task_id=effective_task_id, stage=stage, provider=provider_name,
                        model=model_name, request=request_record, raw_response=raw,
                        response_metadata=metadata, call_kind="initial",
                    )
                log.info("Structured LLM: %s / %s answered in %.1fs",
                         provider_name, model_name, time.monotonic() - started)
                try:
                    result = schema_model.model_validate(parse_json_response(raw))
                    if journal is not None and raw_artifact:
                        journal.update_model_call(raw_artifact, {"status": "passed"})
                    if journal is not None:
                        journal.complete_task(
                            effective_task_id, stage=stage,
                            input_fingerprint=request_fingerprint,
                            result=result.model_dump(mode="json"),
                            schema_name=schema_model.__name__, provider=provider_name,
                            model=model_name, raw_artifact=raw_artifact,
                        )
                    log.info("Structured LLM: %s / %s; Pydantic validation passed",
                             provider_name, model_name)
                    return result
                except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as first_error:
                    if isinstance(first_error, ValidationError):
                        detail = json.dumps(
                            first_error.errors(include_url=False), ensure_ascii=False,
                            indent=2,
                            default=lambda value: f"{type(value).__name__}: {value}",
                        )
                    else:
                        detail = f"{type(first_error).__name__}: {first_error}"
                    detail = detail[:8000]
                    if journal is not None and raw_artifact:
                        journal.update_model_call(raw_artifact, {
                            "status": "failed", "error_type": type(first_error).__name__,
                            "error": str(first_error)[:4000],
                        })
                    repair_prompt = (
                        user_prompt + "\n\nПРЕДЫДУЩИЙ ОТВЕТ НЕ ПРОШЁЛ ПРОВЕРКУ. "
                        "Исправь JSON строго по исходной задаче. Ошибка:\n" + detail +
                        "\nПредыдущий ответ:\n" + raw[:12000]
                    )
                    repair_request = {**request_record, "user_prompt": repair_prompt,
                                      "repair_of": raw_artifact}
                    repair_artifact: str | None = None
                    try:
                        repaired_raw, repaired_metadata = self._request(
                            client, model_name, system_prompt, repair_prompt,
                            temperature=0.1, max_tokens=max_tokens,
                            schema_model=schema_model,
                        )
                    except Exception as repair_error:
                        if journal is not None:
                            journal.record_model_call(
                                task_id=effective_task_id, stage=stage,
                                provider=provider_name, model=model_name,
                                request=repair_request, error=repair_error,
                                call_kind="repair",
                            )
                        raise
                    if journal is not None:
                        repair_artifact = journal.record_model_call(
                            task_id=effective_task_id, stage=stage, provider=provider_name,
                            model=model_name, request=repair_request,
                            raw_response=repaired_raw, response_metadata=repaired_metadata,
                            call_kind="repair",
                        )
                    try:
                        result = schema_model.model_validate(parse_json_response(repaired_raw))
                    except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as repair_validation_error:
                        if journal is not None and repair_artifact:
                            journal.update_model_call(repair_artifact, {
                                "status": "failed",
                                "error_type": type(repair_validation_error).__name__,
                                "error": str(repair_validation_error)[:4000],
                            })
                        raise
                    if journal is not None and repair_artifact:
                        journal.update_model_call(repair_artifact, {"status": "passed"})
                    if journal is not None:
                        journal.complete_task(
                            effective_task_id, stage=stage,
                            input_fingerprint=request_fingerprint,
                            result=result.model_dump(mode="json"),
                            schema_name=schema_model.__name__, provider=provider_name,
                            model=model_name, raw_artifact=repair_artifact or raw_artifact,
                        )
                    log.info("Structured LLM: repaired response passed Pydantic validation")
                    return result
            except Exception as exc:
                failures.append(f"{provider_name}/{model_name}: {type(exc).__name__}: {exc}")
                if journal is not None:
                    if raw_artifact is None:
                        journal.record_model_call(
                            task_id=effective_task_id, stage=stage,
                            provider=provider_name, model=model_name,
                            request=request_record, error=exc, call_kind="initial",
                        )
                    journal.fail_task(
                        effective_task_id, stage=stage,
                        input_fingerprint=request_fingerprint,
                        provider=provider_name, model=model_name, error=exc,
                    )
                log.warning(
                    "Structured LLM provider failed: %s/%s (%s) after %.1fs: %s",
                    provider_name, model_name, type(exc).__name__,
                    time.monotonic() - started, exc,
                )
        raise StructuredLLMError(
            "Не удалось получить валидный структурированный ответ:\n" + "\n".join(failures)
        )
