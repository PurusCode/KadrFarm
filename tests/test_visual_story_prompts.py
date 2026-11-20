from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from kadr_farm.visual_story import (
    ApprovedFact,
    DataProvenance,
    SceneCompositionPlan,
    VisualStoryPlan,
    split_speech_segments,
)
from kadr_farm.visual_story_prompts import (
    GLOBAL_PROMPT_VERSION,
    GLOBAL_STAGE,
    GLOBAL_SYSTEM_PROMPT,
    GLOBAL_TASK_ID,
    SCENE_PROMPT_VERSION,
    SCENE_STAGE,
    SCENE_SYSTEM_PROMPT,
    SpeechRegistry,
    build_global_director_request,
    build_scene_composition_request,
    build_speech_registry,
    storyboard_fingerprint,
    validate_global_plan_coverage,
    validate_scene_composition_prefix,
)


B1 = "Сколько нужно капитала? Правило простое."
B2 = "Вернёмся к счётчику. Умножь расходы на двадцать пять."


def _storyboard():
    return {
        "schema_version": "1.0-draft",
        "title": "Сколько нужно денег для финансовой независимости",
        "source_script_sha256": "a" * 64,
        "scenes": [
            {
                "id": "s_001", "block_id": "block_01", "meaning": "Постановка вопроса",
                "purpose": "Зацепить зрителя вопросом о капитале", "voice_text": B1,
            },
            {
                "id": "s_002", "block_id": "block_01", "meaning": "Переход к расчёту",
                "purpose": "Показать связь расходов и необходимого капитала", "voice_text": B2,
            },
        ],
    }


def _global_plan_dict():
    return {
        "schema_version": "1.1",
        "visual_thesis": "Одна финансовая цель постепенно превращается из вопроса в проверяемый расчёт.",
        "arc_summary": "История начинается с неопределённой цели, показывает исходные расходы и раскрывает способ расчёта необходимого капитала.",
        "style": {"palette": ["#fdf3dc", "#e8862e"], "font_family": "Segoe UI"},
        "arc": ["s_001", "s_002"],
        "motifs": [{
            "motif_id": "motif_capital", "kind": "counter",
            "description": "Счётчик необходимого капитала, который постепенно наполняется",
            "first_scene": "s_001", "returns_in": ["s_002"],
        }],
        "scenes": [
            {
                "scene_id": "s_001", "narrative_role": "hook",
                "purpose": "Поставить главный вопрос ролика",
                "visual_intent": "Ввести один выразительный счётчик как образ финансовой цели",
                "relation_to_previous": "introduces", "develops_scene_id": None,
                "motif_ids": ["motif_capital"], "link_prev": None, "link_next": "s_002",
            },
            {
                "scene_id": "s_002", "narrative_role": "development",
                "purpose": "Перевести вопрос в математическую модель",
                "visual_intent": "Вернуть тот же счётчик и показать, как расходы превращаются в капитал",
                "relation_to_previous": "extends", "develops_scene_id": "s_001",
                "motif_ids": ["motif_capital"], "link_prev": "s_001", "link_next": None,
            },
        ],
    }


