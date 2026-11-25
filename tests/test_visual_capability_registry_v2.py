from __future__ import annotations

import pytest

from kadr_farm.visual_capability_registry_v2 import (
    LEGACY_EXECUTOR_INVENTORY,
    PILLOW_RENDERER_ID,
    PROFESSIONAL_RENDERER_ID,
    REGISTRY_VERSION,
    RUNTIME_ACTIONS,
    RUNTIME_OBJECT_KINDS,
    CapabilityValidationError,
    get_renderer_capability,
    list_renderer_capabilities,
    require_shot_capabilities,
    validate_capability_request,
    validate_shot_capabilities,
)
from kadr_farm.visual_story_v2 import Shot


def _shot(objects, events=None):
    return Shot.model_validate({
        "shot_id": "s01",
        "editorial_purpose": "Проверка реальных возможностей renderer",
        "visual_summary": "Набор объектов и событий для capability validator",
        "speech_unit_ids": ["u01"],
        "objects": objects,
        "events": events or [],
    })


def _event(action, target_id, params=None):
    return {
        "event_id": f"evt_{action}_{len(str(params or {}))}",
        "target_id": target_id,
        "action": action,
        "anchor": {"unit_id": "u01", "offset_sec": 0.1},
        "duration_sec": 0.4,
        "params": params or {},
    }


def test_registry_has_two_runtime_v2_executors_and_stable_version():
    records = list_renderer_capabilities()
    assert REGISTRY_VERSION == "1.0.0"
    assert {item.renderer_id for item in records} == {
        PROFESSIONAL_RENDERER_ID, PILLOW_RENDERER_ID,
    }
    assert all(item.runtime_contract == "kadrfarm.visual_story_v2" for item in records)
    assert all(not item.runtime_contract.endswith("visual_story_contract_v2") for item in records)


def test_both_runtime_executors_register_all_runtime_object_kinds():
    for renderer_id in (PROFESSIONAL_RENDERER_ID, PILLOW_RENDERER_ID):
        caps = get_renderer_capability(renderer_id)
        assert set(caps.supported_kinds) == set(RUNTIME_OBJECT_KINDS)
        assert caps.max_objects_per_shot is None
        assert caps.max_group_depth == 8


def test_professional_renderer_registers_all_ten_runtime_actions():
    caps = get_renderer_capability(PROFESSIONAL_RENDERER_ID)
    assert set(caps.supported_actions) == set(RUNTIME_ACTIONS)


def test_pillow_action_registry_exposes_real_action_subset_and_union():
    caps = get_renderer_capability(PILLOW_RENDERER_ID)
    assert set(caps.supported_actions) == set(RUNTIME_ACTIONS)
    assert "group" not in caps.action("fade_in").target_kinds
    assert caps.action("update_text").target_kinds == ("text",)
    assert caps.action("draw").allowed_subtypes_for("shape") == ("line", "arrow")
    assert caps.action("draw").allowed_subtypes_for("chart") == ("bar", "line")


def test_registered_chart_shapes_icons_and_graph_layouts_match_renderer_dispatch():
    for renderer_id in (PROFESSIONAL_RENDERER_ID, PILLOW_RENDERER_ID):
        caps = get_renderer_capability(renderer_id)
        expected_charts = ("bar", "line", "pie", "donut", "area") if renderer_id == PROFESSIONAL_RENDERER_ID else ("bar", "line", "pie", "donut")
        assert caps.kind("chart").values_for("chart_type") == expected_charts
        assert caps.chart_types == expected_charts
        assert caps.kind("shape").values_for("shape_type") == (
            "rectangle", "rounded_rectangle", "circle", "ellipse", "line", "arrow", "polygon",
        )
        assert caps.kind("icon").values_for("icon_id") == (
            "check", "cross", "warning", "play",
        )
        assert caps.kind("node_graph").values_for("layout") == (
            "left_to_right", "top_to_bottom",
        )


def test_unknown_renderer_is_not_silently_mapped_to_default():
    with pytest.raises(KeyError, match="Unknown renderer_id"):
        get_renderer_capability("imaginary_renderer")


@pytest.mark.parametrize("renderer_id", [PROFESSIONAL_RENDERER_ID, PILLOW_RENDERER_ID])
def test_supported_runtime_shot_passes_capability_validation(renderer_id):
    shot = _shot([{
        "object_id": "title", "kind": "text", "content": {"text": "Заголовок"},
    }], [_event("fade_in", "title")])
    report = validate_shot_capabilities(shot, renderer_id)
    assert report.supported
    assert report.diagnostics == ()
    assert report.as_dict()["status"] == "supported"


