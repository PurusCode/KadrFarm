import copy
from decimal import Decimal

import pytest
from pydantic import ValidationError

from kadr_farm.visual_story import (
    ApprovedFact,
    DataProvenance,
    SpeechAnchor,
    SpeechSegment,
    SceneCompositionPlan,
    VisualStoryPlan,
    continuity_report,
    eval_formula,
    resolve_action_schedule,
    resolve_provenance,
    snap_anchor,
    split_speech_segments,
    validate_plan,
)

B1 = "Сколько нужно капитала? Правило простое."
B2 = "Умножь расходы на двадцать пять. Получишь капитал."


def _measured(text, durations):
    spans = split_speech_segments(text)
    assert len(spans) == len(durations)
    return [
        SpeechSegment(
            text=span.text,
            char_start=span.char_start,
            char_end=span.char_end,
            duration_sec=duration,
            wav_sha256=("a" if index == 0 else "b") * 64,
        )
        for index, (span, duration) in enumerate(zip(spans, durations))
    ]


def _inputs():
    beat_texts = {"b1": B1, "b2": B2}
    segments = {"b1": split_speech_segments(B1), "b2": split_speech_segments(B2)}
    facts = {
        "fact_expenses": ApprovedFact(
            fact_id="fact_expenses", value="1200000", unit="RUB",
            source_url="https://example.org/expenses", source_title="Approved expense source",
            status="verified", verified_by_human=True,
        ),
        "fact_multiplier": ApprovedFact(
            fact_id="fact_multiplier", value="25", unit="1",
            source_url="https://example.org/method", source_title="Approved calculation method",
            status="verified", verified_by_human=True,
        ),
    }
    provenance = {
        "expenses": DataProvenance(
            value_id="expenses", kind="literal", value="1200000", unit="RUB",
            fact_id="fact_expenses",
        ),
        "multiplier": DataProvenance(
            value_id="multiplier", kind="literal", value="25", unit="1",
            fact_id="fact_multiplier",
        ),
        "capital": DataProvenance(
            value_id="capital", kind="derived", value="30000000", unit="RUB",
            formula="expenses * multiplier", inputs=["expenses", "multiplier"],
        ),
    }
    return beat_texts, segments, provenance, facts


def _plan_dict():
    return {
        "style": {"palette": ["#fdf3dc", "#e8862e"], "font_family": "Segoe UI"},
        "arc": ["s1", "s2"],
        "motifs": [{
            "motif_id": "capital_stack", "kind": "object",
            "description": "Растущий столбик капитала", "first_scene": "s1",
            "returns_in": ["s2"],
        }],
        "scenes": [
            {
                "scene_id": "s1", "narrative_role": "hook",
                "purpose": "Поставить вопрос о капитале",
                "visual_intent": "Показать исходную финансовую потребность как один наглядный объект",
                "relation_to_previous": "introduces", "develops_scene_id": None,
                "motif_ids": ["capital_stack"], "link_prev": None, "link_next": "s2",
            },
            {
                "scene_id": "s2", "narrative_role": "development",
                "purpose": "Показать математическую связь",
                "visual_intent": "Вернуть счётчик и превратить исходные расходы в расчёт капитала",
                "relation_to_previous": "extends", "develops_scene_id": "s1",
                "motif_ids": ["capital_stack"], "link_prev": "s1", "link_next": None,
            },
        ],
    }


