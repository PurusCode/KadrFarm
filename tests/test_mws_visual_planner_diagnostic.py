from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT


def load_diagnostic(monkeypatch):
    """Load diagnostic with tiny project-module stubs; no network/config reads."""
    src = PROJECT_ROOT / "src"
    sys.path.insert(0, str(src)) if str(src) not in sys.path else None

    common = types.ModuleType("kadr_farm.common")
    common.parse_json_response = lambda value: __import__("json").loads(value)
    common.read_json = lambda path: {}
    config = types.ModuleType("kadr_farm.config")
    config.load_settings = lambda: None
    llm = types.ModuleType("kadr_farm.llm_structured")
    llm._schema_for_provider = lambda model: {"type": "object"}
    models = types.ModuleType("kadr_farm.models")

    class DummyVisualPlanBatch:
        @classmethod
        def model_validate(cls, value):
            return value

    models.VisualPlanBatch = DummyVisualPlanBatch
    planner = types.ModuleType("kadr_farm.visual_planner")
    planner.SYSTEM_PROMPT = "Test system prompt"

    package = types.ModuleType("kadr_farm")
    package.__path__ = []
    for name, module in {
        "kadr_farm": package,
        "kadr_farm.common": common,
        "kadr_farm.config": config,
        "kadr_farm.llm_structured": llm,
        "kadr_farm.models": models,
        "kadr_farm.visual_planner": planner,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    path = ROOT / "tools" / "diagnose_mws_visual_planner.py"
    spec = importlib.util.spec_from_file_location("mws_visual_diagnostic_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_json_instruction_is_appended_and_can_be_disabled(monkeypatch):
    diagnostic = load_diagnostic(monkeypatch)
    scenes = [{
        "id": "s_001", "block_id": "b_001", "voice_text": "Привет",
        "meaning": "hook", "purpose": "intro",
    }]
    with_instruction = diagnostic.visual_user_prompt(scenes)
    without_instruction = diagnostic.visual_user_prompt(scenes, include_json_instruction=False)
    assert "ровно один ПОЛНЫЙ JSON-объект" in with_instruction
    assert with_instruction.startswith(without_instruction)
    assert "ровно один ПОЛНЫЙ JSON-объект" not in without_instruction


def test_schema_can_be_embedded_in_user_prompt(monkeypatch):
    diagnostic = load_diagnostic(monkeypatch)
    scenes = [{
        "id": "s_001", "block_id": "hook", "voice_text": "Привет",
        "meaning": "hook", "purpose": "intro",
    }]
    schema = {
        "type": "object",
        "properties": {"plans": {"type": "array"}},
        "required": ["plans"],
    }
    prompt = diagnostic.visual_user_prompt(
        scenes, schema=schema, include_schema_in_prompt=True
    )
    assert "ТОЧНАЯ JSON SCHEMA ПРОЕКТА" in prompt
    assert "Допустимые ключи верхнего уровня: plans." in prompt
    assert '"required":["plans"]' in prompt
    assert "Не переименовывай ключи" in prompt


def test_schema_embedding_requires_schema(monkeypatch):
    diagnostic = load_diagnostic(monkeypatch)
    scenes = [{
        "id": "s_001", "block_id": "hook", "voice_text": "Привет",
        "meaning": "hook", "purpose": "intro",
    }]
    try:
        diagnostic.visual_user_prompt(scenes, include_schema_in_prompt=True)
    except ValueError as exc:
        assert "schema is required" in str(exc)
    else:
        raise AssertionError("Expected a ValueError when schema is missing")


def test_response_format_modes(monkeypatch):
    diagnostic = load_diagnostic(monkeypatch)
    schema = {"type": "object"}
    assert diagnostic.response_format(schema, "x", "json_schema_strict")["type"] == "json_schema"
    assert diagnostic.response_format(schema, "x", "json_object") == {"type": "json_object"}
    assert diagnostic.response_format(schema, "x", "none") is None


def test_make_payload_omits_response_format_for_none(monkeypatch):
    diagnostic = load_diagnostic(monkeypatch)
    payload = diagnostic.make_payload(
        model="test-model", system_prompt="system", user_prompt="user",
        schema={"type": "object"}, max_completion_tokens=8192,
        temperature=0.35, reasoning_effort="medium", include_think_token=False,
        response_format_mode="none",
    )
    assert "response_format" not in payload
    assert payload["max_completion_tokens"] == 8192
    assert payload["messages"][1]["content"] == "user"


def test_analyze_content_measures_trailing_whitespace(monkeypatch):
    diagnostic = load_diagnostic(monkeypatch)
    stats = diagnostic.analyze_content('{"a":1}' + (" \n" * 25))
    assert stats["total_chars"] == 57
    assert stats["non_whitespace_chars"] == 7
    assert stats["trailing_whitespace_chars"] == 50
    assert stats["chars_after_last_non_whitespace"] == 50
    assert stats["whitespace_ratio"] > 0.8


def test_accumulator_counts_whitespace_only_deltas(monkeypatch):
    diagnostic = load_diagnostic(monkeypatch)
    accumulator = diagnostic.StreamAccumulator()
    accumulator.add({"choices": [{"delta": {"content": "{\"a\":1}"}, "finish_reason": None}]})
    accumulator.add({"choices": [{"delta": {"content": "   \n"}, "finish_reason": None}]})
    accumulator.add({"choices": [{"delta": {"content": "  "}, "finish_reason": "length"}]})
    assert accumulator.content_delta_count == 3
    assert accumulator.whitespace_only_delta_count == 2
    assert accumulator.whitespace_only_delta_chars == 6
    assert accumulator.max_whitespace_only_delta_chars == 4
    assert accumulator.finish_reason == "length"


def test_reasoning_content_has_priority_over_aliases(monkeypatch):
    diagnostic = load_diagnostic(monkeypatch)
    accumulator = diagnostic.StreamAccumulator()
    accumulator.add({"choices": [{
        "delta": {
            "reasoning_content": "официальное поле",
            "reasoning": "другой alias",
            "content": "{}",
        },
        "finish_reason": None,
    }]})
    assert accumulator.reasoning_text == "официальное поле"
    assert accumulator.content_text == "{}"


def test_sse_parser_decodes_cyrillic_as_utf8(monkeypatch):
    diagnostic = load_diagnostic(monkeypatch)

    class FakeResponse:
        def iter_lines(self, decode_unicode=False):
            assert decode_unicode is False
            yield 'data: {"choices":[{"delta":{"content":"Привет"}}]}'.encode("utf-8")
            yield b"data: [DONE]"

    events = list(diagnostic.iter_sse_events(FakeResponse()))
    assert events[0]["choices"][0]["delta"]["content"] == "Привет"


def test_think_token_is_opt_in(monkeypatch):
    diagnostic = load_diagnostic(monkeypatch)
    assert diagnostic.gemma_system_prompt("SYS", False) == "SYS"
    assert diagnostic.gemma_system_prompt("SYS", True).startswith("<|think|>\n")


def test_visual_type_asset_strategy_and_truthfulness_constraints_are_in_prompt(monkeypatch):
    diagnostic = load_diagnostic(monkeypatch)
    scenes = [{
        "id": "s_001", "block_id": "hook", "voice_text": "Привет",
        "meaning": "hook", "purpose": "intro",
    }]
    prompt = diagnostic.visual_user_prompt(scenes)
    assert "КРИТИЧЕСКОЕ РАЗЛИЧИЕ МЕЖДУ visual_type И asset_strategy" in prompt
    assert "Не используй" in prompt and "в visual_type" in prompt
    assert "сумма всех sub_duration_ratio" in prompt
    assert "Не выдумывай реальные на вид имена авторов" in prompt
    assert "[AUTHOR NAME]" in prompt