def test_runtime_schema_rejects_chart_variant_outside_runtime_enum():
    from pydantic import ValidationError
    from kadr_farm.visual_story_v2 import VisualObject

    # Runtime VisualObject validates chart_type before a Shot can reach the
    # capability registry. This is the earliest correct rejection boundary.
    with pytest.raises(ValidationError, match="Unsupported chart_type"):
        VisualObject.model_validate({
            "object_id": "chart", "kind": "chart",
            "content": {
                "chart_type": "radar", "data_mode": "provided",
                "series": [{"values": [1, 2]}],
            },
        })


def test_registry_also_reports_unregistered_chart_variant_as_unsupported():
    # Exercise the registry query directly: an invalid value cannot be wrapped
    # in runtime Shot because the strict runtime schema already rejects it.
    issues = validate_capability_request(
        PROFESSIONAL_RENDERER_ID,
        kind="chart",
        subtype_field="chart_type",
        subtype="radar",
    )
    assert [item.code for item in issues] == ["unsupported_subtype"]


def test_unknown_icon_is_blocked_instead_of_producing_placeholder():
    shot = _shot([{
        "object_id": "icon", "kind": "icon", "content": {"icon_id": "rocket"},
    }])
    report = validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID)
    assert not report.supported
    assert "unsupported_subtype" in {item.code for item in report.diagnostics}


def test_unsupported_graph_layout_is_blocked_instead_of_falling_back_to_left_to_right():
    shot = _shot([{
        "object_id": "graph", "kind": "node_graph",
        "content": {"layout": "radial", "nodes": [{"node_id": "a"}]},
    }])
    report = validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID)
    assert not report.supported
    assert "unsupported_subtype" in {item.code for item in report.diagnostics}


def test_pillow_rejects_draw_on_rectangle_because_no_progressive_draw_is_implemented():
    shot = _shot([{
        "object_id": "box", "kind": "shape", "content": {"shape_type": "rectangle"},
    }], [_event("draw", "box")])
    report = validate_shot_capabilities(shot, PILLOW_RENDERER_ID)
    assert not report.supported
    assert "incompatible_action_subtype" in {item.code for item in report.diagnostics}


def test_pillow_accepts_progressive_draw_on_line_shape():
    shot = _shot([{
        "object_id": "connector", "kind": "shape", "content": {"shape_type": "line"},
    }], [_event("draw", "connector")])
    assert validate_shot_capabilities(shot, PILLOW_RENDERER_ID).supported


def test_pillow_rejects_update_text_on_callout_because_callout_ignores_state_text():
    shot = _shot([{
        "object_id": "note", "kind": "callout", "content": {"text": "Исходный текст"},
    }], [_event("update_text", "note", {"text": "Новый текст"})])
    report = validate_shot_capabilities(shot, PILLOW_RENDERER_ID)
    assert not report.supported
    assert "incompatible_action_target" in {item.code for item in report.diagnostics}


def test_professional_allows_update_text_on_callout():
    shot = _shot([{
        "object_id": "note", "kind": "callout", "content": {"text": "Исходный текст"},
    }], [_event("update_text", "note", {"text": "Новый текст"})])
    assert validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID).supported


def test_reveal_row_requires_existing_row_index():
    shot = _shot([{
        "object_id": "table", "kind": "table",
        "content": {"columns": [{"title": "A"}], "rows": [{"cells": ["1"]}]},
    }], [_event("reveal_row", "table", {"row_index": 4})])
    report = validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID)
    assert not report.supported
    assert "invalid_row_subtarget" in {item.code for item in report.diagnostics}


def test_reveal_row_accepts_existing_row_index():
    shot = _shot([{
        "object_id": "table", "kind": "table",
        "content": {"columns": [{"title": "A"}], "rows": [{"cells": ["1"]}]},
    }], [_event("reveal_row", "table", {"row_index": 0})])
    assert validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID).supported


def test_stable_subtarget_id_is_blocked_for_runtime_renderer_actions():
    issues = validate_capability_request(
        PROFESSIONAL_RENDERER_ID,
        kind="node_graph",
        action="draw",
        subtype_field="layout",
        subtype="left_to_right",
        subtarget_id="edge_a_b",
    )
    assert [item.code for item in issues] == ["unsupported_subtarget"]


