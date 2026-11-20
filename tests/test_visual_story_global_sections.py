from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kadr_farm.config import ProviderConfig
from kadr_farm.production_recovery import ProductionJournal
from kadr_farm.visual_story_global_sections import (
    GlobalStoryOutline,
    PartDirectionPlan,
    assemble_global_story_plan,
    build_part_direction_request,
    validate_global_story_outline,
    validate_part_direction,
)


def _storyboard():
    return {
        "title": "A story about a river",
        "source_script_sha256": "a" * 64,
        "scenes": [
            {"id": "s_001", "block_id": "b1", "meaning": "Source", "purpose": "Introduce the river", "voice_text": "Where does the river begin?"},
            {"id": "s_002", "block_id": "b1", "meaning": "First path", "purpose": "Follow the river", "voice_text": "It passes through the valley."},
            {"id": "s_003", "block_id": "b2", "meaning": "Wider flow", "purpose": "Show the expanding river", "voice_text": "More streams join its course."},
            {"id": "s_004", "block_id": "b2", "meaning": "Resolution", "purpose": "Reach the sea", "voice_text": "At last it reaches the sea."},
        ],
    }


def _outline():
    return {
        "schema_version": "1.0",
        "visual_thesis": "The river is a continuous visual path connecting each stage of the story.",
        "arc_summary": "The visual story begins at the source, follows the river through the valley, gathers tributaries, and resolves at the sea.",
        "style": {"palette": ["#112233", "#ddeeff"], "font_family": "Arial"},
        "arc": ["s_001", "s_002", "s_004"],
        "motifs": [{
            "motif_id": "river_path", "kind": "object",
            "description": "A continuous winding river path", "first_scene": "s_001",
            "returns_in": ["s_003"],
        }],
        "parts": [
            {"part_id": "part_01", "title": "The source", "dramatic_purpose": "Establish the question and introduce the river.", "visual_development": "Reveal the river as a continuous path beginning at its source.", "scene_ids": ["s_001", "s_002"], "opening_link": "Begin with the river source.", "closing_link": "The river leads into the wider valley."},
            {"part_id": "part_02", "title": "The course", "dramatic_purpose": "Expand the river journey and resolve its destination.", "visual_development": "The river gathers tributaries and reaches the sea.", "scene_ids": ["s_003", "s_004"], "opening_link": "Continue the path from the previous part.", "closing_link": "Resolve the path at the sea."},
        ],
    }


def _part(part_id, scenes, handoff):
    return {
        "schema_version": "1.0", "part_id": part_id,
        "part_summary": "This part develops the continuous river visual argument.",
        "handoff_to_next": handoff,
        "scenes": scenes,
    }


def _scene(scene_id, role, purpose, intent, relation, develops, motifs, prev, nxt):
    return {
        "scene_id": scene_id, "narrative_role": role, "purpose": purpose,
        "visual_intent": intent, "relation_to_previous": relation,
        "develops_scene_id": develops, "motif_ids": motifs,
        "link_prev": prev, "link_next": nxt,
    }


def _plans():
    first = _part("part_01", [
        _scene("s_001", "hook", "Introduce the river question", "Show the river source as the beginning of one continuous path.", "introduces", None, ["river_path"], None, "s_002"),
        _scene("s_002", "development", "Follow the river", "Extend the path through the valley without changing its identity.", "extends", "s_001", [], "s_001", "s_003"),
    ], "The path continues beyond the valley and will gather tributaries.")
    second = _part("part_02", [
        _scene("s_003", "turn", "Show the expanding river", "Return to the river path as streams join it.", "extends", "s_002", ["river_path"], "s_002", "s_004"),
        _scene("s_004", "resolution", "Reach the sea", "Resolve the continuous river path at the sea.", "resolves", "s_003", [], "s_003", None),
    ], "The river reaches its final destination and the visual argument resolves.")
    # Keep fixtures as validated Pydantic models: later tests intentionally
    # use model_copy() and model_dump_json() to create altered requests.
    return (
        PartDirectionPlan.model_validate(first),
        PartDirectionPlan.model_validate(second),
    )


def test_outline_requires_exact_scene_coverage_and_order():
    storyboard = _storyboard()
    outline = _outline()
    parsed = validate_global_story_outline(storyboard, outline)
    assert [scene_id for part in parsed.parts for scene_id in part.scene_ids] == [
        "s_001", "s_002", "s_003", "s_004"
    ]
    broken = json.loads(json.dumps(outline))
    broken["parts"][1]["scene_ids"] = ["s_004", "s_003"]
    with pytest.raises(ValueError, match="exactly once"):
        validate_global_story_outline(storyboard, broken)
    no_motifs = json.loads(json.dumps(outline))
    no_motifs["motifs"] = []
    parsed_without_motifs = validate_global_story_outline(
        storyboard, no_motifs
    )
    assert parsed_without_motifs.motifs == []

    non_recurring_motif = json.loads(json.dumps(outline))
    non_recurring_motif["motifs"][0]["returns_in"] = []
    with pytest.raises(ValueError, match="genuine return"):
        validate_global_story_outline(storyboard, non_recurring_motif)


