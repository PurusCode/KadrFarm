from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from PIL import Image
import pytest

from kadr_farm.visual_story_v2 import VisualStoryPlan
from kadr_farm import visual_story_preview_v2 as preview


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "visual_story_v2_preview_cp04.json"


def _payload() -> dict[str, Any]:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def _plan() -> VisualStoryPlan:
    return VisualStoryPlan.model_validate(_payload())


class _FakeRenderer:
    def __init__(self, *, geometry_errors: list[str] | None = None, warnings: list[str] | None = None):
        self.geometry_errors = geometry_errors or []
        self.warnings = warnings or []
        self.calls: list[tuple[str, float]] = []

    def preflight_shot(self, plan, shot, assets, duration_sec, *, work_dir, event_offsets=None):
        self.calls.append((shot.shot_id, duration_sec))
        assert assets == {}
        assert event_offsets is None
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        return {
            "html_document": "<!doctype html><html><body></body></html>",
            "served_assets": {},
            "layout_manifest": {"status": "passed", "unresolved_count": 0, "fitted_count": 1},
            "geometry_report": {
                "status": "failed" if self.geometry_errors else ("passed_with_warnings" if self.warnings else "passed"),
                "errors": list(self.geometry_errors),
                "warnings": list(self.warnings),
            },
        }


def _fake_capture(prepared_shots, *, plan, snapshot_dir):
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for shot_index, item in enumerate(prepared_shots, start=1):
        path = snapshot_dir / f"{item['shot'].shot_id}_fake.png"
        Image.new("RGB", (plan.width, plan.height), (shot_index * 30, 40, 60)).save(path, format="PNG")
        snapshot = {
            "label": "fake",
            "sample_at_sec": 0.0,
            "file": str(path),
            "sha256": "0" * 64,
        }
        item["report"]["snapshots"] = [snapshot]
        results.append(snapshot)
    return results


def test_cp04_example_is_valid_runtime_plan_and_inside_preview_policy():
    plan = _plan()
    assert len(plan.shots) == 3
    assert preview.validate_preview_scope(plan) == []
    assert plan.model_dump(mode="json")["schema_version"] == "2.0"


def test_cp04_policy_excludes_image_and_video_even_when_runtime_schema_accepts_them():
    payload = _payload()
    payload["shots"][0]["objects"].append({
        "object_id": "external_photo",
        "kind": "image",
        "content": {"path": "assets/photo.jpg"},
    })
    plan = VisualStoryPlan.model_validate(payload)
    issues = preview.validate_preview_scope(plan)
    assert any("image/video assets are deliberately excluded" in item for item in issues)


def test_cp04_policy_recursively_excludes_media_inside_groups():
    payload = _payload()
    group = next(obj for obj in payload["shots"][0]["objects"] if obj["kind"] == "group")
    group["content"]["children"].append({
        "object_id": "nested_clip",
        "kind": "video",
        "content": {"path": "assets/clip.mp4"},
    })
    plan = VisualStoryPlan.model_validate(payload)
    issues = preview.validate_preview_scope(plan)
    assert any("nested_clip" in item and "video assets" in item for item in issues)


def test_cp04_policy_rejects_area_chart_even_though_professional_renderer_supports_it():
    payload = _payload()
    chart = next(obj for obj in payload["shots"][1]["objects"] if obj["kind"] == "chart")
    chart["content"]["chart_type"] = "area"
    plan = VisualStoryPlan.model_validate(payload)
    issues = preview.validate_preview_scope(plan)
    assert any("chart_type='area' is outside CP-04 preview scope" in item for item in issues)


def test_cp04_policy_rejects_actions_outside_the_frozen_direct_preview_subset():
    payload = _payload()
    payload["shots"][1]["events"].append({
        "event_id": "not_in_cp04",
        "target_id": "preview_chart",
        "action": "draw",
        "anchor": {"unit_id": "preview_u02", "offset_sec": 0.1},
        "duration_sec": 0.4,
    })
    plan = VisualStoryPlan.model_validate(payload)
    issues = preview.validate_preview_scope(plan)
    assert any("action 'draw' is outside CP-04 preview scope" in item for item in issues)


def test_cp04_policy_rejects_remote_or_local_poster_assets():
    payload = _payload()
    icon = next(obj for obj in payload["shots"][0]["objects"] if obj["kind"] == "icon")
    icon["content"]["poster"] = "https://example.invalid/poster.png"
    plan = VisualStoryPlan.model_validate(payload)
    issues = preview.validate_preview_scope(plan)
    assert any("poster assets are outside CP-04 preview scope" in item for item in issues)


def test_cp04_policy_limits_preview_event_end_not_just_event_start():
    payload = _payload()
    payload["shots"][0]["events"][0]["anchor"]["offset_sec"] = 11.6
    payload["shots"][0]["events"][0]["duration_sec"] = 0.6
    plan = VisualStoryPlan.model_validate(payload)
    issues = preview.validate_preview_scope(plan)
    assert any("event schedule plus preview tail ends" in item for item in issues)


def test_cp04_rejects_authoring_or_execution_plan_inputs_instead_of_casting_them():
    with pytest.raises(TypeError, match="VisualStoryPlan"):
        preview.validate_preview_scope({"status": "ready", "render_tasks": []})