def test_pillow_blocks_media_inside_second_nested_group_level():
    shot = _shot([{
        "object_id": "outer", "kind": "group", "content": {"children": [{
            "object_id": "inner", "kind": "group", "content": {"children": [{
                "object_id": "deep_photo", "kind": "image", "content": {"path": "photo.png"},
            }]},
        }]},
    }])
    report = validate_shot_capabilities(shot, PILLOW_RENDERER_ID)
    assert not report.supported
    assert "nested_media_depth_unsupported" in {item.code for item in report.diagnostics}


def test_professional_blocks_group_nesting_beyond_eight_levels():
    child = {"object_id": "leaf", "kind": "text", "content": {"text": "leaf"}}
    for index in reversed(range(10)):
        child = {"object_id": f"group_{index}", "kind": "group", "content": {"children": [child]}}
    shot = _shot([child])
    report = validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID)
    assert not report.supported
    assert "group_nesting_limit" in {item.code for item in report.diagnostics}


def test_duplicate_nested_object_ids_are_blocked():
    shot = _shot([{
        "object_id": "outer", "kind": "group", "content": {"children": [
            {"object_id": "same", "kind": "text", "content": {"text": "A"}},
            {"object_id": "same", "kind": "text", "content": {"text": "B"}},
        ]},
    }])
    report = validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID)
    assert not report.supported
    assert "duplicate_nested_object_id" in {item.code for item in report.diagnostics}


def test_pillow_camera_motion_is_blocked_but_professional_browser_motion_is_registered():
    shot = _shot([{
        "object_id": "photo", "kind": "image",
        "content": {"path": "photo.png", "camera_motion": "slow_zoom"},
    }])
    assert validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID).supported
    report = validate_shot_capabilities(shot, PILLOW_RENDERER_ID)
    assert not report.supported
    assert "unsupported_media_motion" in {item.code for item in report.diagnostics}


def test_renderer_input_media_formats_are_explicitly_not_whitelisted():
    for renderer_id in (PROFESSIONAL_RENDERER_ID, PILLOW_RENDERER_ID):
        caps = get_renderer_capability(renderer_id)
        assert caps.input_media_formats is None
        assert "No repository-level" in caps.input_format_policy


def test_output_formats_and_download_limit_are_grounded_in_implementations():
    professional = get_renderer_capability(PROFESSIONAL_RENDERER_ID)
    pillow = get_renderer_capability(PILLOW_RENDERER_ID)
    assert professional.output_formats == ("video/mp4",)
    assert professional.max_download_bytes == 100 * 1024 * 1024
    assert pillow.output_formats == ("image/png",)
    assert pillow.max_download_bytes is None


def test_cp01_renderer_capabilities_projection_is_available():
    model = get_renderer_capability(PROFESSIONAL_RENDERER_ID).as_contract_model()
    assert model.renderer_id == PROFESSIONAL_RENDERER_ID
    assert set(model.supported_kinds) == set(RUNTIME_OBJECT_KINDS)
    assert set(model.supported_actions) == set(RUNTIME_ACTIONS)
    assert model.supported_chart_types == ["bar", "line", "pie", "donut", "area"]
    assert model.supported_formats == ["video/mp4"]
    assert model.max_nesting_depth == 8


def test_require_shot_capabilities_returns_report_or_raises_blocking_error():
    supported = _shot([{"object_id": "title", "kind": "text", "content": {"text": "OK"}}])
    assert require_shot_capabilities(supported, PROFESSIONAL_RENDERER_ID).supported

    unsupported = _shot([{"object_id": "icon", "kind": "icon", "content": {"icon_id": "rocket"}}])
    with pytest.raises(CapabilityValidationError) as exc:
        require_shot_capabilities(unsupported, PROFESSIONAL_RENDERER_ID)
    assert not exc.value.report.supported


def test_legacy_scene_and_beat_renderers_are_not_registered_as_runtime_v2_executors():
    assert {item["executor_id"] for item in LEGACY_EXECUTOR_INVENTORY} == {
        "diagram_renderer_legacy", "html_renderer_legacy",
    }
    assert all(item["runtime_v2_compatible"] is False for item in LEGACY_EXECUTOR_INVENTORY)
    with pytest.raises(KeyError):
        get_renderer_capability("diagram_renderer_legacy")


def test_unknown_object_kind_is_blocked_without_fallback():
    issues = validate_capability_request(
        PROFESSIONAL_RENDERER_ID, kind="animated_widget",
    )
    assert len(issues) == 1
    assert issues[0].code == "unsupported_kind"


