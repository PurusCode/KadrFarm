import json
from pathlib import Path
from kadr_farm import visual_planner
from kadr_farm.models import VisualPlanBatch

class FakeLLM:
    def __init__(self, *_args, **_kwargs): pass
    def complete(self, schema, *_args, **_kwargs):
        assert schema is VisualPlanBatch
        return VisualPlanBatch(plans=[dict(
            scene_id="s_001", visual_type="diagram",
            visual_description="A detailed explanatory diagram for the scene", visual_beats=[],
            asset_strategy="rendered", provider_id="internal_renderer", gen_prompt=None,
            search_queries=[], on_screen_text=["Короткая подпись"], speaker_emotion=None,
            pacing="normal", camera_move="static", music_cue=None, audio_sfx=[],
            claims=[], chart_data=None, humor_note="", transition_in="cut", transition_out="cut"
        )])

def test_planner_removes_service_scene_id_before_scene_merge(tmp_path, monkeypatch):
    source = tmp_path / "draft.json"
    target = tmp_path / "planned.json"
    source.write_text(json.dumps({"title":"t", "scenes":[{
        "id":"s_001", "block_id":"b1", "voice_text":"Текст речи.",
        "meaning":"Смысл сцены", "purpose":"Зачем эта сцена нужна"
    }]}), encoding="utf-8")
    monkeypatch.setattr(visual_planner, "StructuredLLMRouter", FakeLLM)
    result = visual_planner.plan_visuals(object(), source, target)
    assert "scene_id" not in result["scenes"][0]
    assert result["scenes"][0]["provider_id"] == "internal_renderer"


def _scene(scene_id):
    return {
        "id": scene_id, "block_id": "b1", "voice_text": f"Текст {scene_id}.",
        "meaning": f"Смысл сцены {scene_id}", "purpose": "Объяснить отдельную мысль",
    }


def _plan(scene_id):
    return dict(
        scene_id=scene_id, visual_type="diagram",
        visual_description=f"A detailed explanatory diagram for {scene_id}",
        visual_beats=[], asset_strategy="rendered", provider_id="internal_renderer",
        gen_prompt=None, search_queries=[], on_screen_text=["Короткая подпись"],
        speaker_emotion=None, pacing="normal", camera_move="static", music_cue=None,
        audio_sfx=[], claims=[], chart_data=None, humor_note="",
        transition_in="cut", transition_out="cut",
    )


