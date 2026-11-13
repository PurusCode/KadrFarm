from __future__ import annotations
import logging
from typing import Any
from .config import Settings
from .common import parse_json_response

log = logging.getLogger(__name__)

class LLMUnavailable(RuntimeError):
    pass

class LLMRouter:
    """OpenAI-compatible LLM clients with retry and provider fallback."""
    def __init__(self, settings: Settings, *, honor_provider_timeouts: bool = False):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMUnavailable("Не установлен пакет openai. Запустите install.ps1 или `pip install -e .`.") from exc
        self.clients: list[tuple[str, str, Any]] = []
        for p in settings.providers:
            # Keep the legacy content pipeline at its historical 10-minute timeout.
            # The new structured pipeline opts into per-provider values (MWS/Ollama).
            timeout_seconds = (
                float(getattr(p, "timeout_seconds", 600.0))
                if honor_provider_timeouts else 600.0
            )
            self.clients.append((p.name, p.model, OpenAI(
                base_url=p.base_url, api_key=p.api_key,
                timeout=timeout_seconds, max_retries=0)))

    def json(self, system_prompt: str, user_prompt: str, *, temperature: float = 0.65,
             max_tokens: int = 7000) -> Any:
        if not self.clients:
            raise LLMUnavailable("LLM не настроена. Заполните .env: LLM_PRIMARY_* и/или LLM_SECONDARY_*.")
        failures = []
        for name, model, client in self.clients:
            try:
                try:
                    result = client.chat.completions.create(
                        model=model,
                        messages=[{"role": "system", "content": system_prompt},
                                  {"role": "user", "content": user_prompt}],
                        temperature=temperature, max_tokens=max_tokens,
                        response_format={"type": "json_object"})
                except Exception as first:
                    # Some OpenAI-compatible local servers do not support response_format.
                    if "response_format" not in str(first).lower() and "json" not in str(first).lower():
                        raise
                    result = client.chat.completions.create(
                        model=model,
                        messages=[{"role": "system", "content": system_prompt},
                                  {"role": "user", "content": user_prompt + "\nВерни только корректный JSON без Markdown."}],
                        temperature=temperature, max_tokens=max_tokens)
                content = result.choices[0].message.content or ""
                payload = parse_json_response(content)
                log.info("LLM ответил через провайдер %s / %s", name, model)
                return payload
            except Exception as exc:
                failures.append(f"{name} ({model}): {type(exc).__name__}: {exc}")
                log.warning("Провайдер %s недоступен: %s", name, exc)
        raise LLMUnavailable("Не удалось получить ответ ни от одного LLM-провайдера:\n" + "\n".join(failures))