def test_semantic_authoring_action_without_runtime_equivalent_is_blocked():
    issues = validate_capability_request(
        PROFESSIONAL_RENDERER_ID, kind="node_graph", action="reveal_node",
    )
    assert len(issues) == 1
    assert issues[0].code == "unsupported_action"


def test_pillow_rejects_actions_on_group_objects():
    shot = _shot([
        {"object_id": "group", "kind": "group", "content": {"children": [
            {"object_id": "child", "kind": "text", "content": {"text": "Внутри"}},
        ]}},
    ], [_event("fade_in", "group")])
    report = validate_shot_capabilities(shot, PILLOW_RENDERER_ID)
    assert not report.supported
    assert "incompatible_action_target" in {item.code for item in report.diagnostics}


def test_professional_draw_support_is_an_object_wipe_not_a_stable_subtarget_api():
    caps = get_renderer_capability(PROFESSIONAL_RENDERER_ID)
    assert set(caps.action("draw").target_kinds) == set(RUNTIME_OBJECT_KINDS)
    assert "subtarget" in caps.action("draw").behavior.lower()


def test_registry_rejects_unknown_subtype_field_instead_of_ignoring_it():
    issues = validate_capability_request(
        PROFESSIONAL_RENDERER_ID,
        kind="chart",
        subtype_field="shape_type",
        subtype="radar",
    )
    assert [item.code for item in issues] == ["unknown_subtype_field"]


@pytest.mark.parametrize("renderer_id", [PROFESSIONAL_RENDERER_ID, PILLOW_RENDERER_ID])
def test_graph_edge_referencing_unknown_node_is_blocked(renderer_id):
    shot = _shot([{
        "object_id": "graph", "kind": "node_graph",
        "content": {
            "nodes": [{"node_id": "known", "label": "Узел"}],
            "edges": [{"from": "known", "to": "missing"}],
        },
    }])
    report = validate_shot_capabilities(shot, renderer_id)
    assert not report.supported
    assert "invalid_graph_edge_reference" in {item.code for item in report.diagnostics}


def test_pillow_draw_on_node_graph_without_edges_is_blocked():
    shot = _shot([{
        "object_id": "graph", "kind": "node_graph",
        "content": {"nodes": [{"node_id": "a", "label": "Узел A"}], "edges": []},
    }], [_event("draw", "graph")])
    report = validate_shot_capabilities(shot, PILLOW_RENDERER_ID)
    assert not report.supported
    assert "empty_draw_target" in {item.code for item in report.diagnostics}




def test_pillow_allows_appear_on_group_objects():
    shot = _shot([{
        "object_id": "group", "kind": "group", "initial_state": "hidden",
        "content": {"children": [{"object_id": "child", "kind": "text", "content": {"text": "Внутри"}}]},
    }], [_event("appear", "group")])
    assert validate_shot_capabilities(shot, PILLOW_RENDERER_ID).supported
    assert "group" in get_renderer_capability(PILLOW_RENDERER_ID).action("appear").target_kinds


def test_pillow_rejects_area_chart_instead_of_silently_rendering_a_line():
    shot = _shot([{
        "object_id": "area", "kind": "chart",
        "content": {"chart_type": "area", "data_mode": "provided", "series": [{"values": [1, 2, 3]}]},
    }])
    report = validate_shot_capabilities(shot, PILLOW_RENDERER_ID)
    assert not report.supported
    assert "unsupported_subtype" in {item.code for item in report.diagnostics}


def test_pillow_rejects_multiseries_bar_chart_because_only_first_series_is_rendered():
    shot = _shot([{
        "object_id": "bar", "kind": "chart",
        "content": {"chart_type": "bar", "data_mode": "provided", "series": [
            {"values": [1, 2]}, {"values": [3, 4]},
        ]},
    }], [_event("draw", "bar")])
    report = validate_shot_capabilities(shot, PILLOW_RENDERER_ID)
    assert not report.supported
    assert "unsupported_chart_series_count" in {item.code for item in report.diagnostics}


@pytest.mark.parametrize("renderer_id", [PROFESSIONAL_RENDERER_ID, PILLOW_RENDERER_ID])
@pytest.mark.parametrize("chart_type", ["pie", "donut"])
def test_both_renderers_reject_multiseries_pie_and_donut(renderer_id, chart_type):
    shot = _shot([{
        "object_id": "pie", "kind": "chart",
        "content": {"chart_type": chart_type, "data_mode": "provided", "series": [
            {"values": [1, 2]}, {"values": [3, 4]},
        ]},
    }])
    report = validate_shot_capabilities(shot, renderer_id)
    assert not report.supported
    assert "unsupported_chart_series_count" in {item.code for item in report.diagnostics}