def _scene_plans_dict():
    s1 = split_speech_segments(B1)
    s2 = split_speech_segments(B2)
    return [
        {
            "scene_id": "s1",
            "compositions": [{
                "composition_id": "comp_01", "scene_id": "s1", "develops": None,
                "relation": "introduces",
                "layers": [
                    {"object_id": "bg_desk", "zone": "bg", "primitive": "image", "z_order": 0},
                    {
                        "object_id": "cap_counter", "zone": "center", "primitive": "counter",
                        "z_order": 1, "motif_id": "capital_stack", "value_ids": ["expenses"],
                        "labels": ["Годовые расходы"],
                        "actions": [{
                            "action_id": "action_01", "kind": "appear", "target": "cap_counter",
                            "anchor": {"beat_id": "b1", "char_start": s1[0].char_start,
                                       "char_end": s1[0].char_end},
                            "params": {"kind": "appear"},
                        }],
                    },
                ],
                "entry_state": {}, "exit_state": {"cap_counter": "visible"},
            }],
        },
        {
            "scene_id": "s2",
            "compositions": [{
                "composition_id": "comp_02", "scene_id": "s2", "develops": "comp_01",
                "relation": "extends",
                "layers": [
                    {"object_id": "bg_desk_2", "zone": "bg", "primitive": "image", "z_order": 0},
                    {
                        "object_id": "cap_counter", "zone": "left", "primitive": "counter",
                        "z_order": 1, "motif_id": "capital_stack", "value_ids": ["expenses"],
                        "labels": ["Годовые расходы"],
                        "actions": [{
                            "action_id": "action_02", "kind": "highlight", "target": "cap_counter",
                            "anchor": {"beat_id": "b2", "char_start": s2[0].char_start,
                                       "char_end": s2[0].char_end},
                            "params": {"kind": "highlight", "color": "#e8862e"},
                        }],
                    },
                    {
                        "object_id": "capital_formula", "zone": "right", "primitive": "formula",
                        "z_order": 2, "motif_id": "capital_stack",
                        "value_ids": ["expenses", "multiplier", "capital"],
                        "labels": ["Расходы", "x25", "Капитал"],
                        "actions": [
                            {
                                "action_id": "action_03", "kind": "appear", "target": "capital_formula",
                                "anchor": {"beat_id": "b2", "char_start": s2[0].char_start,
                                           "char_end": s2[0].char_end},
                                "params": {"kind": "appear"},
                            },
                            {
                                "action_id": "action_04", "kind": "update_value", "target": "capital_formula",
                                "anchor": {"beat_id": "b2", "char_start": s2[1].char_start,
                                           "char_end": s2[1].char_end},
                                "params": {"kind": "update_value", "value_id": "capital"},
                            },
                        ],
                    },
                ],
                "entry_state": {"cap_counter": "visible"},
                "exit_state": {"cap_counter": "visible", "capital_formula": "visible"},
            }],
        },
    ]


def _validate(data=None, scene_plans=None, **overrides):
    beat_texts, segments, provenance, facts = _inputs()
    plan = VisualStoryPlan.model_validate(data or _plan_dict())
    raw_scene_plans = scene_plans if scene_plans is not None else _scene_plans_dict()
    parsed_scene_plans = [SceneCompositionPlan.model_validate(item) for item in raw_scene_plans]
    params = {
        "expected_scene_ids": ["s1", "s2"],
        "beat_texts": beat_texts,
        "beat_order": ["b1", "b2"],
        "segments": segments,
        "scene_beat_ids": {"s1": ["b1"], "s2": ["b2"]},
        "provenance": provenance,
        "facts": facts,
    }
    params.update(overrides)
    return plan, parsed_scene_plans, validate_plan(plan, parsed_scene_plans, **params)


def test_valid_plan_passes_and_reports_continuity():
    plan, scene_plans, errors = _validate()
    assert errors == []
    report = continuity_report(plan, scene_plans)
    assert report["developing_composition_share"] == 0.5
    assert report["scene_developing_share"] == 0.5
    assert report["motifs_reused"] == 1


def test_scene_coverage_rejects_missing_extra_or_reordered_scenes():
    plan, scene_plans, errors = _validate(expected_scene_ids=["s1", "s2", "s3"])
    assert any("whole-video scene coverage/order mismatch" in error for error in errors)
    data = _plan_dict()
    data["scenes"].reverse()
    with pytest.raises(ValidationError):
        VisualStoryPlan.model_validate(data)


def test_composition_must_belong_to_containing_scene():
    scene_plans = _scene_plans_dict()
    scene_plans[1]["compositions"][0]["scene_id"] = "s1"
    with pytest.raises(ValidationError, match="does not match SceneCompositionPlan"):
        SceneCompositionPlan.model_validate(scene_plans[1])


