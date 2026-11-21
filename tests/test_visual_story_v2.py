import pytest

from pydantic import ValidationError

from kadr_farm.visual_story_v2 import (
    VisualStoryPlan,
)

from kadr_farm.visual_scene_compiler_v2 import (
    compile_plan,
)


def sample_plan():
    return {
        "schema_version": "2.0",
        "title": "Visual V2 test",
        "width": 1280,
        "height": 720,
        "fps": 25,
        "speech_units": [
            {
                "unit_id": "u01",
                "text": "Первая фраза.",
                "char_start": 0,
                "char_end": 13,
            },
            {
                "unit_id": "u02",
                "text": " Вторая фраза.",
                "char_start": 13,
                "char_end": 27,
            },
        ],
        "shots": [
            {
                "shot_id": "s01",
                "editorial_purpose": "Показать основную мысль",
                "visual_summary": "На экране появляется крупный заголовок",
                "speech_unit_ids": [
                    "u01",
                ],
                "objects": [
                    {
                        "object_id": "headline",
                        "kind": "text",
                        "layer": {
                            "z_index": 1,
                            "x": 0.5,
                            "y": 0.5,
                            "width": 0.8,
                            "height": 0.3,
                        },
                        "content": {
                            "text": "ПЕРВАЯ МЫСЛЬ",
                            "font_size": 64,
                        },
                        "initial_state": "hidden",
                    }
                ],
                "events": [
                    {
                        "event_id": "e01",
                        "target_id": "headline",
                        "action": "fade_in",
                        "anchor": {
                            "unit_id": "u01",
                            "offset_sec": 0.2,
                        },
                        "duration_sec": 0.6,
                    }
                ],
            },
            {
                "shot_id": "s02",
                "editorial_purpose": "Развить основную мысль",
                "visual_summary": "Три блока соединяются стрелками",
                "speech_unit_ids": [
                    "u02",
                ],
                "relation_to_previous": "extends",
                "objects": [
                    {
                        "object_id": "block_a",
                        "kind": "shape",
                        "layer": {
                            "z_index": 1,
                            "x": 0.2,
                            "y": 0.5,
                            "width": 0.2,
                            "height": 0.2,
                        },
                        "content": {
                            "shape_type": "rounded_rectangle",
                        },
                        "appearance": {
                            "fill": "#263449",
                            "stroke": "#F4B860",
                        },
                    },
                    {
                        "object_id": "arrow_a",
                        "kind": "shape",
                        "layer": {
                            "z_index": 2,
                            "x": 0.35,
                            "y": 0.5,
                            "width": 0.1,
                            "height": 0.02,
                        },
                        "content": {
                            "shape_type": "arrow",
                        },
                        "initial_state": "hidden",
                    },
                ],
                "events": [
                    {
                        "event_id": "e02",
                        "target_id": "arrow_a",
                        "action": "draw",
                        "anchor": {
                            "unit_id": "u02",
                            "offset_sec": 0.5,
                        },
                        "duration_sec": 0.8,
                    }
                ],
            },
        ],
    }


def test_valid_plan():
    plan = VisualStoryPlan.model_validate(
        sample_plan()
    )

    assert plan.schema_version == "2.0"
    assert len(plan.shots) == 2


def test_shots_must_cover_all_speech_units_in_order():
    data = sample_plan()

    data["shots"][1]["speech_unit_ids"] = []

    with pytest.raises(ValidationError):
        VisualStoryPlan.model_validate(data)


def test_duplicate_object_ids_rejected():
    data = sample_plan()

    data["shots"][0]["objects"].append(
        data["shots"][0]["objects"][0]
    )

    with pytest.raises(ValidationError):
        VisualStoryPlan.model_validate(data)


def test_missing_event_target_rejected():
    data = sample_plan()

    data["shots"][0]["events"][0]["target_id"] = "unknown_object"

    with pytest.raises(ValidationError):
        VisualStoryPlan.model_validate(data)


def test_measured_timeline():
    plan = VisualStoryPlan.model_validate(
        sample_plan()
    )

    jobs = compile_plan(
        plan,
        {
            "u01": 2.0,
            "u02": 3.0,
        },
    )

    assert len(jobs) == 2

    assert jobs[0].start_sec == 0.0
    assert jobs[0].end_sec == 2.0

    assert jobs[1].start_sec == 2.0
    assert jobs[1].end_sec == 5.0

    assert jobs[0].frame_start == 0
    assert jobs[0].frame_end == 50

    assert jobs[1].frame_start == 50
    assert jobs[1].frame_end == 125


def test_chart_requires_explicit_data_mode():
    data = sample_plan()

    data["shots"][0]["objects"] = [
        {
            "object_id": "bad_chart",
            "kind": "chart",
            "content": {
                "chart_type": "line",
                "series": [
                    {
                        "values": [1, 2, 3],
                    }
                ],
            },
        }
    ]

    with pytest.raises(ValidationError):
        VisualStoryPlan.model_validate(data)


def test_illustrative_chart_is_allowed():
    data = sample_plan()

    # Добавляем график, сохраняя существующий headline,
    # на который ссылается анимационное событие e01.
    data["shots"][0]["objects"].append(
        {
            "object_id": "example_chart",
            "kind": "chart",
            "content": {
                "chart_type": "line",
                "data_mode": "illustrative",
                "series": [
                    {
                        "values": [1, 2, 3],
                    }
                ],
            },
        }
    )

    plan = VisualStoryPlan.model_validate(data)

    chart = next(
        obj
        for obj in plan.shots[0].objects
        if obj.object_id == "example_chart"
    )

    assert chart.kind == "chart"
    assert chart.content["data_mode"] == "illustrative"

def test_event_offset_uses_globally_rounded_video_start():
    plan = VisualStoryPlan.model_validate(sample_plan())
    jobs = compile_plan(plan, {"u01": 1.019, "u02": 2.0})

    # u02 audio begins at 1.019s; its video shot begins at frame 25 = 1.0s.
    event = next(item for item in jobs[1].events if item.event.event_id == "e02")
    assert event.offset_sec == pytest.approx(0.519)


def test_event_offset_clamps_to_zero_when_video_rounding_starts_later():
    plan = VisualStoryPlan.model_validate(sample_plan())
    plan.shots[1].events[0].anchor.offset_sec = 0.0

    jobs = compile_plan(plan, {"u01": 1.021, "u02": 2.0})

    # u02 audio begins at 1.021s, while its video segment starts at frame 26 = 1.04s.
    event = next(item for item in jobs[1].events if item.event.event_id == "e02")
    assert event.offset_sec == 0.0


def test_compile_rejects_shot_that_rounds_to_zero_frames():
    plan = VisualStoryPlan.model_validate(sample_plan())

    with pytest.raises(ValueError, match="zero video frames"):
        compile_plan(plan, {"u01": 0.005, "u02": 2.0})


@pytest.mark.parametrize("bad_duration", [float("nan"), float("inf"), float("-inf"), True])
def test_compile_rejects_non_finite_or_non_numeric_wav_duration(bad_duration):
    plan = VisualStoryPlan.model_validate(sample_plan())

    with pytest.raises(ValueError, match="finite positive number"):
        compile_plan(plan, {"u01": bad_duration, "u02": 2.0})