def _scene_plan_dict(storyboard=None):
    storyboard = storyboard or _storyboard()
    registry = build_speech_registry(storyboard)
    beat1 = registry.scene_beat_ids["s_001"][0]
    beat2 = registry.scene_beat_ids["s_002"][0]
    s1 = registry.segments[beat1]
    s2 = registry.segments[beat2]
    return [
        {
            "scene_id": "s_001",
            "compositions": [{
                "composition_id": "comp_s001_01", "scene_id": "s_001",
                "develops": None, "relation": "introduces",
                "layers": [
                    {"object_id": "bg_desk", "zone": "bg", "primitive": "image", "z_order": 0},
                    {
                        "object_id": "capital_counter", "zone": "center", "primitive": "counter",
                        "z_order": 1, "motif_id": "motif_capital", "labels": ["Цель"],
                        "actions": [{
                            "action_id": "action_s001_01", "kind": "appear", "target": "capital_counter",
                            "anchor": {"beat_id": beat1, "char_start": s1[0].char_start,
                                       "char_end": s1[0].char_end},
                            "params": {"kind": "appear"},
                        }],
                    },
                ],
                "entry_state": {}, "exit_state": {"capital_counter": "visible"},
            }],
        },
        {
            "scene_id": "s_002",
            "compositions": [{
                "composition_id": "comp_s002_01", "scene_id": "s_002",
                "develops": "comp_s001_01", "relation": "extends",
                "layers": [
                    {"object_id": "bg_desk_2", "zone": "bg", "primitive": "image", "z_order": 0},
                    {
                        "object_id": "capital_counter", "zone": "left", "primitive": "counter",
                        "z_order": 1, "motif_id": "motif_capital", "labels": ["Цель"],
                        "actions": [{
                            "action_id": "action_s002_01", "kind": "highlight", "target": "capital_counter",
                            "anchor": {"beat_id": beat2, "char_start": s2[0].char_start,
                                       "char_end": s2[0].char_end},
                            "params": {"kind": "highlight", "color": "#e8862e"},
                        }],
                    },
                    {
                        "object_id": "calculation_formula", "zone": "right", "primitive": "formula",
                        "z_order": 2, "motif_id": "motif_capital",
                        "labels": ["Расходы", "× множитель", "Капитал"],
                        "actions": [{
                            "action_id": "action_s002_02", "kind": "appear", "target": "calculation_formula",
                            "anchor": {"beat_id": beat2, "char_start": s2[1].char_start,
                                       "char_end": s2[1].char_end},
                            "params": {"kind": "appear"},
                        }],
                    },
                ],
                "entry_state": {"capital_counter": "visible"},
                "exit_state": {"capital_counter": "emphasized", "calculation_formula": "visible"},
            }],
        },
    ]


def _parsed_scene_plans():
    return [SceneCompositionPlan.model_validate(item) for item in _scene_plan_dict()]


def _facts_and_values():
    facts = {
        "fact_expenses": ApprovedFact(
            fact_id="fact_expenses", value="1200000", unit="RUB",
            source_url="https://example.org/expenses", source_title="Approved expense record",
            status="verified", verified_by_human=True,
        ),
    }
    provenance = {
        "expenses": DataProvenance(
            value_id="expenses", kind="literal", value="1200000", unit="RUB",
            fact_id="fact_expenses",
        ),
    }
    return provenance, facts


def test_global_request_is_one_whole_video_call_with_exact_ordered_scenes():
    storyboard = _storyboard()
    request = build_global_director_request(storyboard)
    payload = json.loads(request.user_prompt.split("\n", 1)[1])

    assert request.schema_model is VisualStoryPlan
    assert request.task_id == GLOBAL_TASK_ID
    assert request.stage == GLOBAL_STAGE
    assert request.prompt_version == GLOBAL_PROMPT_VERSION
    assert [item["scene_id"] for item in payload["scenes_in_authoritative_order"]] == ["s_001", "s_002"]
    assert payload["scenes_in_authoritative_order"][0]["voice_text"] == B1
    assert payload["scenes_in_authoritative_order"][1]["voice_text"] == B2
    assert "ВЕСЬ ролик целиком" in request.system_prompt
    assert "таймкоды" in request.system_prompt
    assert "не разбивай его на независимые задачи" in request.user_prompt


def test_global_plan_requires_explicit_visual_thesis_and_arc_summary():
    data = _global_plan_dict()
    data.pop("visual_thesis")
    with pytest.raises(ValidationError):
        VisualStoryPlan.model_validate(data)

    data = _global_plan_dict()
    data["arc_summary"] = "слишком коротко"
    with pytest.raises(ValidationError):
        VisualStoryPlan.model_validate(data)


def test_global_request_rejects_missing_title_and_empty_scenes():
    with pytest.raises(ValueError, match="title/topic"):
        build_global_director_request({"scenes": _storyboard()["scenes"]})
    with pytest.raises(ValueError, match="non-empty scenes"):
        build_global_director_request({"title": "A title", "scenes": []})


