from __future__ import annotations

from pathlib import Path

from kadr_farm.visual_professional_assets_v2 import ResolvedMedia
from kadr_farm.visual_professional_renderer_v2 import (
    build_shot_html,
    _recording_frame_window,
)
from kadr_farm.visual_story_v2 import VisualStoryPlan


def _base_plan(objects, events=None):
    text = "Проверяем профессиональный композитор."
    return VisualStoryPlan.model_validate({
        "schema_version": "2.0",
        "title": "Professional renderer test",
        "width": 1280,
        "height": 720,
        "fps": 25,
        "background": "#101827",
        "style": {
            "palette": ["#F5F7FA", "#AAB5C4", "#6D9EFF", "#18A999"],
            "font_display": "Arial",
            "font_body": "Arial",
        },
        "speech_units": [{
            "unit_id": "u01",
            "text": text,
            "char_start": 0,
            "char_end": len(text),
        }],
        "shots": [{
            "shot_id": "s01",
            "editorial_purpose": "Проверить композицию",
            "visual_summary": "Текст и графика располагаются как независимые слои",
            "speech_unit_ids": ["u01"],
            "objects": objects,
            "events": events or [],
        }],
    })


def test_text_is_composed_inside_layer_and_html_is_escaped():
    plan = _base_plan([{
        "object_id": "headline",
        "kind": "text",
        "layer": {"x": 0.42, "y": 0.45, "width": 0.66, "height": 0.4, "z_index": 4},
        "content": {
            "text": "Профессиональный <текст>",
            "eyebrow": "ВИЗУАЛЬНАЯ ИЕРАРХИЯ",
            "subtitle": "Подзаголовок",
            "font_size": 88,
            "alignment": "center",
            "vertical_align": "center",
        },
        "appearance": {"color": "#FFFFFF"},
    }])
    html, _ = build_shot_html(plan, plan.shots[0], {})
    assert "position:absolute" in html
    assert "text-wrap:balance" in html
    assert "&lt;текст&gt;" in html
    assert "Профессиональный <текст>" not in html
    assert "text-align:var(--type-align,center)" not in html  # layout is explicitly composed