def test_part_validation_enforces_whole_story_links_and_global_motifs():
    storyboard = _storyboard()
    outline = validate_global_story_outline(storyboard, _outline())
    first, second = _plans()
    assert validate_part_direction(storyboard, outline, 0, first).part_id == "part_01"
    assert validate_part_direction(storyboard, outline, 1, second).part_id == "part_02"
    broken = second.model_copy(deep=True)
    broken.scenes[0].motif_ids = []
    with pytest.raises(ValueError, match="motif_ids"):
        validate_part_direction(storyboard, outline, 1, broken)
    broken_links = second.model_copy(deep=True)
    broken_links.scenes[0].link_prev = None
    with pytest.raises(ValueError, match="whole-story adjacency"):
        validate_part_direction(storyboard, outline, 1, broken_links)


def test_later_part_request_changes_when_previous_part_handoff_changes():
    storyboard = _storyboard()
    outline = validate_global_story_outline(storyboard, _outline())
    first, _ = _plans()
    original = build_part_direction_request(storyboard, outline, 1, [first])
    changed_first = first.model_copy(deep=True)
    changed_first.handoff_to_next += " The transition changes to a darker visual tone."
    changed = build_part_direction_request(storyboard, outline, 1, [changed_first])
    assert original.task_id == changed.task_id
    assert original.user_prompt != changed.user_prompt


def test_part_request_carries_global_outline_previous_handoff_and_next_part():
    storyboard = _storyboard()
    outline = validate_global_story_outline(storyboard, _outline())
    first, _ = _plans()
    request = build_part_direction_request(storyboard, outline, 1, [first])
    assert request.task_id.endswith(":part_02:global-sections-v1")
    assert "The path continues beyond the valley" in request.user_prompt
    assert "part_02" in request.user_prompt
    assert "part_01" in request.user_prompt
    assert "s_003" in request.user_prompt and "s_004" in request.user_prompt


def test_assembly_accepts_global_story_without_artificial_motifs():
    storyboard = _storyboard()
    outline_data = _outline()
    outline_data["motifs"] = []
    outline = validate_global_story_outline(storyboard, outline_data)

    first, second = _plans()
    first = first.model_copy(deep=True)
    second = second.model_copy(deep=True)

    for plan in (first, second):
        for scene in plan.scenes:
            scene.motif_ids = []

    final = assemble_global_story_plan(
        storyboard, outline, [first, second]
    )

    assert final.motifs == []
    assert len(final.scenes) == 4
    assert sum(
        scene.develops_scene_id is not None
        for scene in final.scenes
    ) >= 2


def test_assembly_requires_all_parts_and_builds_one_valid_visual_story_plan():
    storyboard = _storyboard()
    outline = validate_global_story_outline(storyboard, _outline())
    first, second = _plans()
    with pytest.raises(ValueError, match="completed part plans"):
        assemble_global_story_plan(storyboard, outline, [first])
    final = assemble_global_story_plan(storyboard, outline, [first, second])
    assert [scene.scene_id for scene in final.scenes] == ["s_001", "s_002", "s_003", "s_004"]
    assert final.motifs[0].returns_in == ["s_003"]


class _Completions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise TimeoutError("simulated MWS timeout")
        content = self.responses.pop(0)
        return SimpleNamespace(
            id=f"fake-{len(self.calls)}", created=1,
            usage={"total_tokens": 25},
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=content, refusal=None),
            )],
        )


class _Client:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=_Completions(responses))


def _response_payloads():
    first, second = _plans()
    return [json.dumps(_outline(), ensure_ascii=False),
            first.model_dump_json(), second.model_dump_json()]


