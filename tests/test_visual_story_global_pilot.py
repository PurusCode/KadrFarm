from types import SimpleNamespace

import pytest

from kadr_farm.common import write_json
from kadr_farm.config import ProviderConfig
from kadr_farm.visual_story_prompts import build_global_director_request
from kadr_farm.visual_story_global_pilot import (
    _request_preview,
    pin_one_provider,
    preflight_existing_journal,
    provider_identity,
)

def _settings():
    return SimpleNamespace(
        providers=[
            ProviderConfig("primary", "https://primary.example/v1", "secret-a", "model-a", 120.0),
            ProviderConfig("secondary", "http://localhost:11434/v1", "secret-b", "model-b", 900.0),
        ]
    )


def _storyboard():
    return {
        "title": "A story about a river",
        "source_script_sha256": "a" * 64,
        "scenes": [
            {
                "id": "s_001", "block_id": "b1", "meaning": "Introduce the river",
                "purpose": "Establish the main question", "voice_text": "Where does the river begin?",
            }
        ],
    }


def test_pin_one_provider_selects_exactly_one_and_does_not_mutate_settings():
    settings = _settings()
    pinned = pin_one_provider(settings, "PRIMARY", 300)

    assert len(pinned.providers) == 1
    assert pinned.providers[0].name == "primary"
    assert pinned.providers[0].timeout_seconds == 300.0
    assert settings.providers[0].timeout_seconds == 120.0
    assert len(settings.providers) == 2


def test_pin_one_provider_can_select_secondary_only_when_explicit():
    pinned = pin_one_provider(_settings(), "secondary", 450)
    assert len(pinned.providers) == 1
    assert pinned.providers[0].name == "secondary"
    assert pinned.providers[0].timeout_seconds == 450.0


@pytest.mark.parametrize("name", ["", "auto", "fallback", "tertiary"])
def test_pin_one_provider_rejects_implicit_or_unknown_provider_names(name):
    with pytest.raises(ValueError, match="provider_name"):
        pin_one_provider(_settings(), name, 300)


@pytest.mark.parametrize("timeout", [True, 0, 9.99, 3600.1, float("nan"), float("inf")])
def test_pin_one_provider_rejects_invalid_timeout(timeout):
    with pytest.raises((TypeError, ValueError)):
        pin_one_provider(_settings(), "primary", timeout)


def test_pin_one_provider_rejects_missing_or_duplicate_provider():
    missing = SimpleNamespace(providers=[])
    with pytest.raises(ValueError, match="Exactly one provider"):
        pin_one_provider(missing, "primary", 300)

    duplicate = SimpleNamespace(providers=[
        ProviderConfig("primary", "https://one.example/v1", "a", "m1", 120),
        ProviderConfig("primary", "https://two.example/v1", "b", "m2", 120),
    ])
    with pytest.raises(ValueError, match="Exactly one provider"):
        pin_one_provider(duplicate, "primary", 300)


def test_provider_identity_does_not_expose_api_key():
    pinned = pin_one_provider(_settings(), "primary", 300)
    identity = provider_identity(pinned)
    assert identity == {
        "name": "primary", "model": "model-a", "base_url": "https://primary.example/v1"
    }
    assert "secret" not in repr(identity)


def test_provider_identity_rejects_multiple_clients():
    with pytest.raises(ValueError, match="exactly one pinned provider"):
        provider_identity(_settings())


def test_request_preview_contains_schema_and_prompt_but_no_api_credentials():
    request = build_global_director_request(_storyboard())
    identity = provider_identity(pin_one_provider(_settings(), "primary", 300))
    preview = _request_preview(request, identity, 300, "b" * 64)

    assert preview["task_id"] == request.task_id
    assert preview["timeout_seconds"] == 300
    assert "api_key" not in preview and "llm_called" not in preview
    assert preview["json_schema"]["title"] == "VisualStoryPlan"
    assert "Where does the river begin?" in preview["user_prompt"]
    assert "secret-a" not in repr(preview)
    assert "secret-b" not in repr(preview)


def test_prompt_spec_and_schema_are_versioned_for_visual_story_1_1():
    request = build_global_director_request(_storyboard())
    assert request.prompt_version == "global-v2"
    schema = request.schema_model.model_json_schema()
    assert "visual_thesis" in schema["properties"]
    assert "arc_summary" in schema["properties"]