def test_storyboard_input_rejects_duplicate_ids_and_invalid_source_hash():
    data = _storyboard()
    data["scenes"].append(dict(data["scenes"][0]))
    with pytest.raises(ValueError, match="duplicate storyboard scene id"):
        build_global_director_request(data)

    data = _storyboard()
    data["source_script_sha256"] = "not-a-hash"
    with pytest.raises(ValueError, match="source_script_sha256"):
        build_global_director_request(data)


def test_speech_registry_uses_deterministic_scene_beat_ids_and_exact_text_spans():
    registry = build_speech_registry(_storyboard())

    assert isinstance(registry, SpeechRegistry)
    assert registry.beat_order == ["s_001_voice", "s_002_voice"]
    assert registry.scene_beat_ids == {
        "s_001": ["s_001_voice"], "s_002": ["s_002_voice"]
    }
    for beat_id in registry.beat_order:
        assert "".join(unit.text for unit in registry.segments[beat_id]) == registry.beat_texts[beat_id]
        assert all(unit.duration_sec is None and unit.wav_sha256 is None
                   for unit in registry.segments[beat_id])


def test_global_prompt_builder_is_deterministic_and_does_not_mutate_input():
    storyboard = _storyboard()
    before = json.dumps(storyboard, ensure_ascii=False, sort_keys=True)
    first = build_global_director_request(storyboard)
    second = build_global_director_request(storyboard)

    assert first.user_prompt == second.user_prompt
    assert storyboard_fingerprint(storyboard) == storyboard_fingerprint(storyboard)
    assert json.dumps(storyboard, ensure_ascii=False, sort_keys=True) == before


def test_scene_request_uses_scene_composition_schema_and_stable_task_id():
    storyboard = _storyboard()
    global_plan = VisualStoryPlan.model_validate(_global_plan_dict())
    first_plan = _parsed_scene_plans()[0]

    request = build_scene_composition_request(
        storyboard, global_plan, "s_002", previous_scene_plans=[first_plan]
    )
    payload = json.loads(request.user_prompt.split("\n", 1)[1])

    assert request.schema_model is SceneCompositionPlan
    assert request.task_id == f"visual_story:scene:s_002:{SCENE_PROMPT_VERSION}"
    assert request.stage == SCENE_STAGE
    assert request.prompt_version == SCENE_PROMPT_VERSION
    assert payload["current_scene"]["scene"]["scene_id"] == "s_002"
    assert payload["global_direction"]["visual_thesis"].startswith("Одна финансовая цель")
    assert payload["global_direction"]["arc_summary"]
    assert payload["current_scene"]["direction"]["motif_ids"] == ["motif_capital"]
    assert payload["adjacent_scene_directions"]["previous"]["scene_id"] == "s_001"
    assert payload["previous_composition_index"][0]["compositions"][0]["composition_id"] == "comp_s001_01"
    assert payload["recent_continuity_window"][0]["compositions"][0]["exit_state"] == {
        "capital_counter": "visible"
    }
    assert "candidate_units_are_not_measured_wav_durations" in payload["explicit_rules"]
    assert "Не создавай собственные ID" in request.system_prompt


def test_scene_request_rejects_unknown_scene_and_global_plan_coverage_mismatch():
    storyboard = _storyboard()
    plan = VisualStoryPlan.model_validate(_global_plan_dict())
    with pytest.raises(ValueError, match="unknown scene_id"):
        build_scene_composition_request(storyboard, plan, "s_999")

    expanded_storyboard = _storyboard()
    expanded_storyboard["scenes"].append({
        "id": "s_003", "block_id": "block_01", "meaning": "Вывод",
        "purpose": "Завершить рассуждение", "voice_text": "Итак, это итог.",
    })
    with pytest.raises(ValueError, match="scene coverage/order mismatch"):
        build_scene_composition_request(expanded_storyboard, plan, "s_001")