def test_independent_pictures_rejected_by_continuity_policy():
    scene_plans = _scene_plans_dict()
    second = scene_plans[1]["compositions"][0]
    second["develops"] = None
    second["relation"] = "introduces"
    second["entry_state"] = {}
    plan, parsed_scene_plans, errors = _validate(scene_plans=scene_plans)
    assert any("developing composition share" in error for error in errors)
    assert any("no composition develops an earlier scene" in error for error in errors)


def test_dangling_develops_rejected():
    scene_plans = _scene_plans_dict()
    scene_plans[1]["compositions"][0]["develops"] = "comp_99"
    plan, parsed_scene_plans, errors = _validate(scene_plans=scene_plans)
    assert any("develops references missing composition" in error for error in errors)


def test_motif_must_be_declared_in_whole_video_plan_for_every_return_scene():
    data = _plan_dict()
    data["scenes"][1]["motif_ids"] = []
    with pytest.raises(ValidationError, match="do not match scene-direction assignments"):
        VisualStoryPlan.model_validate(data)


def test_unknown_motif_reference_rejected():
    data = _plan_dict()
    data["scenes"][1]["motif_ids"] = ["unknown_motif"]
    with pytest.raises(ValidationError, match="unknown motifs"):
        VisualStoryPlan.model_validate(data)


def test_motif_return_scenes_must_be_ordered_and_unique():
    data = _plan_dict()
    data["motifs"][0]["returns_in"] = ["s2", "s2"]
    with pytest.raises(ValidationError):
        VisualStoryPlan.model_validate(data)


def test_anchor_crossing_tts_unit_rejected():
    scene_plans = _scene_plans_dict()
    scene_plans[0]["compositions"][0]["layers"][1]["actions"][0]["anchor"]["char_end"] = 10
    plan, parsed_scene_plans, errors = _validate(scene_plans=scene_plans)
    assert any("whole measured/text speech units" in error for error in errors)


def test_anchor_must_use_a_beat_belonging_to_its_scene():
    scene_plans = _scene_plans_dict()
    action = scene_plans[0]["compositions"][0]["layers"][1]["actions"][0]
    action["anchor"]["beat_id"] = "b2"
    plan, parsed_scene_plans, errors = _validate(scene_plans=scene_plans)
    assert any("does not belong to scene s1" in error for error in errors)


def test_speech_segments_reconstruct_text_and_respect_abbreviations_and_decimals():
    samples = [
        B1,
        B2,
        "Одна фраза без точки",
        "А! Б? В…",
        "Цена 3.14 руб. Далее текст.",
        "Это т.е. сокращение. А это новое предложение.",
        "Он сказал: «Готово!» Затем ушёл.",
    ]
    for text in samples:
        spans = split_speech_segments(text)
        assert "".join(segment.text for segment in spans) == text
        assert spans[0].char_start == 0
        assert spans[-1].char_end == len(text)
        assert all(a.char_end == b.char_start for a, b in zip(spans, spans[1:]))
    assert len(split_speech_segments("Цена 3.14 руб. Далее текст.")) == 2
    assert len(split_speech_segments("Это т.е. сокращение. А это новое предложение.")) == 2
    quoted = split_speech_segments("Он сказал: «Готово!» Затем ушёл.")
    assert quoted[0].text.endswith("!» ")


def test_snap_anchor_expands_to_whole_units_and_rejects_out_of_range():
    spans = split_speech_segments(B1)
    anchor = snap_anchor("b1", 3, 12, spans)
    assert anchor.char_start == spans[0].char_start
    assert anchor.char_end == spans[0].char_end
    with pytest.raises(ValueError, match="outside"):
        snap_anchor("b1", len(B1) + 1, len(B1) + 2, spans)


def test_segment_coverage_rejects_gaps_and_text_mismatch():
    text = "Первая. Вторая."
    beat_texts = {"b1": text, "b2": B2}
    segments = {"b1": split_speech_segments(text), "b2": split_speech_segments(B2)}
    broken = list(segments["b1"])
    broken[1] = broken[1].model_copy(update={"char_start": broken[1].char_start + 1})
    _, _, errors = _validate(beat_texts=beat_texts, segments={**segments, "b1": broken})
    assert any("leaves a gap or overlaps" in error for error in errors)


