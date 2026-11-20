from __future__ import annotations
import json
from types import SimpleNamespace
from kadr_farm.config import ProviderConfig
from kadr_farm.llm_structured import StructuredLLMRouter
from kadr_farm.production_recovery import ProductionJournal
from kadr_farm.visual_story import VisualStoryPlan

def _payload():
    return {
        "schema_version": "1.1",
        "visual_thesis": "A river becomes a path from question to answer.",
        "arc_summary": "The story follows the river from its source to the sea.",
        "style": {"palette": ["#112233", "#ddeeff"], "font_family": "Arial"},
        "arc": ["s_001"], "motifs": [],
        "scenes": [{
            "scene_id": "s_001", "narrative_role": "hook",
            "purpose": "Introduce the river question",
            "visual_intent": "Show a winding river disappearing beyond the horizon",
            "relation_to_previous": "introduces", "develops_scene_id": None,
            "motif_ids": [], "link_prev": None, "link_next": None,
        }],
    }

class _Completions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.responses.pop(0)
        return SimpleNamespace(id=f"fake-{len(self.calls)}", created=1,
            usage={"total_tokens": 42}, choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=content, refusal=None))])

class _Client:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=_Completions(responses))

def _make_router(monkeypatch, client, journal):
    import kadr_farm.llm_structured as module
    settings = SimpleNamespace(providers=[
        ProviderConfig("primary", "https://primary.example/v1", "unused", "model-a", 300.0)])
    class FakeLLMRouter:
        def __init__(self, received, *, honor_provider_timeouts=False):
            assert honor_provider_timeouts is True
            assert len(received.providers) == 1
            assert received.providers[0].name == "primary"
            assert received.providers[0].timeout_seconds == 300.0
            self.clients = [("primary", "model-a", client)]
    monkeypatch.setattr(module, "LLMRouter", FakeLLMRouter)
    return StructuredLLMRouter(settings, journal=journal)

def test_global_director_real_router_journal_checkpoint_reused(tmp_path, monkeypatch):
    client = _Client([json.dumps(_payload(), ensure_ascii=False)])
    journal = ProductionJournal(tmp_path / "pilot")
    router = _make_router(monkeypatch, client, journal)
    kwargs = dict(temperature=0.2, max_tokens=8500,
        task_id="visual_story:global:global-v2", stage="visual_story_global")
    first = router.complete(VisualStoryPlan, "system", "whole video direction", **kwargs)
    assert first.visual_thesis.startswith("A river")
    assert len(client.chat.completions.calls) == 1
    assert journal.state["tasks"]["visual_story:global:global-v2"]["status"] == "completed"
    assert journal.state["model_call_count"] == 1
    resumed = ProductionJournal(tmp_path / "pilot")
    second = _make_router(monkeypatch, client, resumed).complete(
        VisualStoryPlan, "system", "whole video direction", **kwargs)
    assert second.model_dump(mode="json") == first.model_dump(mode="json")
    assert len(client.chat.completions.calls) == 1
    assert resumed.state["model_call_count"] == 1

def test_global_director_repair_call_is_journaled(tmp_path, monkeypatch):
    client = _Client(['{"schema_version":"1.1","visual_thesis":',
                      json.dumps(_payload(), ensure_ascii=False)])
    journal = ProductionJournal(tmp_path / "pilot")
    result = _make_router(monkeypatch, client, journal).complete(
        VisualStoryPlan, "system", "whole video direction",
        temperature=0.2, max_tokens=8500,
        task_id="visual_story:global:global-v2", stage="visual_story_global")
    assert result.schema_version == "1.1"
    assert len(client.chat.completions.calls) == 2
    task = journal.state["tasks"]["visual_story:global:global-v2"]
    assert task["status"] == "completed"
    assert len(task["model_calls"]) == 2
    records = [json.loads((journal.run_dir / rel).read_text(encoding="utf-8"))
               for rel in task["model_calls"]]
    assert [record["call_kind"] for record in records] == ["initial", "repair"]
    assert records[0]["validation"]["status"] == "failed"
    assert records[1]["validation"]["status"] == "passed"


def _child_hold_pilot_lock(output, acquired, release):
    from pathlib import Path
    from kadr_farm.visual_story_global_pilot import _pilot_directory_lock

    with _pilot_directory_lock(Path(output)):
        acquired.set()

        if not release.wait(15):
            raise RuntimeError("test process was not released in time")


def _child_try_pilot_lock(output, acquired, timed_out):
    from pathlib import Path
    from kadr_farm.visual_story_global_pilot import _pilot_directory_lock

    try:
        with _pilot_directory_lock(
            Path(output),
            timeout_seconds=0.4,
        ):
            acquired.set()

    except TimeoutError:
        timed_out.set()


def test_global_director_lock_serializes_processes(tmp_path):
    import multiprocessing

    from kadr_farm.visual_story_global_pilot import _pilot_directory_lock

    context = multiprocessing.get_context("spawn")
    output = tmp_path / "shared-pilot-output"
    output.mkdir()

    first_acquired = context.Event()
    second_acquired = context.Event()
    second_timed_out = context.Event()
    release_first = context.Event()

    first = context.Process(
        target=_child_hold_pilot_lock,
        args=(str(output), first_acquired, release_first),
    )

    second = context.Process(
        target=_child_try_pilot_lock,
        args=(str(output), second_acquired, second_timed_out),
    )

    first.start()

    try:
        assert first_acquired.wait(10), (
            "first process did not acquire the lock"
        )

        second.start()

        assert second_timed_out.wait(10), (
            "second process did not time out while the lock was held"
        )

        assert not second_acquired.is_set(), (
            "second process entered the directory while it was locked"
        )

    finally:
        release_first.set()

        first.join(10)

        if first.is_alive():
            first.terminate()
            first.join(5)

        if second.pid is not None:
            second.join(10)

            if second.is_alive():
                second.terminate()
                second.join(5)

    assert first.exitcode == 0
    assert second.exitcode == 0

    # Confirm that the lock becomes available after the first process exits.
    with _pilot_directory_lock(output, timeout_seconds=1):
        pass