def test_scene_request_requires_all_prior_plans_as_contiguous_prefix():
    storyboard = _storyboard()
    plan = VisualStoryPlan.model_validate(_global_plan_dict())
    first_plan, second_plan = _parsed_scene_plans()

    with pytest.raises(ValueError, match="contiguous validated prefix"):
        build_scene_composition_request(storyboard, plan, "s_002", previous_scene_plans=[])
    with pytest.raises(ValueError, match="contiguous validated prefix"):
        build_scene_composition_request(storyboard, plan, "s_002", previous_scene_plans=[second_plan])
    with pytest.raises(ValueError, match="contiguous validated prefix"):
        build_scene_composition_request(
            storyboard, plan, "s_001", previous_scene_plans=[first_plan]
        )


def test_scene_request_rejects_invalid_previous_scene_plan():
    storyboard = _storyboard()
    plan = VisualStoryPlan.model_validate(_global_plan_dict())
    broken = _scene_plan_dict()[0]
    broken["compositions"][0]["scene_id"] = "s_002"
    with pytest.raises(ValidationError):
        build_scene_composition_request(storyboard, plan, "s_002", previous_scene_plans=[broken])


def test_scene_request_includes_only_resolved_fact_values_and_status():
    storyboard = _storyboard()
    plan = VisualStoryPlan.model_validate(_global_plan_dict())
    previous = _parsed_scene_plans()[0]
    provenance, facts = _facts_and_values()

    request = build_scene_composition_request(
        storyboard, plan, "s_002", previous_scene_plans=[previous],
        provenance=provenance, facts=facts,
    )
    payload = json.loads(request.user_prompt.split("\n", 1)[1])
    assert payload["approved_value_catalog"] == [{
        "value_id": "expenses", "value": "1200000", "unit": "RUB",
        "status": "verified", "fact_ids": ["fact_expenses"],
        "source_urls": ["https://example.org/expenses"],
    }]


def test_scene_request_rejects_facts_without_value_registry():
    storyboard = _storyboard()
    plan = VisualStoryPlan.model_validate(_global_plan_dict())
    provenance, facts = _facts_and_values()
    with pytest.raises(ValueError, match="without a value-provenance registry"):
        build_scene_composition_request(
            storyboard, plan, "s_001", facts=facts
        )


def test_scene_request_rejects_unresolved_or_forged_provenance_before_llm_call():
    storyboard = _storyboard()
    plan = VisualStoryPlan.model_validate(_global_plan_dict())
    bad_provenance = {
        "expenses": DataProvenance(
            value_id="expenses", kind="literal", value="999", unit="RUB",
            fact_id="fact_expenses",
        )
    }
    _, facts = _facts_and_values()
    with pytest.raises(ValueError, match="does not exactly match approved fact"):
        build_scene_composition_request(
            storyboard, plan, "s_001", provenance=bad_provenance, facts=facts
        )


def test_scene_request_contains_full_global_direction_but_only_current_scene_text():
    storyboard = _storyboard()
    plan = VisualStoryPlan.model_validate(_global_plan_dict())
    previous = _parsed_scene_plans()[0]
    request = build_scene_composition_request(
        storyboard, plan, "s_002", previous_scene_plans=[previous]
    )
    payload = json.loads(request.user_prompt.split("\n", 1)[1])

    assert [item["scene_id"] for item in payload["global_direction"]["scene_directions"]] == ["s_001", "s_002"]
    assert payload["current_scene"]["scene"]["voice_text"] == B2
    assert B1 not in payload["current_scene"]["scene"]["voice_text"]
    assert payload["adjacent_scene_directions"]["next"] is None


def test_generation_parameter_bounds_are_validated():
    storyboard = _storyboard()
    with pytest.raises(ValueError, match="temperature"):
        build_global_director_request(storyboard, temperature=2.1)
    with pytest.raises(ValueError, match="max_tokens"):
        build_global_director_request(storyboard, max_tokens=0)
    with pytest.raises(TypeError, match="temperature"):
        build_global_director_request(storyboard, temperature=True)