def test_pillow_requires_draw_event_for_top_level_line_shape():
    shot = _shot([{
        "object_id": "line", "kind": "shape", "content": {"shape_type": "line"},
    }])
    report = validate_shot_capabilities(shot, PILLOW_RENDERER_ID)
    assert not report.supported
    assert "required_draw_event_missing" in {item.code for item in report.diagnostics}


def test_pillow_requires_draw_event_for_top_level_bar_chart():
    shot = _shot([{
        "object_id": "bar", "kind": "chart",
        "content": {"chart_type": "bar", "data_mode": "provided", "series": [{"values": [1, 2, 3]}]},
    }])
    report = validate_shot_capabilities(shot, PILLOW_RENDERER_ID)
    assert not report.supported
    assert "required_draw_event_missing" in {item.code for item in report.diagnostics}


def test_pillow_requires_draw_event_for_top_level_graph_edges():
    shot = _shot([{
        "object_id": "graph", "kind": "node_graph",
        "content": {"nodes": [{"node_id": "a"}, {"node_id": "b"}], "edges": [{"from": "a", "to": "b"}]},
    }])
    report = validate_shot_capabilities(shot, PILLOW_RENDERER_ID)
    assert not report.supported
    assert "required_draw_event_missing" in {item.code for item in report.diagnostics}


def test_pillow_requires_reveal_event_for_final_row_of_top_level_table():
    shot = _shot([{
        "object_id": "table", "kind": "table",
        "content": {"columns": [{"title": "A"}], "rows": [{"cells": ["1"]}, {"cells": ["2"]}]},
    }])
    report = validate_shot_capabilities(shot, PILLOW_RENDERER_ID)
    assert not report.supported
    assert "required_final_row_reveal_missing" in {item.code for item in report.diagnostics}


def test_pillow_accepts_top_level_table_when_final_row_is_revealed():
    shot = _shot([{
        "object_id": "table", "kind": "table",
        "content": {"columns": [{"title": "A"}], "rows": [{"cells": ["1"]}, {"cells": ["2"]}]},
    }], [_event("reveal_row", "table", {"row_index": 1})])
    assert validate_shot_capabilities(shot, PILLOW_RENDERER_ID).supported


@pytest.mark.parametrize("nodes", [
    [{"label": "Без ID"}],
    [{"node_id": "same"}, {"node_id": "same"}],
    [{"node_id": 123}],
])
def test_graph_requires_unique_nonempty_string_node_ids(nodes):
    shot = _shot([{
        "object_id": "graph", "kind": "node_graph", "content": {"nodes": nodes, "edges": []},
    }])
    report = validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID)
    assert not report.supported
    assert {"invalid_graph_node_id", "duplicate_graph_node_id"} & {item.code for item in report.diagnostics}


def test_polygon_requires_at_least_three_points():
    shot = _shot([{
        "object_id": "polygon", "kind": "shape", "content": {"shape_type": "polygon", "points": [[0, 0], [1, 1]]},
    }])
    report = validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID)
    assert not report.supported
    assert "invalid_polygon_points" in {item.code for item in report.diagnostics}


def test_polygon_rejects_more_than_professional_renderers_64_point_limit():
    points = [[i / 64, (i % 2) * 0.5] for i in range(65)]
    shot = _shot([{
        "object_id": "polygon", "kind": "shape", "content": {"shape_type": "polygon", "points": points},
    }])
    report = validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID)
    assert not report.supported
    assert "polygon_point_limit" in {item.code for item in report.diagnostics}


def test_pillow_rejects_authored_line_endpoints_that_it_ignores():
    shot = _shot([{
        "object_id": "line", "kind": "shape",
        "content": {"shape_type": "line", "start": [0.1, 0.2], "end": [0.9, 0.8]},
    }], [_event("draw", "line")])
    report = validate_shot_capabilities(shot, PILLOW_RENDERER_ID)
    assert not report.supported
    assert "unsupported_shape_endpoints" in {item.code for item in report.diagnostics}


