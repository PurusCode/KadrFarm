from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from kadr_farm.visual_capability_registry_v2 import (
    PILLOW_RENDERER_ID,
    PROFESSIONAL_RENDERER_ID,
)
from kadr_farm.visual_story_contract_v2 import (
    ExecutionOutputSpec,
    SceneCompositionPlan,
    VisualStoryPlan,
)
from kadr_farm.visual_story_execution_plan_compiler_v2 import (
    CheckpointMismatchError,
    CompilationInputError,
    CompilationCheckpoint,
    compile_visual_story_to_execution_plan,
    serialize_compilation_result,
    validate_checkpoint_compatibility,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _base_bundle(objects=None, events=None, *, renderer=PROFESSIONAL_RENDERER_ID):
    voice_text = "Нейросеть может ошибаться даже уверенно."
    storyboard = {
        "title": "Тестовый ролик",
        "scenes": [{"id": "s_001", "voice_text": voice_text}],
    }
    canonical_storyboard = json.dumps(
        storyboard, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    global_plan = VisualStoryPlan.model_validate({
        "plan_id": "global_main",
        "title": "Тестовый ролик",
        "storyboard_sha256": _sha(canonical_storyboard),
        "visual_thesis": "Уверенность не заменяет проверку фактов.",
        "arc_summary": "От первого впечатления переходим к проверке источника.",
        "style": {"visual_language": "editorial minimalism"},
        "scenes": [{
            "scene_id": "s_001",
            "narrative_role": "hook",
            "visual_purpose": "Показать уверенную подачу ответа.",
            "visual_strategy": "Сопоставить ответ и визуальные признаки проверки.",
            "relation_to_previous": {"kind": "introduces", "reason": "Начало истории."},
        }],
    })
    if objects is None:
        objects = [{
            "object_id": "headline",
            "kind": "text",
            "content": {"text": "Проверяйте источники", "semantic_role": "headline"},
        }]
    anchor = {
        "anchor_id": "anchor_001",
        "source_scene_id": "s_001",
        "char_start": 0,
        "char_end": len(voice_text),
        "text_excerpt": voice_text,
        "role": "opening",
    }
    scene_plan = SceneCompositionPlan.model_validate({
        "scene_id": "s_001",
        "source_text_sha256": _sha(voice_text),
        "scene_purpose": "Показать необходимость проверки.",
        "shots": [{
            "shot_id": "shot_001",
            "editorial_purpose": "Ввести тему проверки.",
            "visual_summary": "Заголовок и один визуальный объект.",
            "speech_anchors": [anchor],
            "composition": {
                "layout_intent": "Заголовок располагается в центре композиции.",
                "objects": objects,
            },
            "events": events or [],
        }],
    })
    output_spec = ExecutionOutputSpec(width=1920, height=1080, fps=25)
    return storyboard, global_plan, [scene_plan], output_spec


def _compile(bundle, **kwargs):
    storyboard, global_plan, scene_plans, output_spec = bundle
    options = {
        "storyboard": storyboard,
        "global_plan": global_plan,
        "scene_plans": scene_plans,
        "run_id": "run_001",
        "renderer_id": PROFESSIONAL_RENDERER_ID,
        "output_spec": output_spec,
    }
    options.update(kwargs)
    return compile_visual_story_to_execution_plan(**options)


def test_minimal_bundle_compiles_to_ready_plan_deterministically():
    bundle = _base_bundle()
    first = _compile(bundle)
    second = _compile(bundle)

    assert first.execution_plan.status == "ready"
    assert len(first.execution_plan.render_tasks) == 1
    assert first.execution_plan.render_tasks[0].object_id == "shot_composition"
    assert first.execution_plan.render_tasks[0].expected_media_type == "video/mp4"
    assert first.execution_plan.model_dump(mode="json") == second.execution_plan.model_dump(mode="json")
    assert first.checkpoint.model_dump(mode="json") == second.checkpoint.model_dump(mode="json")
    assert first.execution_plan_sha256 == second.execution_plan_sha256


def test_compiler_requires_explicit_renderer_target():
    with pytest.raises(TypeError):
        compile_visual_story_to_execution_plan(**{})


def test_unknown_renderer_is_rejected_without_fallback():
    with pytest.raises(CompilationInputError, match="Unknown renderer_id"):
        _compile(_base_bundle(), renderer_id="local_fallback_renderer")


def test_image_creates_explicit_asset_request_and_provider_allowlist():
    image = {
        "object_id": "desk_photo",
        "kind": "image",
        "content": {
            "subject": "Ноутбук на рабочем столе",
            "scene": "Современный офис",
            "action": "Человек проверяет текст",
            "search_queries": ["person reviewing text on laptop"],
            "orientation": "landscape",
            "crop_intent": "Экран и руки должны оставаться видимыми.",
        },
    }
    result = _compile(_base_bundle([image]), asset_provider_allowlist=["pixabay"])
    assert result.execution_plan.status == "ready"
    assert len(result.execution_plan.asset_requests) == 1
    request = result.execution_plan.asset_requests[0]
    assert request.media_kind == "image"
    assert request.provider_allowlist == ["pixabay"]
    assert request.search_queries == ["person reviewing text on laptop"]
    assert request.request_id.startswith("asset_")
    assert len(result.asset_bindings) == 1
    assert result.asset_bindings[0].task_id == result.execution_plan.render_tasks[0].task_id
    assert result.asset_bindings[0].asset_request_ids == [request.request_id]


def test_video_asset_request_preserves_qualitative_duration():
    video = {
        "object_id": "desk_video",
        "kind": "video",
        "content": {
            "subject": "Человек за ноутбуком",
            "search_queries": ["person typing on laptop"],
            "orientation": "landscape",
            "crop_intent": "Ноутбук и руки в кадре.",
            "desired_action": "Человек просматривает текст на экране",
            "desired_duration": "medium",
        },
    }
    result = _compile(_base_bundle([video]), asset_provider_allowlist=["pixabay"])
    request = result.execution_plan.asset_requests[0]
    assert request.media_kind == "video"
    assert request.desired_duration == "medium"


def test_media_without_provider_allowlist_is_blocked():
    image = {
        "object_id": "photo",
        "kind": "image",
        "content": {
            "subject": "Камера на столе",
            "search_queries": ["camera on desk"],
            "orientation": "landscape",
            "crop_intent": "Камера целиком в кадре.",
        },
    }
    result = _compile(_base_bundle([image]))
    assert result.execution_plan.status == "blocked"
    assert any(d.code == "asset_provider_allowlist_required" for d in result.execution_plan.diagnostics)
    assert result.execution_plan.asset_requests == []


def test_duplicate_provider_allowlist_is_blocked():
    image = {
        "object_id": "photo",
        "kind": "image",
        "content": {
            "subject": "Камера на столе",
            "search_queries": ["camera on desk"],
            "orientation": "landscape",
            "crop_intent": "Камера целиком в кадре.",
        },
    }
    result = _compile(_base_bundle([image]), asset_provider_allowlist=["pixabay", "pixabay"])
    assert result.execution_plan.status == "blocked"
    assert any(d.code == "invalid_asset_provider_allowlist" for d in result.execution_plan.diagnostics)
    assert result.execution_plan.asset_requests == []


def test_pillow_tasks_advertise_png_intermediate_output():
    result = _compile(_base_bundle(), renderer_id=PILLOW_RENDERER_ID)
    assert result.execution_plan.status == "ready"
    assert result.execution_plan.render_tasks[0].expected_media_type == "image/png"
    assert result.execution_plan.output_spec.container == "mp4"


def test_group_and_children_compile_to_one_shot_composition_task():
    objects = [
        {
            "object_id": "header_group",
            "kind": "group",
            "content": {"child_object_ids": ["header_text"], "local_layout": "horizontal"},
        },
        {
            "object_id": "header_text",
            "kind": "text",
            "content": {"text": "Заголовок", "semantic_role": "headline"},
        },
    ]
    plan = _compile(_base_bundle(objects)).execution_plan
    assert plan.status == "ready"
    assert len(plan.render_tasks) == 1
    task = plan.render_tasks[0]
    assert task.object_id == "shot_composition"
    assert task.shot_id == "shot_001"
    assert task.depends_on == []


def test_task_fingerprint_changes_when_output_spec_changes():
    bundle = _base_bundle()
    first = _compile(bundle)
    storyboard, global_plan, scene_plans, output_spec = bundle
    changed = ExecutionOutputSpec(width=1280, height=720, fps=25)
    second = _compile((storyboard, global_plan, scene_plans, changed))
    assert first.execution_plan.render_tasks[0].input_fingerprint != second.execution_plan.render_tasks[0].input_fingerprint


def test_task_fingerprint_is_independent_of_run_id():
    bundle = _base_bundle()
    first = _compile(bundle, run_id="run_alpha")
    second = _compile(bundle, run_id="run_beta")
    assert first.execution_plan.render_tasks[0].input_fingerprint == second.execution_plan.render_tasks[0].input_fingerprint
    assert first.execution_plan.run_id != second.execution_plan.run_id


def test_storyboard_hash_mismatch_returns_blocked_plan_without_tasks():
    bundle = list(_base_bundle())
    bundle[0] = {"title": "Изменённый ролик", "scenes": bundle[0]["scenes"]}
    result = _compile(tuple(bundle))
    assert result.execution_plan.status == "blocked"
    assert result.execution_plan.render_tasks == []
    assert any(d.code == "global_plan_storyboard_mismatch" for d in result.execution_plan.diagnostics)


def test_scene_plan_coverage_mismatch_returns_blocked_plan():
    bundle = list(_base_bundle())
    original = bundle[2][0].model_dump(mode="json")
    original["scene_id"] = "s_002"
    original["shots"][0]["speech_anchors"][0]["source_scene_id"] = "s_002"
    bundle[2] = [SceneCompositionPlan.model_validate(original)]
    result = _compile(tuple(bundle))
    assert result.execution_plan.status == "blocked"
    assert result.execution_plan.render_tasks == []
    assert any(d.code == "scene_plan_coverage_mismatch" for d in result.execution_plan.diagnostics)


def test_scene_text_hash_mismatch_returns_blocked_plan():
    bundle = list(_base_bundle())
    original = bundle[2][0]
    payload = original.model_dump(mode="json")
    payload["source_text_sha256"] = "f" * 64
    bundle[2] = [SceneCompositionPlan.model_validate(payload)]
    result = _compile(tuple(bundle))
    assert result.execution_plan.status == "blocked"
    assert result.execution_plan.render_tasks == []
    assert any(d.code == "scene_plan_coverage_mismatch" for d in result.execution_plan.diagnostics)


def test_continuity_reference_to_unknown_scene_blocks_compilation():
    bundle = list(_base_bundle())
    scene_payload = bundle[2][0].model_dump(mode="json")
    shot = scene_payload["shots"][0]
    shot["continuity"] = {
        "outgoing": [{
            "relation": "contrast",
            "reference": {"scene_id": "s_999"},
            "reason": "Ссылка на отсутствующую сцену для теста.",
        }],
        "incoming": [],
    }
    bundle[2] = [SceneCompositionPlan.model_validate(scene_payload)]
    result = _compile(tuple(bundle))
    assert result.execution_plan.status == "blocked"
    assert any(d.code == "continuity_reference_invalid" for d in result.execution_plan.diagnostics)


def test_unsupported_pillow_area_chart_is_blocked_without_substitution():
    chart = {
        "object_id": "area_chart",
        "kind": "chart",
        "content": {
            "chart_type": "area",
            "data_mode": "illustrative",
            "categories": ["A", "B"],
            "series": [{"series_id": "series_a", "name": "Ряд", "illustrative_values": [1.0, 2.0]}],
            "illustrative_label": "Условный пример",
        },
    }
    result = _compile(_base_bundle([chart]), renderer_id=PILLOW_RENDERER_ID)
    assert result.execution_plan.status == "blocked"
    assert result.execution_plan.render_tasks == []
    assert any(d.code == "unsupported_subtype" for d in result.execution_plan.diagnostics)


def test_non_exact_authoring_action_is_blocked_not_aliased():
    graph = {
        "object_id": "graph",
        "kind": "node_graph",
        "content": {
            "layout": "left_to_right",
            "nodes": [
                {"node_id": "node_a", "label": "Источник"},
                {"node_id": "node_b", "label": "Проверка"},
            ],
            "edges": [{"edge_id": "edge_a", "from_node_id": "node_a", "to_node_id": "node_b"}],
        },
    }
    event = {
        "event_id": "draw_edge_event",
        "action": "draw_edge",
        "target": {"object_id": "graph", "subtarget_id": "edge_a"},
        "trigger": {"anchor_id": "anchor_001"},
    }
    result = _compile(_base_bundle([graph], [event]))
    assert result.execution_plan.status == "blocked"
    assert any(d.code == "unsupported_action_mapping" for d in result.execution_plan.diagnostics)


def test_stable_row_subtarget_is_blocked_by_runtime_registry():
    table = {
        "object_id": "table",
        "kind": "table",
        "content": {
            "data_mode": "illustrative",
            "columns": [{"column_id": "name", "title": "Название"}],
            "rows": [{"row_id": "row_a", "cells": [{"illustrative_value": "Пример"}]}],
            "reveal_mode": "row_by_row",
            "illustrative_label": "Условный пример",
        },
    }
    event = {
        "event_id": "reveal_row_event",
        "action": "reveal_row",
        "target": {"object_id": "table", "subtarget_id": "row_a"},
        "trigger": {"anchor_id": "anchor_001"},
    }
    result = _compile(_base_bundle([table], [event]))
    assert result.execution_plan.status == "blocked"
    assert any(d.code == "unsupported_action_mapping" for d in result.execution_plan.diagnostics)


def test_supported_direct_visibility_action_is_accepted():
    event = {
        "event_id": "fade_event",
        "action": "fade_in",
        "target": {"object_id": "headline"},
        "trigger": {"anchor_id": "anchor_001"},
    }
    result = _compile(_base_bundle(events=[event]))
    assert result.execution_plan.status == "ready"


def test_conflicting_visibility_events_block_compilation():
    events = [
        {
            "event_id": "fade_in_event",
            "action": "fade_in",
            "target": {"object_id": "headline"},
            "trigger": {"anchor_id": "anchor_001"},
        },
        {
            "event_id": "fade_out_event",
            "action": "fade_out",
            "target": {"object_id": "headline"},
            "trigger": {"anchor_id": "anchor_001"},
        },
    ]
    result = _compile(_base_bundle(events=events))
    assert result.execution_plan.status == "blocked"
    assert any(d.code == "conflicting_visibility_events" for d in result.execution_plan.diagnostics)


def test_highlight_style_parameters_that_runtime_cannot_preserve_are_blocked():
    event = {
        "event_id": "highlight_event",
        "action": "highlight",
        "tone": "warning",
        "intensity": "strong",
        "target": {"object_id": "headline"},
        "trigger": {"anchor_id": "anchor_001"},
    }
    result = _compile(_base_bundle(events=[event]))
    assert result.execution_plan.status == "blocked"
    assert any(d.code == "unsupported_action_parameters" for d in result.execution_plan.diagnostics)


def test_unresolved_external_value_refs_are_explicit_warnings():
    counter = {
        "object_id": "counter",
        "kind": "counter",
        "content": {"data_mode": "provided", "value_ref": "trusted.count"},
    }
    result = _compile(_base_bundle([counter]))
    assert result.execution_plan.status == "ready"
    warning = next(d for d in result.execution_plan.diagnostics if d.code == "external_value_refs_not_resolved")
    assert warning.severity == "warning"


def test_count_up_event_keeps_value_refs_but_marks_them_unresolved():
    counter = {
        "object_id": "counter",
        "kind": "counter",
        "content": {"data_mode": "provided", "value_ref": "trusted.count"},
    }
    event = {
        "event_id": "count_up_event",
        "action": "count_up",
        "target": {"object_id": "counter"},
        "trigger": {"anchor_id": "anchor_001"},
        "from_value_ref": "trusted.start",
        "to_value_ref": "trusted.end",
    }
    result = _compile(_base_bundle([counter], [event]))
    assert result.execution_plan.status == "ready"
    assert sum(d.code == "external_value_refs_not_resolved" for d in result.execution_plan.diagnostics) == 2


def test_checkpoint_matches_its_execution_plan():
    result = _compile(_base_bundle())
    validate_checkpoint_compatibility(result.execution_plan, result.checkpoint)


def test_checkpoint_mismatch_is_rejected():
    result = _compile(_base_bundle())
    changed = result.checkpoint.model_copy(update={"execution_plan_sha256": "f" * 64})
    with pytest.raises(CheckpointMismatchError, match="execution_plan_sha256"):
        validate_checkpoint_compatibility(result.execution_plan, changed)


def test_checkpoint_rejects_completed_task_ids_not_in_plan():
    with pytest.raises(ValidationError, match="unknown tasks"):
        CompilationCheckpoint(
            run_id="run_001",
            execution_plan_sha256="a" * 64,
            storyboard_sha256="b" * 64,
            global_plan_sha256="c" * 64,
            scene_plan_sha256={"s_001": "d" * 64},
            capability_registry_version="1.0.0",
            task_fingerprints={"task_a": "e" * 64},
            completed_task_ids=["task_missing"],
        )


def test_blocked_plan_with_no_supported_tasks_still_gets_checkpoint():
    unsupported_shape = {
        "object_id": "path_shape",
        "kind": "shape",
        "content": {"shape_type": "path"},
    }
    result = _compile(_base_bundle([unsupported_shape]), renderer_id=PILLOW_RENDERER_ID)
    assert result.execution_plan.status == "blocked"
    assert result.execution_plan.render_tasks == []
    assert result.checkpoint.task_fingerprints == {}
    validate_checkpoint_compatibility(result.execution_plan, result.checkpoint)


def test_canonical_serialization_is_stable_and_contains_no_generated_timestamps():
    bundle = _base_bundle()
    first = _compile(bundle)
    second = _compile(bundle)
    serialized = serialize_compilation_result(first)
    assert serialized == serialize_compilation_result(second)
    assert "timestamp" not in serialized
    assert json.loads(serialized)["execution_plan"]["status"] == "ready"


def test_invalid_run_id_is_rejected_by_execution_contract():
    with pytest.raises(ValidationError):
        _compile(_base_bundle(), run_id="not a valid id")


def test_invalid_provider_allowlist_type_is_rejected():
    with pytest.raises(CompilationInputError):
        _compile(_base_bundle(), asset_provider_allowlist="pixabay")


def test_duplicate_group_dependencies_do_not_get_emitted():
    # The frozen CP-01 contract rejects duplicate group children at model creation.
    objects = [
        {"object_id": "g", "kind": "group", "content": {"child_object_ids": ["t", "t"]}},
        {"object_id": "t", "kind": "text", "content": {"text": "Текст"}},
    ]
    with pytest.raises(ValidationError):
        # The frozen CP-01 schema rejects the malformed authoring object before compile.
        _base_bundle(objects)


def test_one_render_task_represents_whole_shot_not_each_object():
    objects = [
        {"object_id": "title", "kind": "text", "content": {"text": "Заголовок"}},
        {"object_id": "caption", "kind": "text", "content": {"text": "Пояснение"}},
    ]
    result = _compile(_base_bundle(objects))
    assert result.execution_plan.status == "ready"
    assert len(result.execution_plan.render_tasks) == 1
    assert result.execution_plan.render_tasks[0].object_id == "shot_composition"


def test_pillow_multi_series_bar_chart_is_blocked_by_cp02_guardrail():
    chart = {
        "object_id": "chart", "kind": "chart",
        "content": {
            "chart_type": "bar", "data_mode": "illustrative",
            "categories": ["A", "B"],
            "series": [
                {"series_id": "a", "name": "A", "illustrative_values": [1.0, 2.0]},
                {"series_id": "b", "name": "B", "illustrative_values": [2.0, 3.0]},
            ],
            "illustrative_label": "Условный пример",
        },
    }
    result = _compile(_base_bundle([chart]), renderer_id=PILLOW_RENDERER_ID)
    assert result.execution_plan.status == "blocked"
    assert result.execution_plan.render_tasks == []
    assert any(d.code == "unsupported_chart_series_count" for d in result.execution_plan.diagnostics)


def test_professional_multi_series_pie_is_blocked_by_cp02_guardrail():
    chart = {
        "object_id": "chart", "kind": "chart",
        "content": {
            "chart_type": "pie", "data_mode": "illustrative",
            "categories": ["A", "B"],
            "series": [
                {"series_id": "a", "name": "A", "illustrative_values": [1.0, 2.0]},
                {"series_id": "b", "name": "B", "illustrative_values": [2.0, 3.0]},
            ],
            "illustrative_label": "Условный пример",
        },
    }
    result = _compile(_base_bundle([chart]))
    assert result.execution_plan.status == "blocked"
    assert result.execution_plan.render_tasks == []
    assert any(d.code == "unsupported_chart_series_count" for d in result.execution_plan.diagnostics)


def test_move_event_is_blocked_until_semantic_geometry_lowering_exists():
    event = {
        "event_id": "move_event", "action": "move",
        "target": {"object_id": "headline"},
        "trigger": {"anchor_id": "anchor_001"},
        "destination_region": "left", "destination_anchor": "center_left",
    }
    result = _compile(_base_bundle(events=[event]))
    assert result.execution_plan.status == "blocked"
    assert result.execution_plan.render_tasks == []
    assert any(d.code == "unsupported_action_mapping" for d in result.execution_plan.diagnostics)


def test_scale_event_is_blocked_until_semantic_size_lowering_exists():
    event = {
        "event_id": "scale_event", "action": "scale",
        "target": {"object_id": "headline"},
        "trigger": {"anchor_id": "anchor_001"},
        "target_size": "hero",
    }
    result = _compile(_base_bundle(events=[event]))
    assert result.execution_plan.status == "blocked"
    assert result.execution_plan.render_tasks == []
    assert any(d.code == "unsupported_action_mapping" for d in result.execution_plan.diagnostics)


def test_explicitly_sequential_visibility_events_are_not_conflicts():
    events = [
        {
            "event_id": "fade_in_event", "action": "fade_in",
            "target": {"object_id": "headline"},
            "trigger": {"anchor_id": "anchor_001"},
            "timing": {"stagger": "sequential"},
        },
        {
            "event_id": "fade_out_event", "action": "fade_out",
            "target": {"object_id": "headline"},
            "trigger": {"anchor_id": "anchor_001"},
            "timing": {"stagger": "sequential"},
        },
    ]
    result = _compile(_base_bundle(events=events))
    assert result.execution_plan.status == "ready"
    assert not any(d.code == "conflicting_visibility_events" for d in result.execution_plan.diagnostics)


def test_event_targeting_group_child_is_blocked_for_professional_renderer():
    objects = [
        {"object_id": "g", "kind": "group", "content": {"child_object_ids": ["child"]}},
        {"object_id": "child", "kind": "text", "content": {"text": "Внутри группы"}},
    ]
    event = {
        "event_id": "child_fade", "action": "fade_in",
        "target": {"object_id": "child"},
        "trigger": {"anchor_id": "anchor_001"},
    }
    result = _compile(_base_bundle(objects, [event]))
    assert result.execution_plan.status == "blocked"
    assert result.execution_plan.render_tasks == []
    assert any(d.code == "nested_object_event_target_unsupported" for d in result.execution_plan.diagnostics)


def test_pillow_table_is_blocked_when_full_body_cannot_be_revealed():
    table = {
        "object_id": "table", "kind": "table",
        "content": {
            "data_mode": "illustrative",
            "columns": [{"column_id": "name", "title": "Название"}],
            "rows": [{"row_id": "row_a", "cells": [{"illustrative_value": "Пример"}]}],
            "reveal_mode": "all", "illustrative_label": "Учебный пример",
        },
    }
    result = _compile(_base_bundle([table]), renderer_id=PILLOW_RENDERER_ID)
    assert result.execution_plan.status == "blocked"
    assert result.execution_plan.render_tasks == []
    assert any(d.code == "pillow_progressive_state_unrepresentable" for d in result.execution_plan.diagnostics)


def test_pillow_progressive_line_shape_is_blocked_without_exact_draw_action():
    shape = {"object_id": "line", "kind": "shape", "content": {"shape_type": "line"}}
    result = _compile(_base_bundle([shape]), renderer_id=PILLOW_RENDERER_ID)
    assert result.execution_plan.status == "blocked"
    assert result.execution_plan.render_tasks == []
    assert any(d.code == "pillow_progressive_state_unrepresentable" for d in result.execution_plan.diagnostics)


def test_pillow_nested_group_media_beyond_direct_child_is_blocked():
    objects = [
        {"object_id": "outer", "kind": "group", "content": {"child_object_ids": ["inner"]}},
        {"object_id": "inner", "kind": "group", "content": {"child_object_ids": ["photo"]}},
        {
            "object_id": "photo", "kind": "image",
            "content": {
                "subject": "Ноутбук на столе", "search_queries": ["laptop on desk"],
                "orientation": "landscape", "crop_intent": "Экран остаётся видимым.",
            },
        },
    ]
    result = _compile(_base_bundle(objects), renderer_id=PILLOW_RENDERER_ID, asset_provider_allowlist=["pixabay"])
    assert result.execution_plan.status == "blocked"
    assert result.execution_plan.render_tasks == []
    assert any(d.code == "nested_media_depth_unsupported" for d in result.execution_plan.diagnostics)


def test_pillow_graph_with_edges_is_blocked_without_exact_draw_action():
    graph = {
        "object_id": "graph", "kind": "node_graph",
        "content": {
            "layout": "left_to_right",
            "nodes": [{"node_id": "a", "label": "A"}, {"node_id": "b", "label": "B"}],
            "edges": [{"edge_id": "ab", "from_node_id": "a", "to_node_id": "b"}],
        },
    }
    result = _compile(_base_bundle([graph]), renderer_id=PILLOW_RENDERER_ID)
    assert result.execution_plan.status == "blocked"
    assert result.execution_plan.render_tasks == []
    assert any(d.code == "pillow_progressive_state_unrepresentable" for d in result.execution_plan.diagnostics)


def test_blocked_shot_does_not_emit_orphan_asset_requests():
    image = {
        "object_id": "photo", "kind": "image",
        "content": {
            "subject": "Ноутбук на рабочем столе",
            "search_queries": ["laptop on desk"],
            "orientation": "landscape",
            "crop_intent": "Экран остаётся видимым.",
        },
    }
    move = {
        "event_id": "move_event", "action": "move",
        "target": {"object_id": "photo"},
        "trigger": {"anchor_id": "anchor_001"},
        "destination_region": "left", "destination_anchor": "center_left",
    }
    result = _compile(_base_bundle([image], [move]), asset_provider_allowlist=["pixabay"])
    assert result.execution_plan.status == "blocked"
    assert result.execution_plan.asset_requests == []
    assert result.execution_plan.render_tasks == []
    assert result.asset_bindings == ()


def test_compilation_result_exposes_explicit_task_to_asset_bindings():
    image = {
        "object_id": "photo", "kind": "image",
        "content": {
            "subject": "Камера на столе", "search_queries": ["camera on desk"],
            "orientation": "landscape", "crop_intent": "Камера целиком в кадре.",
        },
    }
    text = {"object_id": "caption", "kind": "text", "content": {"text": "Проверка"}}
    result = _compile(_base_bundle([image, text]), asset_provider_allowlist=["pixabay"])
    task = result.execution_plan.render_tasks[0]
    binding = result.asset_bindings[0]
    assert binding.task_id == task.task_id
    assert len(binding.asset_request_ids) == 1
    assert binding.asset_request_ids[0] == result.execution_plan.asset_requests[0].request_id
    serialized = json.loads(serialize_compilation_result(result))
    assert serialized["asset_bindings"][0]["task_id"] == task.task_id
