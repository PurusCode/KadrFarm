from __future__ import annotations

import pytest

from kadr_farm.production_recovery import ProductionJournal, fingerprint


def test_fingerprint_is_stable_for_mapping_order():
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})


def test_completed_task_is_reused_only_for_same_input(tmp_path):
    journal = ProductionJournal(tmp_path)
    fp = fingerprint({"prompt": "same"})
    journal.start_task("director:scene:s_001", stage="director", input_fingerprint=fp,
                       provider="primary", model="quality-model")
    journal.complete_task("director:scene:s_001", stage="director",
                          input_fingerprint=fp, result={"plans": []},
                          schema_name="DirectorBatch", provider="primary",
                          model="quality-model", raw_artifact=None)

    assert journal.cached_result("director:scene:s_001", fp) == {"plans": []}
    assert journal.cached_result("director:scene:s_001", fingerprint({"prompt": "changed"})) is None


def test_rejected_semantic_result_is_not_reused(tmp_path):
    journal = ProductionJournal(tmp_path)
    fp = fingerprint({"prompt": "same"})
    journal.start_task("director:scene:s_002", stage="director", input_fingerprint=fp,
                       provider="primary", model="quality-model")
    journal.complete_task("director:scene:s_002", stage="director",
                          input_fingerprint=fp, result={"plans": []},
                          schema_name="DirectorBatch", provider="primary",
                          model="quality-model", raw_artifact=None)
    assert journal.cached_result("director:scene:s_002", fp) == {"plans": []}
    journal.reject_task("director:scene:s_002", "scene IDs did not match source")
    assert journal.cached_result("director:scene:s_002", fp) is None


def test_raw_model_response_is_persisted_before_validation(tmp_path):
    journal = ProductionJournal(tmp_path)
    call_path = journal.record_model_call(
        task_id="director:batch:s_001", stage="director", provider="primary",
        model="quality-model", request={"user_prompt": "produce a plan"},
        raw_response='{"plans": [}',
        response_metadata={"finish_reason": "stop", "usage": {"total_tokens": 123}},
    )
    payload = __import__("json").loads((journal.run_dir / call_path).read_text(encoding="utf-8"))
    assert payload["raw_response"] == '{"plans": [}'
    assert payload["response_metadata"]["usage"]["total_tokens"] == 123
    assert payload["validation"]["status"] == "not_checked"


def test_restart_marks_running_stage_as_interrupted(tmp_path):
    first = ProductionJournal(tmp_path)
    first.start_stage("visual_production", input_fingerprint="abc")

    resumed = ProductionJournal(tmp_path)
    state = __import__("json").loads(resumed.state_file().read_text(encoding="utf-8"))
    assert state["last_interruption"]["stage"] == "visual_production"
    assert state["stages"]["visual_production"]["status"] == "interrupted"

def test_run_context_rejects_provider_model_or_endpoint_change(tmp_path):
    journal = ProductionJournal(tmp_path)

    original = {
        "provider": "primary",
        "provider_identity": {
            "name": "primary",
            "model": "quality-model",
            "base_url": "https://mws.example/v1",
        },
    }
    journal.set_run_context(original)

    changed_contexts = [
        {
            **original,
            "provider": "secondary",
            "provider_identity": {
                "name": "secondary",
                "model": "quality-model",
                "base_url": "https://mws.example/v1",
            },
        },
        {
            **original,
            "provider_identity": {
                **original["provider_identity"],
                "model": "different-model",
            },
        },
        {
            **original,
            "provider_identity": {
                **original["provider_identity"],
                "base_url": "https://different.example/v1",
            },
        },
    ]

    for changed in changed_contexts:
        with pytest.raises(ValueError, match="provider"):
            journal.set_run_context(changed)

        assert journal.state["run_context"] == original


def test_non_object_state_file_is_quarantined(tmp_path):
    recovery_dir = tmp_path / "recovery"
    recovery_dir.mkdir()
    state_path = recovery_dir / "production_state.json"
    state_path.write_text("[]", encoding="utf-8")

    journal = ProductionJournal(tmp_path)

    assert isinstance(journal.state, dict)
    assert journal.state_path.is_file()
    assert list(recovery_dir.glob("production_state.corrupt_*.json"))


def test_cached_result_rejects_non_object_json(tmp_path):
    journal = ProductionJournal(tmp_path)
    task_id = "director:scene:s_003"
    fp = fingerprint({"prompt": "test"})

    journal.start_task(
        task_id,
        stage="director",
        input_fingerprint=fp,
        provider="primary",
        model="quality-model",
    )
    journal.complete_task(
        task_id,
        stage="director",
        input_fingerprint=fp,
        result={"plans": []},
        schema_name="DirectorBatch",
        provider="primary",
        model="quality-model",
        raw_artifact=None,
    )

    result_path = journal.run_dir / journal.state["tasks"][task_id]["result_file"]
    result_path.write_text("[]", encoding="utf-8")

    assert journal.cached_result(task_id, fp) is None