def test_video_is_native_html_video_not_extracted_pngs(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    plan = _base_plan([{
        "object_id": "broll",
        "kind": "video",
        "layer": {"x": 0.5, "y": 0.5, "width": 1.0, "height": 1.0},
        "content": {"path": str(video), "loop_allowed": True, "focal_point": [0.4, 0.5]},
    }])
    media = ResolvedMedia("broll", video, "video", "local")
    html, _ = build_shot_html(plan, plan.shots[0], {"broll": media})
    assert "<video" in html
    assert "autoplay" not in html  # playback begins on the explicit shot-start signal
    assert "playsinline" in html
    assert "kadrfarm:start" in html
    assert "frame_000001.png" not in html


def test_scene_animation_clock_is_paused_until_explicit_start():
    plan = _base_plan([{
        "object_id": "headline",
        "kind": "text",
        "content": {"text": "Заголовок", "font_size": 72},
    }], events=[{
        "event_id": "fade",
        "target_id": "headline",
        "action": "fade_in",
        "anchor": {"unit_id": "u01", "offset_sec": 0.2},
        "duration_sec": 0.7,
    }])
    html, _ = build_shot_html(plan, plan.shots[0], {})
    assert "html:not(.playing) *{animation-play-state:paused!important}" in html
    assert "window.dispatchEvent(new Event('kadrfarm:start'))" not in html
    assert "window.addEventListener('kadrfarm:start'" in html


def test_two_animations_on_same_object_are_not_overwritten():
    plan = _base_plan([{
        "object_id": "headline",
        "kind": "text",
        "content": {"text": "Заголовок", "font_size": 72},
    }], events=[
        {
            "event_id": "fade",
            "target_id": "headline",
            "action": "fade_in",
            "anchor": {"unit_id": "u01", "offset_sec": 0.1},
            "duration_sec": 0.7,
        },
        {
            "event_id": "grow",
            "target_id": "headline",
            "action": "scale",
            "anchor": {"unit_id": "u01", "offset_sec": 0.1},
            "duration_sec": 0.7,
            "params": {"factor": 1.08},
        },
    ])
    html, _ = build_shot_html(
        plan,
        plan.shots[0],
        {},
        event_offsets={"fade": 0.25, "grow": 0.25},
    )
    assert "@keyframes kf_0" in html
    assert "@keyframes kf_1" in html
    assert "animation:kf_0" in html
    assert "kf_1 0.7000s" in html


def test_chart_is_svg_with_axis_labels_and_illustrative_disclosure():
    plan = _base_plan([{
        "object_id": "chart",
        "kind": "chart",
        "layer": {"x": 0.55, "y": 0.55, "width": 0.75, "height": 0.7},
        "content": {
            "chart_type": "bar",
            "data_mode": "illustrative",
            "title": "Рост показателя",
            "subtitle": "Условный пример",
            "categories": ["Янв", "Фев", "Мар"],
            "unit": "%",
            "series": [{"name": "Показатель", "values": [12, 28, 44]}],
        },
    }])
    html, _ = build_shot_html(plan, plan.shots[0], {})
    assert "chart-svg" in html
    assert "chart-grid" in html
    assert "chart-bar" in html
    assert "ИЛЛЮСТРАТИВНЫЕ ДАННЫЕ" in html
    assert "Рост показателя" in html


def test_node_graph_has_edges_and_named_nodes():
    plan = _base_plan([{
        "object_id": "flow",
        "kind": "node_graph",
        "layer": {"x": 0.5, "y": 0.5, "width": 0.9, "height": 0.65},
        "content": {
            "layout": "left_to_right",
            "nodes": [
                {"node_id": "a", "label": "СБОР", "x": 0.12, "y": 0.5},
                {"node_id": "b", "label": "АНАЛИЗ", "x": 0.5, "y": 0.5},
                {"node_id": "c", "label": "РЕЗУЛЬТАТ", "x": 0.88, "y": 0.5},
            ],
            "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}],
        },
    }])
    html, _ = build_shot_html(plan, plan.shots[0], {})
    assert "graph-stage" in html
    assert "СБОР" in html and "АНАЛИЗ" in html and "РЕЗУЛЬТАТ" in html
    assert "graph-edge" in html


def test_group_children_are_rendered_in_parent_composition():
    plan = _base_plan([{
        "object_id": "group",
        "kind": "group",
        "layer": {"x": 0.5, "y": 0.5, "width": 0.8, "height": 0.6, "z_index": 1},
        "content": {
            "children": [{
                "object_id": "child_text",
                "kind": "text",
                "layer": {"x": 0.5, "y": 0.5, "width": 0.8, "height": 0.4},
                "content": {"text": "Внутри группы", "font_size": 42, "alignment": "center"},
            }]
        },
    }])
    html, _ = build_shot_html(plan, plan.shots[0], {})
    assert "group-content" in html
    assert "data-object-id=\"child_text\"" in html
    assert "Внутри группы" in html


def test_recording_frame_window_uses_measured_preroll_not_total_excess():
    # The extra frames here are a recording tail, not a leading delay.
    assert _recording_frame_window(140, 120, preroll_frames=4) == (4, 0)
    assert _recording_frame_window(200, 132, preroll_frames=56) == (56, 0)


def test_recording_frame_window_pads_only_one_missing_tail_frame():
    assert _recording_frame_window(121, 118, preroll_frames=4) == (4, 1)


def test_recording_frame_window_rejects_real_capture_shortfall_after_preroll():
    import pytest

    with pytest.raises(RuntimeError, match="short by 2 frames"):
        _recording_frame_window(122, 120, preroll_frames=4)


def test_recording_frame_window_rejects_negative_preroll():
    import pytest

    with pytest.raises(ValueError, match="preroll_frames cannot be negative"):
        _recording_frame_window(120, 120, preroll_frames=-1)
