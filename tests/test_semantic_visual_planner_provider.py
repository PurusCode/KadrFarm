import json
from types import SimpleNamespace

import pytest

from kadr_farm import visual_planner
from kadr_farm.models import VisualPlanBatch


def _scene():
    return {
        "id": "s_001", "block_id": "b1", "voice_text": "Речь сцены.",
        "meaning": "Проверить выбор провайдера", "purpose": "Тест маршрутизации",
    }


def _plan():
    return dict(
        scene_id="s_001", visual_type="diagram",
        visual_description="A clear explanatory diagram for the narration",
        visual_beats=[], asset_strategy="rendered", provider_id="internal_renderer",
        gen_prompt=None, search_queries=[], on_screen_text=["Смысловая схема"],
        speaker_emotion=None, pacing="normal", camera_move="static", music_cue=None,
        audio_sfx=[], claims=[], chart_data=None, humor_note="",
        transition_in="cut", transition_out="cut",
    )


def test_visual_planner_can_use_secondary_only(tmp_path, monkeypatch):
    source = tmp_path / "draft.json"
    target = tmp_path / "planned.json"
    source.write_text(json.dumps({"title": "t", "scenes": [_scene()]}), encoding="utf-8")
    settings = SimpleNamespace(providers=[
        SimpleNamespace(name="primary", model="mws-model"),
        SimpleNamespace(name="secondary", model="qwen2.5:14b-instruct"),
    ])
    captured = {}

    class FakeRouter:
        def __init__(self, selected_settings):
            captured["providers"] = selected_settings.providers
        def complete(self, schema, *_args, **_kwargs):
            assert schema is VisualPlanBatch
            return VisualPlanBatch(plans=[_plan()])

    monkeypatch.setenv("SEMANTIC_VISUAL_PLANNER_PROVIDER", "secondary")
    monkeypatch.setattr(visual_planner, "StructuredLLMRouter", FakeRouter)

    visual_planner.plan_visuals(settings, source, target, batch_size=1)

    assert len(captured["providers"]) == 1
    assert captured["providers"][0].name == "secondary"
    # The shared settings instance must remain unchanged for other stages.
    assert [p.name for p in settings.providers] == ["primary", "secondary"]


def test_visual_planner_provider_policy_defaults_to_auto(monkeypatch):
    providers = [SimpleNamespace(name="primary"), SimpleNamespace(name="secondary")]
    settings = SimpleNamespace(providers=providers)
    monkeypatch.delenv("SEMANTIC_VISUAL_PLANNER_PROVIDER", raising=False)
    assert visual_planner._visual_planner_settings(settings) is settings


def test_visual_planner_provider_policy_requires_configured_provider(monkeypatch):
    monkeypatch.setenv("SEMANTIC_VISUAL_PLANNER_PROVIDER", "secondary")
    with pytest.raises(ValueError, match="не настроен"):
        visual_planner._visual_planner_settings(
            SimpleNamespace(providers=[SimpleNamespace(name="primary")])
        )


def test_visual_planner_provider_policy_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("SEMANTIC_VISUAL_PLANNER_PROVIDER", "qwen")
    with pytest.raises(ValueError, match="должен быть auto, primary или secondary"):
        visual_planner._visual_planner_settings(SimpleNamespace(providers=[]))