def test_literal_number_must_match_external_approved_fact():
    _, _, provenance, facts = _inputs()
    bad = copy.deepcopy(provenance)
    bad["expenses"] = bad["expenses"].model_copy(update={"value": "1300000"})
    with pytest.raises(ValueError, match="does not exactly match approved fact"):
        resolve_provenance(bad, facts)


def test_missing_approved_fact_rejected():
    _, _, provenance, _ = _inputs()
    with pytest.raises(ValueError, match="missing approved fact"):
        resolve_provenance(provenance, {})


def test_derived_provenance_recomputes_exactly():
    _, _, provenance, facts = _inputs()
    resolved = resolve_provenance(provenance, facts)
    assert resolved["capital"].value == Decimal("30000000")
    assert resolved["capital"].unit == "RUB"
    assert resolved["capital"].status == "derived_verified"
    assert resolved["capital"].fact_ids == ("fact_expenses", "fact_multiplier")


def test_derived_value_mismatch_rejected():
    _, _, provenance, facts = _inputs()
    bad = copy.deepcopy(provenance)
    bad["capital"] = bad["capital"].model_copy(update={"value": "31000000"})
    with pytest.raises(ValueError, match="does not recompute exactly"):
        resolve_provenance(bad, facts)


def test_incompatible_units_in_addition_rejected():
    _, _, provenance, facts = _inputs()
    bad = copy.deepcopy(provenance)
    bad["capital"] = DataProvenance(
        value_id="capital", kind="derived", value="1200025", unit="RUB",
        formula="expenses + multiplier", inputs=["expenses", "multiplier"],
    )
    with pytest.raises(ValueError, match="compatible units"):
        resolve_provenance(bad, facts)


def test_derived_unit_mismatch_rejected():
    _, _, provenance, facts = _inputs()
    bad = copy.deepcopy(provenance)
    bad["capital"] = bad["capital"].model_copy(update={"unit": "USD"})
    with pytest.raises(ValueError, match="unit mismatch"):
        resolve_provenance(bad, facts)


def test_provenance_cycle_rejected():
    cycle = {
        "value_aa": DataProvenance(
            value_id="value_aa", kind="derived", value="1", unit="1",
            formula="value_bb", inputs=["value_bb"],
        ),
        "value_bb": DataProvenance(
            value_id="value_bb", kind="derived", value="1", unit="1",
            formula="value_aa", inputs=["value_aa"],
        ),
    }
    with pytest.raises(ValueError, match="cyclic"):
        resolve_provenance(cycle, {})


def test_non_finite_and_unsafe_formula_inputs_rejected():
    with pytest.raises(ValidationError):
        DataProvenance(value_id="value_aa", kind="literal", value="NaN", unit="1", fact_id="fact_aa")
    with pytest.raises(ValueError, match="division by zero|invalid arithmetic"):
        eval_formula("10 / 0", {})
    with pytest.raises(ValueError, match="unsupported formula node"):
        eval_formula("__import__('os').system('whoami')", {})
    with pytest.raises(ValueError, match="numeric constants"):
        eval_formula("True + 1", {})
    with pytest.raises(ValueError, match="finite Decimal"):
        eval_formula("source_value", {"source_value": Decimal("NaN")})


def test_formula_must_use_exactly_declared_inputs():
    _, _, provenance, facts = _inputs()
    bad = copy.deepcopy(provenance)
    bad["capital"] = bad["capital"].model_copy(update={"inputs": ["expenses"]})
    with pytest.raises(ValueError, match="do not match declared inputs"):
        resolve_provenance(bad, facts)