def test_scene_task_id_is_safe_and_scene_specific():
    storyboard = _storyboard()
    plan = VisualStoryPlan.model_validate(_global_plan_dict())
    first, _ = _parsed_scene_plans()
    request = build_scene_composition_request(
        storyboard, plan, "s_002", previous_scene_plans=[first]
    )
    assert request.task_id == "visual_story:scene:s_002:scene-v2"
    assert request.task_id != GLOBAL_TASK_ID


def test_prompt_does_not_allow_model_authored_timing_or_fact_creation():
    assert "Никаких start_sec, end_sec" in SCENE_SYSTEM_PROMPT
    assert "Не создавай DataProvenance" in SCENE_SYSTEM_PROMPT
    assert "Не назначай равномерную частоту смены планов" in GLOBAL_SYSTEM_PROMPT


def test_global_coverage_validator_rejects_missing_or_reordered_scene_ids():
    storyboard = _storyboard()
    plan = VisualStoryPlan.model_validate(_global_plan_dict())
    assert validate_global_plan_coverage(storyboard, plan) is plan

    expanded = _storyboard()
    expanded["scenes"].append({
        "id": "s_003", "block_id": "block_01", "meaning": "Вывод",
        "purpose": "Завершить историю", "voice_text": "Итак, это итог.",
    })
    with pytest.raises(ValueError, match="scene coverage/order mismatch"):
        validate_global_plan_coverage(expanded, plan)

    reordered = _global_plan_dict()
    reordered["scenes"].reverse()
    reordered["scenes"][0]["link_prev"] = None
    reordered["scenes"][0]["link_next"] = "s_001"
    reordered["scenes"][1]["link_prev"] = "s_002"
    reordered["scenes"][1]["link_next"] = None
    # The plan's own ordering constraints reject this before coverage comparison.
    with pytest.raises(ValidationError):
        validate_global_plan_coverage(storyboard, reordered)


def test_documentation_json_examples_match_the_pydantic_contracts():
    from pathlib import Path
    import re

    document = Path(__file__).parents[1] / "docs" / "32_VISUAL_STORY_TWO_PASS_CONTRACT_RU.md"
    blocks = re.findall(r"```json\s*(.*?)```", document.read_text(encoding="utf-8"), re.S)
    assert len(blocks) == 2
    VisualStoryPlan.model_validate(json.loads(blocks[0]))
    scene_plan = SceneCompositionPlan.model_validate(json.loads(blocks[1]))
    assert scene_plan.scene_id == "s_002"
    action = scene_plan.compositions[0].layers[1].actions[0]
    assert action.anchor.beat_id == "s_002_voice"
    assert (action.anchor.char_start, action.anchor.char_end) == (0, 21)


def test_next_scene_is_blocked_when_previous_plan_fails_semantic_validation():
    storyboard = _storyboard()
    plan = VisualStoryPlan.model_validate(_global_plan_dict())
    broken = _scene_plan_dict()[0]
    # Pydantic shape remains valid, but the global motif declared for s_001 is absent.
    broken["compositions"][0]["layers"][1]["motif_id"] = None
    parsed_broken = SceneCompositionPlan.model_validate(broken)
    with pytest.raises(ValueError, match="failed semantic validation"):
        build_scene_composition_request(
            storyboard, plan, "s_002", previous_scene_plans=[parsed_broken]
        )


def test_execute_prompt_delegates_to_structured_router_with_journal_task_metadata():
    request = build_global_director_request(_storyboard())

    class FakeRouter:
        def __init__(self):
            self.call = None

        def complete(self, schema_model, system_prompt, user_prompt, **kwargs):
            self.call = (schema_model, system_prompt, user_prompt, kwargs)
            return "fake-result"

    from kadr_farm.visual_story_prompts import execute_prompt
    router = FakeRouter()
    result = execute_prompt(router, request)
    assert result == "fake-result"
    schema_model, system_prompt, user_prompt, kwargs = router.call
    assert schema_model is VisualStoryPlan
    assert system_prompt == request.system_prompt
    assert user_prompt == request.user_prompt
    assert kwargs == {
        "temperature": request.temperature, "max_tokens": request.max_tokens,
        "task_id": request.task_id, "stage": request.stage,
    }