def test_event_schedule_is_chronological_and_samples_mid_state_or_instant_update():
    plan = _plan()
    schedule = preview._event_schedule(plan.shots[0])
    assert [item["event_id"] for item in schedule] == [
        "callout_reveal", "icon_highlight", "headline_update",
    ]
    assert schedule[0]["sample_state"] == "mid"
    assert schedule[0]["sample_at_sec"] == pytest.approx(0.475)
    assert schedule[2]["sample_state"] == "after"
    assert schedule[2]["sample_at_sec"] == pytest.approx(0.96)


def test_count_up_schedule_samples_progress_before_eased_integer_rounds_to_final():
    plan = _plan()
    shot = next(item for item in plan.shots if item.shot_id == "cp04_process")

    schedule = preview._event_schedule(shot)

    assert len(schedule) == 1
    sample = schedule[0]
    assert sample["action"] == "count_up"
    assert sample["sample_state"] == "progress"
    assert sample["sample_at_sec"] == pytest.approx(0.35)
    assert sample["sample_at_sec"] < sample["start_sec"] + sample["duration_sec"]


def test_preview_duration_uses_explicit_runtime_offsets_and_never_reads_wav():
    plan = _plan()
    assert preview._preview_duration(plan.shots[1]) == pytest.approx(0.5)
    assert preview._preview_duration(plan.shots[0]) == pytest.approx(1.30)


def test_run_preview_writes_contact_sheet_and_audit_with_explicit_non_goals(tmp_path, monkeypatch):
    plan = _plan()
    renderer = _FakeRenderer(warnings=["one object is partially outside the canvas"])
    monkeypatch.setattr(preview, "_capture_prepared_shots", _fake_capture)

    report = preview.run_preview(plan, tmp_path, renderer=renderer)

    assert report["status"] == "passed_with_warnings"
    assert report["scope"]["audio_timing_used"] is False
    assert report["scope"]["wav_files_read"] is False
    assert report["scope"]["network_asset_resolution"] is False
    assert report["scope"]["ffmpeg_used"] is False
    assert report["scope"]["mp4_rendered"] is False
    assert report["scope"]["authoring_to_runtime_adapter_used"] is False
    assert len(renderer.calls) == 3
    assert report["outputs"]["snapshot_count"] == 3

    contact_sheet = Path(report["outputs"]["contact_sheet"])
    audit = Path(report["outputs"]["audit"])
    assert contact_sheet.is_file()
    assert audit.is_file()
    persisted = json.loads(audit.read_text(encoding="utf-8"))
    assert persisted["plan_sha256"] == report["plan_sha256"]
    assert persisted["shots"][0]["status"] == "passed_with_warnings"


def test_run_preview_blocks_policy_violations_before_calling_renderer(tmp_path):
    payload = _payload()
    payload["shots"][0]["objects"].append({
        "object_id": "blocked_media",
        "kind": "image",
        "content": {"path": "private/photo.png"},
    })
    plan = VisualStoryPlan.model_validate(payload)

    class NeverCalledRenderer:
        def preflight_shot(self, *args, **kwargs):
            raise AssertionError("renderer must not run for a policy-blocked plan")

    report = preview.run_preview(plan, tmp_path, renderer=NeverCalledRenderer())
    assert report["status"] == "blocked"
    assert report["outputs"]["contact_sheet"] is None
    assert report["policy_diagnostics"]
    assert Path(report["outputs"]["audit"]).is_file()


def test_run_preview_fails_when_browser_preflight_reports_geometry_errors(tmp_path, monkeypatch):
    plan = _plan()
    renderer = _FakeRenderer(geometry_errors=["headline: text overflow"])

    def capture_must_not_run(*args, **kwargs):
        raise AssertionError("snapshots must not be captured after failed preflight")

    monkeypatch.setattr(preview, "_capture_prepared_shots", capture_must_not_run)
    report = preview.run_preview(plan, tmp_path, renderer=renderer)
    assert report["status"] == "failed"
    assert report["shots"][0]["status"] == "failed"
    assert report["shots"][0]["errors"] == ["headline: text overflow"]
    assert Path(report["outputs"]["audit"]).is_file()


def test_chromium_preview_smoke_is_optional_when_playwright_or_browser_is_unavailable(tmp_path):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright is not installed")

    try:
        with sync_playwright() as playwright:
            launch_options = {"headless": True}
            custom_chromium = os.getenv("KADRFARM_CHROMIUM_PATH", "").strip()
            if custom_chromium:
                launch_options["executable_path"] = str(Path(custom_chromium).expanduser().resolve())
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                launch_options["args"] = ["--no-sandbox", "--disable-setuid-sandbox"]
            browser = playwright.chromium.launch(**launch_options)
            browser.close()
    except Exception as exc:
        pytest.skip(f"Chromium is not installed or cannot launch: {exc}")

    report = preview.run_preview(_plan(), tmp_path)
    assert report["status"] in {"passed", "passed_with_warnings"}
    assert report["outputs"]["snapshot_count"] >= 5
    assert Path(report["outputs"]["contact_sheet"]).is_file()
    assert Path(report["outputs"]["audit"]).is_file()

    process_report = next(item for item in report["shots"] if item["shot_id"] == "cp04_process")
    counter_snapshots = {item["label"]: item for item in process_report["snapshots"]}
    assert "counter_count_up_progress" in counter_snapshots
    assert "final" in counter_snapshots
    assert counter_snapshots["counter_count_up_progress"]["sha256"] != counter_snapshots["final"]["sha256"]