def test_typed_action_params_reject_wrong_kind_and_extra_keys():
    data = _scene_plans_dict()
    action = data[0]["compositions"][0]["layers"][1]["actions"][0]
    action["params"] = {"kind": "highlight"}
    with pytest.raises(ValidationError):
        SceneCompositionPlan.model_validate(data[0])
    data = _scene_plans_dict()
    action = data[0]["compositions"][0]["layers"][1]["actions"][0]
    action["params"] = {"kind": "appear", "pixel_x": 10}
    with pytest.raises(ValidationError):
        SceneCompositionPlan.model_validate(data[0])


def test_model_timestamps_are_forbidden():
    data = _scene_plans_dict()
    data[0]["compositions"][0]["layers"][1]["actions"][0]["start_sec"] = 1.0
    with pytest.raises(ValidationError):
        SceneCompositionPlan.model_validate(data[0])


def test_opaque_overlapping_layer_cannot_cover_text():
    data = _scene_plans_dict()
    data[1]["compositions"][0]["layers"].append({
        "object_id": "overlay_box", "zone": "right", "primitive": "callout",
        "z_order": 3, "labels": ["Поверх формулы"],
    })
    with pytest.raises(ValidationError, match="obscured"):
        SceneCompositionPlan.model_validate(data[1])


def test_scrim_or_transparent_overlap_is_explicitly_allowed():
    data = _scene_plans_dict()
    data[1]["compositions"][0]["layers"].append({
        "object_id": "overlay_box", "zone": "right", "primitive": "image",
        "z_order": 3, "overlap_policy": "scrim",
    })
    SceneCompositionPlan.model_validate(data[1])


def test_entry_state_must_match_developed_exit_state():
    data = _scene_plans_dict()
    data[1]["compositions"][0]["entry_state"] = {"cap_counter": "hidden"}
    plan, parsed_scene_plans, errors = _validate(scene_plans=data)
    assert any("does not match" in error for error in errors)


def test_extends_requires_a_real_carried_object():
    data = _scene_plans_dict()
    second = data[1]["compositions"][0]
    second["layers"][1]["object_id"] = "other_counter"
    second["layers"][1]["actions"][0]["target"] = "other_counter"
    second["entry_state"] = {"other_counter": "visible"}
    second["exit_state"] = {"capital_formula": "visible"}
    plan, parsed_scene_plans, errors = _validate(scene_plans=data)
    assert any("was not in developed exit_state" in error for error in errors)


def test_update_value_must_be_registered_on_layer():
    beat_texts, segments, provenance, facts = _inputs()
    data = _scene_plans_dict()
    action = data[1]["compositions"][0]["layers"][2]["actions"][1]
    action["params"]["value_id"] = "ghost"
    plan = VisualStoryPlan.model_validate(_plan_dict())
    parsed_scene_plans = [SceneCompositionPlan.model_validate(item) for item in data]
    errors = validate_plan(
        plan, parsed_scene_plans, expected_scene_ids=["s1", "s2"], beat_texts=beat_texts,
        beat_order=["b1", "b2"], segments=segments,
        scene_beat_ids={"s1": ["b1"], "s2": ["b2"]},
        provenance=provenance, facts=facts,
    )
    assert any("update_value ghost is not declared" in error for error in errors)


def test_row_reveal_requires_existing_table_row():
    data = _scene_plans_dict()
    layer = data[1]["compositions"][0]["layers"][2]
    layer["primitive"] = "table"
    layer["row_count"] = 2
    layer["actions"].append({
        "action_id": "action_05", "kind": "reveal_row", "target": "capital_formula",
        "anchor": {"beat_id": "b2", "char_start": 0, "char_end": len(B2)},
        "params": {"kind": "reveal_row", "row_index": 2},
    })
    with pytest.raises(ValidationError, match="existing table row"):
        SceneCompositionPlan.model_validate(data[1])


