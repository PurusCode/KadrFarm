from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT_SRC = ROOT / "src"
# Test the patched script against the project's real production Pydantic schema.
PROJECT_ROOT = Path.cwd()
if (PROJECT_ROOT / "src").exists():
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
else:
    # The diagnostic helpers can also be tested in isolation outside the repo.
    import types

    package = types.ModuleType("kadr_farm")
    package.__path__ = []
    sys.modules.setdefault("kadr_farm", package)

    common = types.ModuleType("kadr_farm.common")
    common.parse_json_response = json.loads
    common.read_json = lambda path: json.loads(Path(path).read_text(encoding="utf-8"))
    common.write_json = lambda path, data: Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    sys.modules["kadr_farm.common"] = common

    config = types.ModuleType("kadr_farm.config")
    config.load_settings = lambda: None
    sys.modules["kadr_farm.config"] = config

    llm_structured = types.ModuleType("kadr_farm.llm_structured")
    llm_structured._schema_for_provider = lambda model: {"type": "object"}
    sys.modules["kadr_farm.llm_structured"] = llm_structured

    models = types.ModuleType("kadr_farm.models")
    models.VisualPlanBatch = type("VisualPlanBatch", (), {})
    sys.modules["kadr_farm.models"] = models

    visual_planner = types.ModuleType("kadr_farm.visual_planner")
    visual_planner.SYSTEM_PROMPT = "test visual planner system prompt"
    sys.modules["kadr_farm.visual_planner"] = visual_planner

SCRIPT = ROOT / "tools" / "diagnose_ollama_native.py"
spec = importlib.util.spec_from_file_location("diagnose_ollama_native", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_auto_enables_thinking_for_canonical_qwen35():
    assert module.resolve_think_setting("qwen3.5:9b", "auto") is True
    assert module.resolve_think_setting("QWEN3.5:27B", "auto") is True


def test_auto_selects_xhigh_for_qwen38():
    assert module.resolve_think_setting("qwen3.8:27b", "auto") == "xhigh"
    assert module.resolve_think_setting("qwen3.8", "auto") == "xhigh"


def test_explicit_qwen38_thinking_levels_are_preserved():
    assert module.resolve_think_setting("qwen3.8:27b", "xhigh") == "xhigh"
    assert module.resolve_think_setting("qwen3.8:27b", "medium") == "medium"
    assert module.resolve_think_setting("qwen3.8:27b", "low") == "low"


def test_auto_enables_thinking_for_qwen35_and_preserves_other_defaults():
    assert module.resolve_think_setting("qwen3.5:9b", "auto") is True
    assert module.resolve_think_setting("qwen2.5:14b-instruct", "auto") is None
    assert module.resolve_think_setting("frob/qwen3.5-instruct:latest", "auto") is None


def test_payload_passes_xhigh_as_a_string_without_downgrading():
    common = dict(model="qwen3.8:27b", messages=[{"role": "user", "content": "test"}], num_ctx=16384, num_predict=12000)
    assert module.build_native_payload(**common, think="xhigh")["think"] == "xhigh"
    assert "think" not in module.build_native_payload(**common, think=None)
    assert module.build_native_payload(**common, think=False)["think"] is False


def test_payload_defaults_to_streaming_and_sets_context():
    payload = module.build_native_payload(
        "qwen3.5:9b", [{"role": "user", "content": "test"}],
        num_ctx=16384, num_predict=12000, think=True,
    )
    assert payload["stream"] is True
    assert payload["options"]["num_ctx"] == 16384
    assert payload["options"]["num_predict"] == 12000
    assert payload["think"] is True


def test_parse_timeout_none_is_unlimited():
    assert module.parse_timeout("none") is None
    assert module.parse_timeout("unlimited") is None
    assert module.parse_timeout("120") == 120.0
    with pytest.raises(Exception):
        module.parse_timeout("0")


class FakeResponse:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def raise_for_status(self):
        return None

    def iter_lines(self, decode_unicode=False):
        assert decode_unicode is False
        for event in self.events:
            yield json.dumps(event, ensure_ascii=False).encode("utf-8")

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def post(self, endpoint, **kwargs):
        self.kwargs = {"endpoint": endpoint, **kwargs}
        return self.response


def test_streaming_call_collects_thinking_content_and_final_metrics():
    response = FakeResponse([
        {"message": {"role": "assistant", "thinking": "Рассуждение "}, "done": False},
        {"message": {"role": "assistant", "thinking": "модели."}, "done": False},
        {"message": {"role": "assistant", "content": "Ответ."}, "done": False},
        {"message": {"role": "assistant", "content": " Готово."}, "done": True, "done_reason": "stop", "eval_count": 123},
    ])
    session = FakeSession(response)
    snapshots = []
    result = module.call_ollama(
        session, "http://localhost:11434/api/chat",
        {"model": "qwen3.5:9b", "messages": [], "stream": False},
        None,
        progress_callback=snapshots.append,
        progress_interval=0.001,
    )
    content, thinking = module.get_message_text(result)
    assert content == "Ответ. Готово."
    assert thinking == "Рассуждение модели."
    assert result["done_reason"] == "stop"
    assert result["eval_count"] == 123
    assert session.kwargs["timeout"] is None
    assert session.kwargs["stream"] is True
    assert session.kwargs["json"]["stream"] is True
    assert response.closed is True
    assert snapshots


def test_streaming_call_flushes_partial_progress_on_keyboard_interrupt():
    class InterruptingResponse(FakeResponse):
        def iter_lines(self, decode_unicode=False):
            yield json.dumps({"message": {"role": "assistant", "thinking": "partial"}, "done": False}).encode()
            raise KeyboardInterrupt()

    response = InterruptingResponse([])
    session = FakeSession(response)
    snapshots = []
    with pytest.raises(KeyboardInterrupt):
        module.call_ollama(
            session, "http://localhost:11434/api/chat", {"model": "qwen3.5:9b"}, None,
            progress_callback=snapshots.append,
        )
    assert response.closed is True
    assert snapshots[-1]["thinking"] == "partial"
    assert snapshots[-1]["interrupted_or_failed"] is True
