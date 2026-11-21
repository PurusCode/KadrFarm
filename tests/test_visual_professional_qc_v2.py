from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from kadr_farm.visual_professional_assets_v2 import ResolvedMedia
from kadr_farm.visual_professional_qc_v2 import (
    VisualProfessionalQCError,
    build_asset_manifest_entry,
    build_event_timeline,
    build_semantic_frame_requests,
    evaluate_browser_geometry,
    validate_event_conflicts,
    validate_hidden_objects_are_revealed,
)
from kadr_farm.visual_story_v2 import VisualStoryPlan
from kadr_farm.visual_scene_compiler_v2 import compile_plan
from kadr_farm.visual_professional_renderer_v2 import _event_css


def _plan(events=None, *, initial_state="visible", fps=25):
    text = "Проверяем временную шкалу."
    return VisualStoryPlan.model_validate({
        "schema_version": "2.0",
        "title": "Professional QC test",
        "width": 1280,
        "height": 720,
        "fps": fps,
        "speech_units": [{"unit_id": "u01", "text": text, "char_start": 0, "char_end": len(text)}],
        "shots": [{
            "shot_id": "s01",
            "editorial_purpose": "Проверка профессионального рендера",
            "visual_summary": "Объект появляется и меняет состояние по расписанию",
            "speech_unit_ids": ["u01"],
            "objects": [{
                "object_id": "title", "kind": "text", "initial_state": initial_state,
                "content": {"text": "Проверка"},
            }],
            "events": events or [],
        }],
    })


def _event(event_id, action, start, duration=0.4, params=None):
    return {
        "event_id": event_id,
        "target_id": "title",
        "action": action,
        "anchor": {"unit_id": "u01", "offset_sec": start},
        "duration_sec": duration,
        "params": params or {},
    }


def test_overlapping_writes_to_same_property_are_rejected():
    plan = _plan([
        _event("fade_a", "fade_in", 0.1, 0.6),
        _event("fade_b", "fade_out", 0.4, 0.5),
    ])
    with pytest.raises(VisualProfessionalQCError, match="both write opacity"):
        validate_event_conflicts(plan.shots[0])


def test_independent_properties_may_animate_concurrently():
    plan = _plan([
        _event("fade", "fade_in", 0.1, 0.6),
        _event("scale", "scale", 0.1, 0.6, {"factor": 1.1}),
        _event("glow", "highlight", 0.2, 0.3, {"color": "#38C7AE"}),
    ])
    validate_event_conflicts(plan.shots[0])


def test_sequential_writes_to_same_property_are_allowed():
    plan = _plan([
        _event("fade_in", "fade_in", 0.1, 0.3),
        _event("fade_out", "fade_out", 0.4, 0.3),
    ])
    validate_event_conflicts(plan.shots[0])


def test_reveal_row_events_for_different_rows_are_independent():
    shot = SimpleNamespace(
        shot_id="s01",
        events=[
            SimpleNamespace(event_id="row0", target_id="table", action="reveal_row",
                            anchor=SimpleNamespace(offset_sec=0.1), duration_sec=0.5,
                            params={"row_index": 0}),
            SimpleNamespace(event_id="row1", target_id="table", action="reveal_row",
                            anchor=SimpleNamespace(offset_sec=0.1), duration_sec=0.5,
                            params={"row_index": 1}),
        ],
    )
    validate_event_conflicts(shot)


def test_overlapping_reveals_of_same_table_row_are_rejected():
    shot = SimpleNamespace(
        shot_id="s01",
        events=[
            SimpleNamespace(event_id="row0a", target_id="table", action="reveal_row",
                            anchor=SimpleNamespace(offset_sec=0.1), duration_sec=0.5,
                            params={"row_index": 0}),
            SimpleNamespace(event_id="row0b", target_id="table", action="reveal_row",
                            anchor=SimpleNamespace(offset_sec=0.3), duration_sec=0.5,
                            params={"row_index": 0}),
        ],
    )
    with pytest.raises(VisualProfessionalQCError, match=r"row\[0\]"):
        validate_event_conflicts(shot)


def test_hidden_object_requires_explicit_reveal_event():
    plan = _plan([_event("scale", "scale", 0.1, 0.3, {"factor": 1.2})], initial_state="hidden")
    with pytest.raises(VisualProfessionalQCError, match="no appear/fade_in"):
        validate_hidden_objects_are_revealed(plan.shots[0])


def test_hidden_object_with_appear_event_is_valid():
    plan = _plan([_event("appear", "appear", 0.1, 0.1)], initial_state="hidden")
    validate_hidden_objects_are_revealed(plan.shots[0])