def test_global_schedule_accumulates_previous_beats_and_requires_real_wavs():
    plan = VisualStoryPlan.model_validate(_plan_dict())
    scene_plans = [SceneCompositionPlan.model_validate(item) for item in _scene_plans_dict()]
    measured = {
        "b1": _measured(B1, [1.0, 1.5]),
        "b2": _measured(B2, [2.0, 1.25]),
    }
    schedule = resolve_action_schedule(plan, scene_plans, beat_texts={"b1": B1, "b2": B2},
                                       segments=measured, beat_order=["b1", "b2"])
    by_id = {row["action_id"]: row for row in schedule}
    assert by_id["action_01"]["start_sec"] == 0.0
    assert by_id["action_02"]["start_sec"] == 2.5
    assert by_id["action_04"]["start_sec"] == 4.5
    assert by_id["action_04"]["end_sec"] == 5.75


def test_schedule_rejects_unmeasured_wav_units():
    plan = VisualStoryPlan.model_validate(_plan_dict())
    scene_plans = [SceneCompositionPlan.model_validate(item) for item in _scene_plans_dict()]
    spans = {"b1": split_speech_segments(B1), "b2": split_speech_segments(B2)}
    with pytest.raises(ValueError, match="without measured WAV metadata"):
        resolve_action_schedule(plan, scene_plans, beat_texts={"b1": B1, "b2": B2},
                                segments=spans, beat_order=["b1", "b2"])


def test_schedule_rejects_missing_or_invalid_durations():
    plan = VisualStoryPlan.model_validate(_plan_dict())
    scene_plans = [SceneCompositionPlan.model_validate(item) for item in _scene_plans_dict()]
    measured = {
        "b1": _measured(B1, [1.0, 1.5]),
        "b2": _measured(B2, [2.0, 1.25]),
    }
    with pytest.raises(ValueError, match="beat_order"):
        resolve_action_schedule(plan, scene_plans, beat_texts={"b1": B1, "b2": B2},
                                segments=measured, beat_order=["b1"])


def test_actions_are_ordered_by_beat_then_character_offset():
    scene_plans = _scene_plans_dict()
    actions = scene_plans[1]["compositions"][0]["layers"][2]["actions"]
    actions.reverse()
    plan, parsed_scene_plans, errors = _validate(scene_plans=scene_plans)
    assert any("global speech order" in error for error in errors)


def test_derived_value_preserves_unverified_source_status():
    _, _, provenance, facts = _inputs()
    facts["fact_expenses"] = facts["fact_expenses"].model_copy(
        update={"status": "review_required", "verified_by_human": False}
    )
    resolved = resolve_provenance(provenance, facts)
    assert resolved["capital"].status == "review_required"


def test_approved_fact_requires_a_real_absolute_http_url():
    with pytest.raises(ValidationError, match=r"absolute HTTP\(S\) URL"):
        ApprovedFact(
            fact_id="fact_aa", value="10", unit="RUB", source_url="https://bad url.example/source",
            source_title="A source title", status="review_required",
        )


def test_count_up_values_must_have_compatible_units():
    beat_texts, segments, provenance, facts = _inputs()
    data = _scene_plans_dict()
    counter = data[0]["compositions"][0]["layers"][1]
    counter["value_ids"] = ["expenses", "multiplier"]
    counter["actions"].append({
        "action_id": "action_05", "kind": "count_up", "target": "cap_counter",
        "anchor": {"beat_id": "b1", "char_start": 0, "char_end": len(B1)},
        "params": {"kind": "count_up", "from_id": "expenses", "to_id": "multiplier"},
    })
    plan = VisualStoryPlan.model_validate(_plan_dict())
    parsed_scene_plans = [SceneCompositionPlan.model_validate(item) for item in data]
    errors = validate_plan(
        plan, parsed_scene_plans, expected_scene_ids=["s1", "s2"], beat_texts=beat_texts,
        beat_order=["b1", "b2"], segments=segments,
        scene_beat_ids={"s1": ["b1"], "s2": ["b2"]},
        provenance=provenance, facts=facts,
    )
    assert any("count_up values must use compatible units" in error for error in errors)


def test_entry_exit_states_reject_unknown_state_values():
    data = _scene_plans_dict()
    data[0]["compositions"][0]["exit_state"] = {"cap_counter": "teleported"}
    with pytest.raises(ValidationError):
        SceneCompositionPlan.model_validate(data[0])