def test_dry_run_writes_preview_without_creating_recovery_or_calling_router(tmp_path, monkeypatch):
    import json
    from kadr_farm import visual_story_global_pilot as pilot

    source = tmp_path / "storyboard.json"
    source.write_text(json.dumps(_storyboard(), ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "pilot-output"
    monkeypatch.setattr(pilot, "load_settings", _settings)

    result = pilot.run_global_pilot(
        storyboard_path=source, out_dir=output, provider_name="primary",
        timeout_seconds=300, execute=False,
    )

    assert result["mode"] == "dry_run"
    assert result["llm_called"] is False
    assert (output / "global_request_preview.json").is_file()
    assert not (output / "recovery").exists()
    preview = json.loads((output / "global_request_preview.json").read_text(encoding="utf-8"))
    assert preview["provider_identity"]["name"] == "primary"
    assert "ПОЛНОГО ролика" in preview["user_prompt"]


def test_execute_path_uses_one_pinned_client_and_records_validated_plan(tmp_path, monkeypatch):
    import json
    from kadr_farm import visual_story_global_pilot as pilot
    from kadr_farm.visual_story import VisualStoryPlan

    source = tmp_path / "storyboard.json"
    source.write_text(json.dumps(_storyboard(), ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "pilot-output"
    monkeypatch.setattr(pilot, "load_settings", _settings)

    calls = {}

    class FakeJournal:
        def __init__(self, path):
            calls["journal_path"] = path
        def set_run_context(self, context):
            calls["run_context"] = context
        def start_stage(self, stage, **kwargs):
            calls["stage_started"] = stage
        def reject_task(self, task_id, reason):
            calls["rejected"] = (task_id, reason)
        def complete_stage(self, stage, **kwargs):
            calls["stage_completed"] = stage
        def finish_run(self, **kwargs):
            calls["finished"] = kwargs
        def fail_stage(self, stage, error):
            calls["failed"] = (stage, str(error))

    class FakeRouter:
        def __init__(self, settings, **kwargs):
            calls["router_provider_count"] = len(settings.providers)
            calls["router_provider_name"] = settings.providers[0].name
            calls["router_timeout"] = settings.providers[0].timeout_seconds

    plan = VisualStoryPlan.model_validate({
        "schema_version": "1.1",
        "visual_thesis": "The river becomes a visual path from question to answer.",
        "arc_summary": "The story begins with the source of the river, traces its course, and resolves at the sea.",
        "style": {"palette": ["#112233", "#ddeeff"], "font_family": "Arial"},
        "arc": ["s_001"],
        "motifs": [],
        "scenes": [{
            "scene_id": "s_001", "narrative_role": "hook",
            "purpose": "Introduce the river question",
            "visual_intent": "Show a winding river disappearing beyond the horizon",
            "relation_to_previous": "introduces", "develops_scene_id": None,
            "motif_ids": [], "link_prev": None, "link_next": None,
        }],
    })

    monkeypatch.setattr(pilot, "ProductionJournal", FakeJournal)
    monkeypatch.setattr(pilot, "StructuredLLMRouter", FakeRouter)
    monkeypatch.setattr(pilot, "execute_prompt", lambda router, request: plan)

    result = pilot.run_global_pilot(
        storyboard_path=source, out_dir=output, provider_name="primary",
        timeout_seconds=300, execute=True,
    )

    assert calls["router_provider_count"] == 1
    assert calls["router_provider_name"] == "primary"
    assert calls["router_timeout"] == 300.0
    assert calls["stage_started"] == "visual_story_global"
    assert calls["stage_completed"] == "visual_story_global"
    assert calls["finished"]["status"] == "complete"
    assert result["status"] == "complete"
    assert result["human_review_required"] is True
    assert (output / "visual_story_plan.json").is_file()
    assert (output / "visual_story_global_report.json").is_file()

def test_preflight_rejects_different_provider_without_mutating_journal(tmp_path):
    output = tmp_path / "pilot-output"
    state_path = output / "recovery" / "production_state.json"
    state_path.parent.mkdir(parents=True)

    original = {
        "run_status": "running",
        "run_id": "original-run",
        "run_context": {
            "provider_identity": {
                "name": "primary",
                "model": "model-a",
                "base_url": "https://primary.example/v1",
            }
        },
        "tasks": {"visual_story:global:global-v2": {"status": "running"}},
        "model_call_count": 1,
    }
    write_json(state_path, original)
    before = state_path.read_bytes()

    with pytest.raises(ValueError, match="different"):
        preflight_existing_journal(
            output,
            {
                "name": "secondary",
                "model": "model-b",
                "base_url": "http://localhost:11434/v1",
            },
        )

    assert state_path.read_bytes() == before


def test_preflight_rejects_legacy_tasks_without_provider_identity(tmp_path):
    output = tmp_path / "pilot-output"
    state_path = output / "recovery" / "production_state.json"
    state_path.parent.mkdir(parents=True)

    write_json(state_path, {
        "run_context": {},
        "tasks": {"old-task": {"status": "completed"}},
        "model_call_count": 1,
    })

    with pytest.raises(ValueError, match="no saved provider identity"):
        preflight_existing_journal(
            output,
            {"name": "primary", "model": "model-a",
             "base_url": "https://primary.example/v1"},
        )


def test_preflight_allows_matching_provider_identity(tmp_path):
    output = tmp_path / "pilot-output"
    state_path = output / "recovery" / "production_state.json"
    state_path.parent.mkdir(parents=True)

    identity = {
        "name": "primary",
        "model": "model-a",
        "base_url": "https://primary.example/v1",
    }
    write_json(state_path, {
        "run_context": {
            "pilot": "visual_story_global",
            "provider_identity": identity,
        },
        "tasks": {"old-task": {"status": "running"}},
        "model_call_count": 1,
    })

    before = state_path.read_bytes()
    preflight_existing_journal(output, identity)
    assert state_path.read_bytes() == before


def test_preflight_rejects_journal_from_different_pipeline(tmp_path):
    output = tmp_path / "pilot-output"
    state_path = output / "recovery" / "production_state.json"
    state_path.parent.mkdir(parents=True)

    identity = {
        "name": "primary",
        "model": "model-a",
        "base_url": "https://primary.example/v1",
    }
    write_json(state_path, {
        "run_context": {
            "pilot": "another_pipeline",
            "provider_identity": identity,
        },
        "tasks": {"old-task": {"status": "completed"}},
        "model_call_count": 1,
    })

    before = state_path.read_bytes()

    with pytest.raises(ValueError, match="does not belong"):
        preflight_existing_journal(output, identity)

    assert state_path.read_bytes() == before