def test_chart_rejects_non_numeric_values_before_renderer_failure():
    shot = _shot([{
        "object_id": "chart", "kind": "chart",
        "content": {"chart_type": "line", "data_mode": "provided", "series": [{"values": [1, "not-a-number"]}]},
    }])
    report = validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID)
    assert not report.supported
    assert "invalid_chart_value" in {item.code for item in report.diagnostics}


@pytest.mark.parametrize("renderer_id", [PROFESSIONAL_RENDERER_ID, PILLOW_RENDERER_ID])
def test_pie_chart_requires_positive_total(renderer_id):
    shot = _shot([{
        "object_id": "pie", "kind": "chart",
        "content": {"chart_type": "pie", "data_mode": "provided", "series": [{"values": [0, -2]}]},
    }])
    report = validate_shot_capabilities(shot, renderer_id)
    assert not report.supported
    assert "empty_pie_data" in {item.code for item in report.diagnostics}


def test_professional_reveal_row_obeys_renderer_index_clamp():
    rows = [{"cells": [str(index)]} for index in range(1002)]
    shot = _shot([{
        "object_id": "table", "kind": "table",
        "content": {"columns": [{"title": "A"}], "rows": rows},
    }], [_event("reveal_row", "table", {"row_index": 1001})])
    report = validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID)
    assert not report.supported
    assert "invalid_row_subtarget" in {item.code for item in report.diagnostics}



def test_professional_bar_chart_accepts_multiple_series():
    shot = _shot([{
        "object_id": "bar", "kind": "chart",
        "content": {"chart_type": "bar", "data_mode": "provided", "series": [
            {"values": [1, 2]}, {"values": [3, 4]},
        ]},
    }])
    assert validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID).supported


def test_pillow_line_chart_accepts_multiple_series_when_draw_event_is_present():
    shot = _shot([{
        "object_id": "line", "kind": "chart",
        "content": {"chart_type": "line", "data_mode": "provided", "series": [
            {"values": [1, 2, 3]}, {"values": [3, 2, 1]},
        ]},
    }], [_event("draw", "line")])
    assert validate_shot_capabilities(shot, PILLOW_RENDERER_ID).supported


def test_table_rejects_malformed_columns_or_rows():
    shot = _shot([{
        "object_id": "table", "kind": "table",
        "content": {"columns": ["not-an-object"], "rows": [{"cells": "not-a-list"}]},
    }])
    report = validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID)
    assert not report.supported
    assert {"invalid_table_columns", "invalid_table_rows"} <= {item.code for item in report.diagnostics}


def test_polygon_rejects_out_of_range_or_non_numeric_coordinates():
    shot = _shot([{
        "object_id": "polygon", "kind": "shape",
        "content": {"shape_type": "polygon", "points": [[0, 0], [1.2, 0.5], [0, "bad"]]},
    }])
    report = validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID)
    assert not report.supported
    assert "invalid_polygon_point" in {item.code for item in report.diagnostics}


def test_line_chart_series_requires_at_least_two_points():
    shot = _shot([{
        "object_id": "line", "kind": "chart",
        "content": {"chart_type": "line", "data_mode": "provided", "series": [{"values": [5]}]},
    }])
    report = validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID)
    assert not report.supported
    assert "insufficient_chart_points" in {item.code for item in report.diagnostics}



def test_pillow_single_bar_category_does_not_require_draw_event():
    shot = _shot([{
        "object_id": "bar", "kind": "chart",
        "content": {"chart_type": "bar", "data_mode": "provided", "series": [{"values": [7]}]},
    }])
    assert validate_shot_capabilities(shot, PILLOW_RENDERER_ID).supported


def test_pillow_two_point_line_chart_does_not_require_draw_event():
    shot = _shot([{
        "object_id": "line", "kind": "chart",
        "content": {"chart_type": "line", "data_mode": "provided", "series": [{"values": [1, 2]}]},
    }])
    assert validate_shot_capabilities(shot, PILLOW_RENDERER_ID).supported



def test_pillow_cp01_projection_excludes_unsupported_area_chart():
    model = get_renderer_capability(PILLOW_RENDERER_ID).as_contract_model()
    assert model.supported_chart_types == ["bar", "line", "pie", "donut"]


def test_chart_rejects_huge_integer_that_cannot_be_safely_rendered_as_float():
    shot = _shot([{
        "object_id": "chart", "kind": "chart",
        "content": {"chart_type": "bar", "data_mode": "provided", "series": [{"values": [10 ** 400]}]},
    }])
    report = validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID)
    assert not report.supported
    assert "invalid_chart_value" in {item.code for item in report.diagnostics}