def test_schedule_rejects_segments_that_do_not_match_source_text():
    plan = VisualStoryPlan.model_validate(_plan_dict())
    scene_plans = [SceneCompositionPlan.model_validate(item) for item in _scene_plans_dict()]
    measured = {
        "b1": _measured(B1, [1.0, 1.5]),
        "b2": _measured(B2, [2.0, 1.25]),
    }
    broken = list(measured["b1"])
    broken[0] = broken[0].model_copy(update={"text": "not the same text"})
    measured["b1"] = broken
    with pytest.raises(ValueError, match="does not match source text"):
        resolve_action_schedule(
            plan, scene_plans, beat_texts={"b1": B1, "b2": B2},
            segments=measured, beat_order=["b1", "b2"],
        )


def test_second_pass_must_provide_a_plan_for_every_scene():
    scene_plans = _scene_plans_dict()[:1]
    plan, parsed_scene_plans, errors = _validate(scene_plans=scene_plans)
    assert any("scene-composition coverage/order mismatch" in error for error in errors)


def test_second_pass_cannot_invent_a_motif_not_in_the_global_plan():
    scene_plans = _scene_plans_dict()
    scene_plans[1]["compositions"][0]["layers"][1]["motif_id"] = None
    scene_plans[1]["compositions"][0]["layers"][2]["motif_id"] = None
    plan, parsed_scene_plans, errors = _validate(scene_plans=scene_plans)
    assert any("motif usage" in error for error in errors)


def test_global_direction_requires_prior_scene_reference_for_developing_relation():
    data = _plan_dict()
    data["scenes"][1]["develops_scene_id"] = "scene_99"
    with pytest.raises(ValidationError, match="earlier scene"):
        VisualStoryPlan.model_validate(data)


def test_global_direction_rejects_too_many_independent_scene_introductions():
    data = _plan_dict()
    data["scenes"][1]["relation_to_previous"] = "introduces"
    data["scenes"][1]["develops_scene_id"] = None
    plan, scene_plans, errors = _validate(data)
    assert any("whole-video direction is insufficiently connected" in error for error in errors)


def test_duplicate_composition_ids_across_scene_plans_rejected():
    scene_plans = _scene_plans_dict()
    scene_plans[1]["compositions"][0]["composition_id"] = "comp_01"
    plan, parsed_scene_plans, errors = _validate(scene_plans=scene_plans)
    assert any("duplicate composition_id across scene plans" in error for error in errors)


def test_review_required_fact_blocks_visual_numeric_use():
    beat_texts, segments, provenance, facts = _inputs()
    facts["fact_expenses"] = facts["fact_expenses"].model_copy(
        update={"status": "review_required", "verified_by_human": False}
    )
    plan, scene_plans, errors = _validate(provenance=provenance, facts=facts)
    assert any("blocked until its source is human-reviewed" in error for error in errors)


def test_editorial_estimate_requires_visible_qualification():
    beat_texts, segments, provenance, facts = _inputs()
    facts["fact_expenses"] = facts["fact_expenses"].model_copy(
        update={"status": "editorial_estimate", "verified_by_human": False}
    )
    plan, scene_plans, errors = _validate(provenance=provenance, facts=facts)
    assert any("requires a visible value_qualification" in error for error in errors)
    # The qualification is explicit in the scene plan and must be rendered beside the value.
    scene_data = _scene_plans_dict()
    scene_data[0]["compositions"][0]["layers"][1]["value_qualifications"] = {
        "expenses": {"kind": "estimate", "text": "примерная оценка"}
    }
    scene_data[1]["compositions"][0]["layers"][1]["value_qualifications"] = {
        "expenses": {"kind": "estimate", "text": "примерная оценка"}
    }
    scene_data[1]["compositions"][0]["layers"][2]["value_qualifications"] = {
        "expenses": {"kind": "estimate", "text": "примерная оценка"},
        "capital": {"kind": "estimate", "text": "расчётная оценка"},
    }
    plan, parsed_scene_plans, errors = _validate(scene_plans=scene_data, provenance=provenance, facts=facts)
    assert not any("requires a visible value_qualification" in error for error in errors)