def test_event_that_finishes_after_last_encoded_frame_is_rejected():
    plan = _plan([_event("late_fade", "fade_in", 0.7, 0.4)])
    jobs = compile_plan(plan, {"u01": 1.0})
    with pytest.raises(VisualProfessionalQCError, match="last encoded frame"):
        build_event_timeline(plan, jobs)


def test_event_timeline_and_semantic_contact_requests_cover_event_states():
    plan = _plan([_event("fade", "fade_in", 0.1, 0.4)])
    jobs = compile_plan(plan, {"u01": 1.0})
    timeline = build_event_timeline(plan, jobs)
    requests = build_semantic_frame_requests(jobs, plan.fps, timeline)
    states = {state for request in requests for state in request["states"]}
    assert {"shot_start", "shot_end", "event_start", "event_middle", "event_end"} <= states
    assert all(request["frame_index"] >= 0 for request in requests)
    assert len({request["frame_index"] for request in requests}) == len(requests)


def test_geometry_report_is_returned_from_browser_evaluator():
    import asyncio
    class FakePage:
        async def evaluate(self, script, options):
            assert "getBoundingClientRect" in script
            assert options["safeAreaMargin"] == pytest.approx(0.04)
            return {"schema_version": "1.0", "status": "passed", "errors": [], "warnings": [], "objects": []}
    report = asyncio.run(evaluate_browser_geometry(FakePage()))
    assert report["status"] == "passed"


def test_geometry_evaluator_rejects_invalid_safe_area_margin():
    import asyncio
    class FakePage:
        async def evaluate(self, *args):
            raise AssertionError("page must not be evaluated for invalid input")
    with pytest.raises(ValueError, match="safe_area_margin"):
        asyncio.run(evaluate_browser_geometry(FakePage(), safe_area_margin=0.5))




def test_sequential_draw_events_accumulate_in_one_css_rule():
    plan = _plan([
        _event("draw_a", "draw", 0.1, 0.2),
        _event("draw_b", "draw", 0.5, 0.2),
    ])
    css, _ = _event_css(plan.shots[0])
    assert css.count(".draw-path{") == 1
    assert "draw_kf_0" in css and "draw_kf_1" in css
    assert "stroke-dashoffset:1000" in css


def test_sequential_move_uses_previous_move_destination_as_next_start():
    plan = _plan([
        _event("move_a", "move", 0.1, 0.2, {"x": 0.6, "y": 0.4}),
        _event("move_b", "move", 0.5, 0.2, {"x": 0.7, "y": 0.3}),
    ])
    css, _ = _event_css(plan.shots[0])
    assert "@keyframes kf_1{from{left:60.0000%;top:40.0000%}to{left:70.0000%;top:30.0000%}}" in css


def test_only_first_css_animation_uses_backwards_fill():
    plan = _plan([
        _event("fade_a", "fade_in", 0.1, 0.2),
        _event("fade_b", "fade_out", 0.5, 0.2),
    ])
    css, _ = _event_css(plan.shots[0])
    assert "kf_0 0.2000s cubic-bezier(.22,.61,.36,1) 0.1000s both" in css
    assert "kf_1 0.2000s cubic-bezier(.22,.61,.36,1) 0.5000s forwards" in css


def test_asset_manifest_contains_file_hash_and_source_metadata(tmp_path: Path):
    path = tmp_path / "asset.png"
    path.write_bytes(b"test asset bytes")
    asset = ResolvedMedia(
        object_id="photo", path=path, media_type="image", source="Pixabay",
        source_url="https://example.invalid/photo", creator="Example Creator",
        metadata={"rights_status": "recorded", "query": "server room"},
    )
    manifest = build_asset_manifest_entry("s01", asset)
    assert manifest["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert manifest["file_size_bytes"] == path.stat().st_size
    assert manifest["source_url"] == "https://example.invalid/photo"
    assert manifest["metadata"]["query"] == "server room"


def test_text_overflow_qc_only_flags_elements_that_clip_content():
    """Visible glyph overflow is not itself evidence that text is clipped."""
    from kadr_farm.visual_professional_qc_v2 import _BROWSER_GEOMETRY_SCRIPT

    assert 'getComputedStyle(element)' in _BROWSER_GEOMETRY_SCRIPT
    assert 'clipsHorizontally' in _BROWSER_GEOMETRY_SCRIPT
    assert 'clipsVertically' in _BROWSER_GEOMETRY_SCRIPT
    assert 'horizontalOverflow || verticalOverflow' in _BROWSER_GEOMETRY_SCRIPT