def test_runner_resumes_after_part_failure_and_reuses_completed_checkpoints(tmp_path, monkeypatch):
    import kadr_farm.llm_structured as llm_module
    import kadr_farm.visual_story_global_sections as sections

    storyboard_path = tmp_path / "storyboard.json"
    storyboard_path.write_text(json.dumps(_storyboard(), ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "sections-pilot"

    clients = [_Client(_response_payloads()[:2]), _Client(_response_payloads()[2:])]
    created = []

    class FakeLLMRouter:
        def __init__(self, settings, *, honor_provider_timeouts=False):
            assert honor_provider_timeouts is True
            assert len(settings.providers) == 1
            assert settings.providers[0].name == "primary"
            self.client = clients[len(created)]
            created.append(self.client)
            self.clients = [("primary", "model-a", self.client)]

    monkeypatch.setattr(llm_module, "LLMRouter", FakeLLMRouter)
    monkeypatch.setattr(sections, "load_settings", lambda: SimpleNamespace(providers=[
        ProviderConfig("primary", "https://primary.example/v1", "unused", "model-a", 300.0),
        ProviderConfig("secondary", "http://localhost:11434/v1", "unused2", "model-b", 300.0),
    ]))

    # Outline and part 1 complete. Part 2 has no fake response and times out.
    with pytest.raises(Exception):
        sections.run_global_sections_pilot(
            storyboard_path=storyboard_path, out_dir=output, provider_name="primary",
            timeout_seconds=30, execute=True,
        )
    state = json.loads((output / "recovery" / "production_state.json").read_text(encoding="utf-8"))
    assert state["tasks"][sections.OUTLINE_TASK_ID]["status"] == "completed"
    assert state["tasks"]["visual_story:global_sections:part:part_01:global-sections-v1"]["status"] == "completed"
    assert state["tasks"]["visual_story:global_sections:part:part_02:global-sections-v1"]["status"] == "failed"
    assert len(clients[0].chat.completions.calls) == 3

    report = sections.run_global_sections_pilot(
        storyboard_path=storyboard_path, out_dir=output, provider_name="primary",
        timeout_seconds=30, execute=True,
    )
    assert report["status"] == "complete"
    assert report["scene_count"] == 4
    assert report["part_count"] == 2
    # The second process only calls the unfinished part; outline and part 1 are cached.
    assert len(clients[1].chat.completions.calls) == 1
    assert (output / "visual_story_plan.json").is_file()
    assert (output / "global_story_outline.json").is_file()
    assert (output / "parts" / "part_01.json").is_file()
    assert (output / "parts" / "part_02.json").is_file()


def test_changed_global_outline_invalidates_part_request_fingerprint():
    storyboard = _storyboard()
    outline = validate_global_story_outline(storyboard, _outline())
    original = build_part_direction_request(storyboard, outline, 0, [])

    changed_data = outline.model_dump(mode="json")
    changed_data["visual_thesis"] += " The visual path also changes its emotional meaning."
    changed_outline = GlobalStoryOutline.model_validate(changed_data)
    changed = build_part_direction_request(storyboard, changed_outline, 0, [])

    # The stable task ID identifies the logical part; the changed prompt causes
    # StructuredLLMRouter's full request fingerprint to miss the old checkpoint.
    assert changed.task_id == original.task_id
    assert changed.user_prompt != original.user_prompt


def test_runner_materializes_part_from_journal_checkpoint_after_file_write_failure(
    tmp_path, monkeypatch
):
    import kadr_farm.llm_structured as llm_module
    import kadr_farm.visual_story_global_sections as sections

    storyboard_path = tmp_path / "storyboard.json"
    storyboard_path.write_text(
        json.dumps(_storyboard(), ensure_ascii=False), encoding="utf-8"
    )
    output = tmp_path / "sections-pilot-crash-window"

    payloads = _response_payloads()
    clients = [_Client(payloads[:2]), _Client(payloads[2:])]
    created = []

    class FakeLLMRouter:
        def __init__(self, settings, *, honor_provider_timeouts=False):
            assert honor_provider_timeouts is True
            assert len(settings.providers) == 1
            assert settings.providers[0].name == "primary"
            self.client = clients[len(created)]
            created.append(self.client)
            self.clients = [("primary", "model-a", self.client)]

    monkeypatch.setattr(llm_module, "LLMRouter", FakeLLMRouter)
    monkeypatch.setattr(
        sections,
        "load_settings",
        lambda: SimpleNamespace(providers=[
            ProviderConfig("primary", "https://primary.example/v1", "unused", "model-a", 300.0),
            ProviderConfig("secondary", "http://localhost:11434/v1", "unused2", "model-b", 300.0),
        ]),
    )

    from pathlib import Path

    original_writer = sections._atomic_write_json
    failed_once = {"value": False}

    def fail_before_part_file_is_materialized(path, value):
        path = Path(path)
        if path.name == "part_01.json" and not failed_once["value"]:
            failed_once["value"] = True
            raise OSError("simulated crash after journal checkpoint")
        original_writer(path, value)

    monkeypatch.setattr(sections, "_atomic_write_json", fail_before_part_file_is_materialized)

    with pytest.raises(OSError, match="after journal checkpoint"):
        sections.run_global_sections_pilot(
            storyboard_path=storyboard_path,
            out_dir=output,
            provider_name="primary",
            timeout_seconds=30,
            execute=True,
        )

    state_path = output / "recovery" / "production_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    part_one_task = "visual_story:global_sections:part:part_01:global-sections-v1"
    assert state["tasks"][part_one_task]["status"] == "completed"
    assert state["tasks"][part_one_task]["result_file"]
    assert not (output / "parts" / "part_01.json").exists()
    assert len(clients[0].chat.completions.calls) == 2  # outline + part 1

    # Simulate a new process: keep the journal, but use a fresh client that has
    # only the response for part 2. Outline and part 1 must come from cache.
    monkeypatch.setattr(sections, "_atomic_write_json", original_writer)
    report = sections.run_global_sections_pilot(
        storyboard_path=storyboard_path,
        out_dir=output,
        provider_name="primary",
        timeout_seconds=30,
        execute=True,
    )

    assert report["status"] == "complete"
    assert report["scene_count"] == 4
    assert len(clients[1].chat.completions.calls) == 1  # part 2 only
    assert (output / "parts" / "part_01.json").is_file()
    assert (output / "parts" / "part_02.json").is_file()
    assert (output / "visual_story_plan.json").is_file()