def test_visual_planner_checkpoints_batches_and_resumes(tmp_path, monkeypatch):
    import json
    import pytest

    source = tmp_path / "draft.json"
    target = tmp_path / "planned.json"
    source.write_text(json.dumps({"title": "t", "scenes": [
        _scene("s_001"), _scene("s_002"), _scene("s_003"),
    ]}), encoding="utf-8")

    class FailAfterFirstBatch:
        calls = 0
        def __init__(self, *_args, **_kwargs):
            pass
        def complete(self, schema, _system, prompt, **_kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("temporary provider failure")
            scene_line = next(line for line in prompt.splitlines() if line.startswith("СЦЕНЫ: "))
            payload = json.loads(scene_line.removeprefix("СЦЕНЫ: "))
            return VisualPlanBatch(plans=[_plan(row["scene_id"]) for row in payload])

    monkeypatch.setattr(visual_planner, "StructuredLLMRouter", FailAfterFirstBatch)
    with pytest.raises(RuntimeError, match="temporary provider failure"):
        visual_planner.plan_visuals(object(), source, target, batch_size=2)

    progress = target.with_name("planned.progress.json")
    saved = json.loads(progress.read_text(encoding="utf-8"))
    assert saved["status"] == "incomplete"
    assert set(saved["completed"]) == {"s_001", "s_002"}
    assert not target.exists()

    class ResumeLastScene:
        calls = 0
        def __init__(self, *_args, **_kwargs):
            pass
        def complete(self, schema, _system, prompt, **_kwargs):
            self.calls += 1
            scene_line = next(line for line in prompt.splitlines() if line.startswith("СЦЕНЫ: "))
            payload = json.loads(scene_line.removeprefix("СЦЕНЫ: "))
            assert [row["scene_id"] for row in payload] == ["s_003"]
            return VisualPlanBatch(plans=[_plan("s_003")])

    monkeypatch.setattr(visual_planner, "StructuredLLMRouter", ResumeLastScene)
    result = visual_planner.plan_visuals(object(), source, target, batch_size=2)

    assert [s["id"] for s in result["scenes"]] == ["s_001", "s_002", "s_003"]
    assert all(s["provider_id"] == "internal_renderer" for s in result["scenes"])
    assert result["visual_planning_status"] == "complete"
    saved = json.loads(progress.read_text(encoding="utf-8"))
    assert saved["status"] == "complete"
    assert len(saved["completed"]) == 3


def test_visual_beats_normalize_equal_weights_from_llm():
    """Four beats each marked 1.0 mean four equal quarters, not a schema failure."""
    beats = [
        {
            "visual_type": "diagram",
            "visual_description": f"A meaningful visual beat number {index}",
            "asset_strategy": "rendered",
            "provider_id": "internal_renderer",
            "gen_prompt": None,
            "search_queries": [],
            "sub_duration_ratio": 1.0,
            "on_screen_text": [],
        }
        for index in range(1, 5)
    ]
    plan = _plan("s_042")
    plan["visual_beats"] = beats

    validated = VisualPlanBatch.model_validate({"plans": [plan]}).plans[0]

    ratios = [beat.sub_duration_ratio for beat in validated.visual_beats]
    assert ratios == [0.25, 0.25, 0.25, 0.25]
    assert sum(ratios) == 1.0


def test_scene_normalizes_visual_beat_weights_from_checkpoint_or_file():
    from kadr_farm.models import Scene

    scene_data = {
        "id": "s_042",
        "block_id": "b1",
        "voice_text": "Текст сцены.",
        "meaning": "Смысл этой сцены",
        "purpose": "Объяснить мысль",
        "visual_type": "diagram",
        "visual_description": "A detailed diagram illustrating the explanation",
        "visual_beats": [
            {
                "visual_type": "diagram",
                "visual_description": f"A meaningful visual beat number {index}",
                "asset_strategy": "rendered",
                "provider_id": "internal_renderer",
                "gen_prompt": None,
                "search_queries": [],
                "sub_duration_ratio": 1.0,
                "on_screen_text": [],
            }
            for index in range(1, 5)
        ],
        "asset_strategy": "rendered",
        "provider_id": "internal_renderer",
        "gen_prompt": None,
        "search_queries": [],
        "on_screen_text": [],
        "speaker_emotion": None,
        "pacing": "normal",
        "camera_move": "static",
        "music_cue": None,
        "audio_sfx": [],
        "claims": [],
        "chart_data": None,
        "humor_note": "",
        "transition_in": "cut",
        "transition_out": "cut",
    }

    validated = Scene.model_validate(scene_data)
    ratios = [beat.sub_duration_ratio for beat in validated.visual_beats]
    assert ratios == [0.25, 0.25, 0.25, 0.25]
    assert sum(ratios) == 1.0


def test_checkpoint_normalizes_legacy_visual_beat_weights_without_llm_call(tmp_path, monkeypatch):
    import json

    source = tmp_path / "draft.json"
    target = tmp_path / "planned.json"
    storyboard = {"title": "t", "scenes": [_scene("s_001")]}
    source.write_text(json.dumps(storyboard), encoding="utf-8")

    old_plan = _plan("s_001")
    old_plan["visual_beats"] = [
        {
            "visual_type": "diagram",
            "visual_description": f"A meaningful visual beat number {index}",
            "asset_strategy": "rendered",
            "provider_id": "internal_renderer",
            "gen_prompt": None,
            "search_queries": [],
            "sub_duration_ratio": 1.0,
            "on_screen_text": [],
        }
        for index in range(1, 5)
    ]
    progress = target.with_name("planned.progress.json")
    checkpoint = {
        "schema_version": "1.0",
        "source_fingerprint": visual_planner._storyboard_fingerprint(storyboard),
        "batch_size": 2,
        "batch_sizes_used": [2],
        "status": "incomplete",
        "completed": {"s_001": old_plan},
        "last_error": "previous validation error",
    }
    progress.write_text(json.dumps(checkpoint), encoding="utf-8")

    class MustNotCallLLM:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("All scenes are already checkpointed")

    monkeypatch.setattr(visual_planner, "StructuredLLMRouter", MustNotCallLLM)
    result = visual_planner.plan_visuals(object(), source, target)

    assert [beat["sub_duration_ratio"] for beat in result["scenes"][0]["visual_beats"]] == [
        0.25, 0.25, 0.25, 0.25
    ]
    updated_checkpoint = json.loads(progress.read_text(encoding="utf-8"))
    assert [beat["sub_duration_ratio"] for beat in updated_checkpoint["completed"]["s_001"]["visual_beats"]] == [
        0.25, 0.25, 0.25, 0.25
    ]