def _three_scene_global_plan():
    data = _global_plan_dict()
    data["arc"] = ["s_001", "s_002", "s_003"]
    data["motifs"][0]["returns_in"] = ["s_002", "s_003"]
    data["scenes"][0]["link_next"] = "s_002"
    data["scenes"][1]["link_next"] = "s_003"
    data["scenes"].append({
        "scene_id": "s_003", "narrative_role": "resolution",
        "purpose": "Сформулировать практический вывод",
        "visual_intent": "Вернуть визуальный мотив и показать достигнутый результат",
        "relation_to_previous": "resolves", "develops_scene_id": "s_001",
        "motif_ids": ["motif_capital"], "link_prev": "s_002", "link_next": None,
    })
    return data


def _three_scene_storyboard():
    data = _storyboard()
    data["scenes"].append({
        "id": "s_003", "block_id": "block_01", "meaning": "Финальный вывод",
        "purpose": "Закрепить основную мысль", "voice_text": "Итак, это итог.",
    })
    return data


def test_global_plan_must_meet_developing_share_before_scene_pass():
    storyboard = _three_scene_storyboard()
    data = _three_scene_global_plan()
    data["scenes"][1]["relation_to_previous"] = "introduces"
    data["scenes"][1]["develops_scene_id"] = None
    data["scenes"][1]["motif_ids"] = []
    data["motifs"][0]["returns_in"] = ["s_003"]
    plan = VisualStoryPlan.model_validate(data)
    with pytest.raises(ValueError, match="insufficiently connected"):
        validate_global_plan_coverage(storyboard, plan)


def test_global_plan_requires_real_recurring_motif_for_three_or_more_scenes():
    storyboard = _three_scene_storyboard()
    data = _three_scene_global_plan()
    data["motifs"][0]["returns_in"] = []
    data["scenes"][1]["motif_ids"] = []
    data["scenes"][2]["motif_ids"] = []
    plan = VisualStoryPlan.model_validate(data)
    with pytest.raises(ValueError, match="genuinely recurring motif"):
        validate_global_plan_coverage(storyboard, plan)


def test_scene_builder_refuses_global_plan_that_fails_global_policy():
    storyboard = _three_scene_storyboard()
    data = _three_scene_global_plan()
    data["motifs"][0]["returns_in"] = []
    data["scenes"][1]["motif_ids"] = []
    data["scenes"][2]["motif_ids"] = []
    plan = VisualStoryPlan.model_validate(data)
    with pytest.raises(ValueError, match="genuinely recurring motif"):
        build_scene_composition_request(storyboard, plan, "s_001")


def test_public_prefix_validator_accepts_valid_prefix_and_rejects_bad_semantics():
    storyboard = _storyboard()
    plan = VisualStoryPlan.model_validate(_global_plan_dict())
    first_plan = _parsed_scene_plans()[0]
    assert validate_scene_composition_prefix(storyboard, plan, [first_plan]) is None

    broken = _scene_plan_dict()[0]
    broken["compositions"][0]["layers"][1]["motif_id"] = None
    broken_plan = SceneCompositionPlan.model_validate(broken)
    with pytest.raises(ValueError, match="failed semantic validation"):
        validate_scene_composition_prefix(storyboard, plan, [broken_plan])


def test_both_response_schemas_serialize_for_structured_llm_router():
    for schema_model in (VisualStoryPlan, SceneCompositionPlan):
        schema = schema_model.model_json_schema()
        encoded = json.dumps(schema, ensure_ascii=False)
        assert encoded.startswith("{") and "properties" in schema

        def close_objects(node):
            if isinstance(node, dict):
                if node.get("type") == "object" and isinstance(node.get("properties"), dict):
                    node["additionalProperties"] = False
                    node["required"] = list(node["properties"].keys())
                    assert set(node["required"]) == set(node["properties"])
                for value in node.values():
                    close_objects(value)
            elif isinstance(node, list):
                for value in node:
                    close_objects(value)

        close_objects(schema)
        json.dumps(schema, ensure_ascii=False)
