"""Strict structured-output adapter layered on the existing LLMRouter.

Preserves the project's provider order and fallback (MWS -> Ollama). Every
provider response is locally validated by Pydantic regardless of API mode.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, TypeVar
from pydantic import BaseModel, ValidationError

from .llm import LLMRouter
from .common import parse_json_response

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


class StructuredLLMRouter:
    def __init__(self, settings: Any):
        self.router = LLMRouter(settings, honor_provider_timeouts=True)

    def _request(self, client: Any, model_name: str, system_prompt: str,
                 user_prompt: str, *, temperature: float, max_tokens: int,
                 schema_model: type[BaseModel]) -> str:
        schema = _schema_for_provider(schema_model)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
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
            try:
                response = client.chat.completions.create(
                    model=model_name, messages=messages, temperature=temperature,
                    max_tokens=max_tokens, response_format={"type": "json_object"},
                )
            except Exception as json_object_error:
                if not _is_format_unsupported(json_object_error):
                    raise
                messages[0] = {"role": "system", "content": system_prompt +
                               "\nReturn only one valid JSON object. No Markdown."}
                response = client.chat.completions.create(
                    model=model_name, messages=messages, temperature=temperature,
                    max_tokens=max_tokens,
                )
        return response.choices[0].message.content or ""

    def complete(self, schema_model: type[T], system_prompt: str, user_prompt: str,
                 *, temperature: float = 0.25, max_tokens: int = 7000) -> T:
        if not self.router.clients:
            raise StructuredLLMError("LLM не настроена: проверьте LLM_PRIMARY_* и LLM_SECONDARY_* в .env")
        failures: list[str] = []
        for provider_name, model_name, client in self.router.clients:
            started = time.monotonic()
            try:
                raw = self._request(client, model_name, system_prompt, user_prompt,
                                    temperature=temperature, max_tokens=max_tokens,
                                    schema_model=schema_model)
                log.info("Structured LLM: %s / %s answered in %.1fs",
                         provider_name, model_name, time.monotonic() - started)
                try:
                    result = schema_model.model_validate(parse_json_response(raw))
                    log.info("Structured LLM: %s / %s; Pydantic validation passed",
                             provider_name, model_name)
                    return result
                except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as first_error:
                    # Exactly one repair attempt. Pydantic's ctx can contain exception objects
                    # (for example ValueError from model_validator), so plain json.dumps fails.
                    if isinstance(first_error, ValidationError):
                        detail = json.dumps(
                            first_error.errors(include_url=False),
                            ensure_ascii=False, indent=2,
                            default=lambda value: f"{type(value).__name__}: {value}",
                        )
                    else:
                        detail = f"{type(first_error).__name__}: {first_error}"
                    detail = detail[:8000]
                    repair_prompt = (
                        user_prompt + "\n\nПРЕДЫДУЩИЙ ОТВЕТ НЕ ПРОШЁЛ ПРОВЕРКУ. "
                        "Исправь JSON строго по исходной задаче. Ошибка:\n" + detail +
                        "\nПредыдущий ответ:\n" + raw[:12000]
                    )
                    repaired_raw = self._request(
                        client, model_name, system_prompt, repair_prompt,
                        temperature=0.1, max_tokens=max_tokens,
                        schema_model=schema_model,
                    )
                    result = schema_model.model_validate(parse_json_response(repaired_raw))
                    log.info("Structured LLM: исправленный ответ прошёл Pydantic validation")
                    return result
            except Exception as exc:
                failures.append(f"{provider_name}/{model_name}: {type(exc).__name__}: {exc}")
                log.warning(
                    "Structured LLM provider failed: %s/%s (%s) after %.1fs: %s",
                    provider_name, model_name, type(exc).__name__,
                    time.monotonic() - started, exc,
                )
        raise StructuredLLMError("Не удалось получить валидный структурированный ответ:\n" +
                                 "\n".join(failures))
