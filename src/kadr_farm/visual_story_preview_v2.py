"""Bounded, audit-first visual preview for the existing runtime Visual Story V2.

This module deliberately accepts ``visual_story_v2.VisualStoryPlan`` only. It is
not an adapter for ``visual_story_contract_v2`` or the CP-03 ExecutionPlan.
It uses the Professional Browser Renderer V2 preflight and takes browser
screenshots; it never records video or resolves/downloads media assets.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable

from PIL import Image

from .visual_audit_v2 import create_contact_sheet
from .visual_capability_registry_v2 import (
    PROFESSIONAL_RENDERER_ID,
    REGISTRY_VERSION,
    get_renderer_capability,
    validate_shot_capabilities,
)
from .visual_professional_qc_v2 import build_object_inventory


PREVIEW_SCHEMA_VERSION = "1.0"
PREVIEW_KINDS = frozenset({
    "text", "shape", "icon", "callout", "group", "node_graph",
    "chart", "table", "formula", "counter",
})
PREVIEW_ACTIONS = frozenset({
    "appear", "fade_in", "fade_out", "highlight", "count_up", "update_text",
})
PREVIEW_SUBTYPES: dict[str, dict[str, frozenset[str]]] = {
    "shape": {"shape_type": frozenset({
        "rectangle", "rounded_rectangle", "circle", "ellipse", "line", "arrow", "polygon",
    })},
    "icon": {"icon_id": frozenset({"check", "cross", "warning", "play"})},
    "chart": {"chart_type": frozenset({"bar", "line"})},
    "node_graph": {"layout": frozenset({"left_to_right", "top_to_bottom"})},
}

# Conservative preview-run limits. These are CP-04 tool limits, not claims about
# the underlying renderer's maximum capacity.
PREVIEW_MAX_SHOTS = 6
PREVIEW_MAX_OBJECTS_PER_SHOT = 24
PREVIEW_MAX_EVENTS_PER_SHOT = 8
PREVIEW_MAX_EVENT_END_SEC = 12.0
PREVIEW_MAX_GROUP_DEPTH = 8
PREVIEW_TAIL_SEC = 0.15
# The renderer's count_up uses cubic ease-out. Sampling at 25% of the
# timeline keeps the preview frame inside the transition instead of often
# rounding a small integer counter all the way to its final value.
COUNT_UP_PREVIEW_SAMPLE_FRACTION = 0.25


def _safe_name(value: str, *, fallback: str = "item") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._-")
    return (cleaned[:72] or fallback)


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _walk_objects(objects: Iterable[Any], *, shot_id: str) -> Iterable[tuple[Any, int, str | None]]:
    """Yield top-level and nested runtime objects without resolving any assets."""
    from .visual_story_v2 import VisualObject

    def visit(raw: Any, depth: int, parent_id: str | None):
        obj = raw if isinstance(raw, VisualObject) else VisualObject.model_validate(raw)
        yield obj, depth, parent_id
        if depth > PREVIEW_MAX_GROUP_DEPTH:
            return
        if obj.kind == "group":
            children = obj.content.get("children", [])
            if not isinstance(children, list):
                raise PreviewPolicyError(f"{shot_id}/{obj.object_id}: group.children must be a list")
            for child in children:
                yield from visit(child, depth + 1, obj.object_id)

    for item in objects:
        yield from visit(item, 0, None)


def validate_preview_scope(plan: Any) -> list[str]:
    """Return deterministic CP-04 policy diagnostics for a runtime V2 plan."""
    from .visual_story_v2 import VisualStoryPlan

    if not isinstance(plan, VisualStoryPlan):
        raise TypeError(
            "CP-04 preview accepts only kadr_farm.visual_story_v2.VisualStoryPlan; "
            "authoring plans and CP-03 ExecutionPlan are not runtime plans"
        )

    issues: list[str] = []
    if len(plan.shots) > PREVIEW_MAX_SHOTS:
        issues.append(f"plan has {len(plan.shots)} shots; CP-04 preview limit is {PREVIEW_MAX_SHOTS}")

    for shot in plan.shots:
        if len(shot.events) > PREVIEW_MAX_EVENTS_PER_SHOT:
            issues.append(
                f"{shot.shot_id}: {len(shot.events)} events exceed CP-04 limit "
                f"{PREVIEW_MAX_EVENTS_PER_SHOT}"
            )

        try:
            objects = list(_walk_objects(shot.objects, shot_id=shot.shot_id))
        except Exception as exc:
            issues.append(f"{shot.shot_id}: invalid nested object structure: {type(exc).__name__}: {exc}")
            continue

        if len(objects) > PREVIEW_MAX_OBJECTS_PER_SHOT:
            issues.append(
                f"{shot.shot_id}: {len(objects)} objects exceed CP-04 limit "
                f"{PREVIEW_MAX_OBJECTS_PER_SHOT}"
            )

        for obj, depth, parent_id in objects:
            location = f"{shot.shot_id}/{obj.object_id}"
            if depth > PREVIEW_MAX_GROUP_DEPTH:
                issues.append(
                    f"{location}: group nesting depth {depth} exceeds {PREVIEW_MAX_GROUP_DEPTH}"
                )
            if obj.kind not in PREVIEW_KINDS:
                issues.append(
                    f"{location}: kind {obj.kind!r} is outside CP-04 preview scope; "
                    "image/video assets are deliberately excluded"
                )
                continue
            if obj.content.get("poster"):
                issues.append(f"{location}: poster assets are outside CP-04 preview scope")

            for field_name, allowed in PREVIEW_SUBTYPES.get(obj.kind, {}).items():
                value = obj.content.get(field_name)
                if value is not None and value not in allowed:
                    issues.append(
                        f"{location}: {obj.kind}.{field_name}={value!r} is outside CP-04 preview scope; "
                        f"allowed: {', '.join(sorted(allowed))}"
                    )

        max_event_end = 0.0
        for event in shot.events:
            if event.action not in PREVIEW_ACTIONS:
                issues.append(
                    f"{shot.shot_id}/{event.event_id}: action {event.action!r} is outside CP-04 preview scope; "
                    f"allowed: {', '.join(sorted(PREVIEW_ACTIONS))}"
                )
            start = float(event.anchor.offset_sec)
            duration = float(event.duration_sec)
            if not math.isfinite(start) or not math.isfinite(duration) or start < 0 or duration < 0:
                issues.append(f"{shot.shot_id}/{event.event_id}: event timing must be finite and non-negative")
                continue
            max_event_end = max(max_event_end, start + max(duration, 0.001))
        capture_end = max_event_end + (PREVIEW_TAIL_SEC if shot.events else 0.0)
        if capture_end > PREVIEW_MAX_EVENT_END_SEC:
            issues.append(
                f"{shot.shot_id}: event schedule plus preview tail ends at {capture_end:.3f}s; "
                f"CP-04 preview limit is {PREVIEW_MAX_EVENT_END_SEC:.1f}s"
            )

    return issues


def _event_schedule(shot: Any) -> list[dict[str, Any]]:
    """Build preview sample points from explicit runtime offsets, not from WAV."""
    scheduled: list[dict[str, Any]] = []
    indexed = list(enumerate(shot.events))
    indexed.sort(key=lambda item: (float(item[1].anchor.offset_sec), item[0]))
    for _index, event in indexed:
        start = float(event.anchor.offset_sec)
        duration = max(0.001, float(event.duration_sec))
        if event.action == "update_text":
            sample_at = start + 0.06
            state = "after"
        elif event.action == "count_up":
            sample_at = start + duration * COUNT_UP_PREVIEW_SAMPLE_FRACTION
            state = "progress"
        else:
            sample_at = start + duration / 2.0
            state = "mid"
        scheduled.append({
            "event_id": str(event.event_id),
            "action": str(event.action),
            "target_id": str(event.target_id),
            "start_sec": round(start, 4),
            "duration_sec": round(float(event.duration_sec), 4),
            "sample_at_sec": round(sample_at, 4),
            "sample_state": state,
        })
    return scheduled


def _preview_duration(shot: Any) -> float:
    if not shot.events:
        return 0.5
    end = max(
        float(event.anchor.offset_sec) + max(float(event.duration_sec), 0.001)
        for event in shot.events
    )
    return min(PREVIEW_MAX_EVENT_END_SEC, max(0.5, end + PREVIEW_TAIL_SEC))


def _block_external_request(route: Any) -> None:
    url = str(route.request.url)
    if url.startswith(("http://", "https://")):
        route.abort()
    else:
        route.continue_()


def _capture_shot_snapshots(
    browser: Any,
    *,
    plan: Any,
    shot: Any,
    html_document: str,
    output_dir: Path,
) -> list[dict[str, Any]]:
    """Capture initial, per-event and final states from the real Chromium DOM."""
    output_dir.mkdir(parents=True, exist_ok=True)
    context = browser.new_context(
        viewport={"width": plan.width, "height": plan.height},
        device_scale_factor=1,
        reduced_motion="no-preference",
    )
    try:
        page = context.new_page()
        page.set_default_timeout(45000)
        page.route("**/*", _block_external_request)
        page.set_content(html_document, wait_until="load")
        page.wait_for_function("window.__KADRFARM_READY__ === true", timeout=45000)

        media_errors = page.evaluate("window.__KADRFARM_MEDIA_ERRORS__ || []")
        if media_errors:
            raise RuntimeError(f"{shot.shot_id}: browser reported media errors: {media_errors}")

        expected_ids = [
            str(obj.object_id)
            for obj, _depth, _parent_id in _walk_objects(shot.objects, shot_id=shot.shot_id)
        ]
        actual_ids = [
            str(item.get_attribute("data-object-id"))
            for item in page.locator(".scene-object").all()
        ]
        if Counter(actual_ids) != Counter(expected_ids):
            raise RuntimeError(
                f"{shot.shot_id}: rendered DOM object IDs differ from runtime shot; "
                f"expected={expected_ids}, actual={actual_ids}"
            )

        engine_errors = page.locator(".engine-error").all_text_contents()
        if engine_errors:
            raise RuntimeError(f"{shot.shot_id}: renderer emitted engine-error nodes: {engine_errors}")

        snapshots: list[dict[str, Any]] = []

        def capture(label: str, sample_at: float | None) -> None:
            file_name = f"{_safe_name(shot.shot_id)}_{len(snapshots) + 1:02d}_{_safe_name(label)}.png"
            path = output_dir / file_name
            page.screenshot(path=str(path), full_page=False, animations="allow")
            with Image.open(path) as image:
                if image.format != "PNG" or image.size != (plan.width, plan.height):
                    raise RuntimeError(
                        f"{shot.shot_id}: screenshot {file_name} has unexpected format/size "
                        f"{image.format} {image.size}"
                    )
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshots.append({
                "label": label,
                "sample_at_sec": None if sample_at is None else round(sample_at, 4),
                "file": str(path),
                "sha256": digest,
            })

        capture("initial", None)
        if shot.events:
            page.evaluate("""() => {
                window.__KADRFARM_PREVIEW_T0__ = performance.now();
                window.dispatchEvent(new Event('kadrfarm:start'));
            }""")
            schedule = _event_schedule(shot)
            for item in schedule:
                sample_ms = max(0, round(float(item["sample_at_sec"]) * 1000))
                page.wait_for_function(
                    "target => performance.now() >= window.__KADRFARM_PREVIEW_T0__ + target",
                    arg=sample_ms,
                    timeout=45000,
                )
                capture(f"{item['event_id']}_{item['sample_state']}", item["sample_at_sec"])

            final_at = max(
                float(event.anchor.offset_sec) + max(float(event.duration_sec), 0.001)
                for event in shot.events
            ) + PREVIEW_TAIL_SEC
            final_ms = max(0, round(final_at * 1000))
            page.wait_for_function(
                "target => performance.now() >= window.__KADRFARM_PREVIEW_T0__ + target",
                arg=final_ms,
                timeout=45000,
            )
            capture("final", final_at)

        return snapshots
    finally:
        context.close()


def _capture_prepared_shots(
    prepared_shots: list[dict[str, Any]],
    *,
    plan: Any,
    snapshot_dir: Path,
) -> list[dict[str, Any]]:
    """Capture all preflight-approved shots in one Chromium process."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is required for CP-04 preview. Install with: "
            "python -m pip install -e '.[visual]' && python -m playwright install chromium"
        ) from exc

    launch_args = ["--force-color-profile=srgb", "--disable-background-timer-throttling"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        launch_args.extend(["--no-sandbox", "--disable-setuid-sandbox"])
    launch_options: dict[str, Any] = {"headless": True, "args": launch_args}
    custom_chromium = os.getenv("KADRFARM_CHROMIUM_PATH", "").strip()
    if custom_chromium:
        executable = Path(custom_chromium).expanduser()
        if not executable.is_file():
            raise RuntimeError(f"KADRFARM_CHROMIUM_PATH does not exist: {executable}")
        launch_options["executable_path"] = str(executable.resolve())

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    all_snapshots: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_options)
        try:
            for item in prepared_shots:
                snapshots = _capture_shot_snapshots(
                    browser,
                    plan=plan,
                    shot=item["shot"],
                    html_document=item["html_document"],
                    output_dir=snapshot_dir,
                )
                item["report"]["snapshots"] = snapshots
                all_snapshots.extend(snapshots)
        finally:
            browser.close()
    return all_snapshots


def run_preview(
    plan: Any,
    output_dir: str | Path,
    *,
    renderer: Any | None = None,
) -> dict[str, Any]:
    """Preflight and preview a small runtime V2 plan without MP4/audio/media IO.

    The plan's runtime event offsets are used only to sample preview states. They
    are not measured against WAV files and must not be interpreted as audio sync.
    """
    from .visual_story_v2 import VisualStoryPlan
    from .visual_professional_renderer_v2 import ProfessionalBrowserRenderer

    if not isinstance(plan, VisualStoryPlan):
        raise TypeError(
            "run_preview requires a VisualStoryPlan from visual_story_v2; "
            "CP-01 authoring plans and CP-03 ExecutionPlan are not accepted"
        )

    policy_issues = validate_preview_scope(plan)
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    plan_payload = plan.model_dump(mode="json")
    plan_sha = _canonical_sha256(plan_payload)
    run_dir = output_root / f"preview_{plan_sha[:12]}"
    run_dir.mkdir(parents=True, exist_ok=True)

    capability = get_renderer_capability(PROFESSIONAL_RENDERER_ID)
    try:
        object_inventory = build_object_inventory(plan)
    except Exception as exc:
        object_inventory = {
            "objects": [],
            "object_count": 0,
            "counts_by_kind": {},
            "inventory_error": f"{type(exc).__name__}: {exc}",
        }
        if not policy_issues:
            policy_issues.append(f"object inventory could not be built: {type(exc).__name__}: {exc}")

    report: dict[str, Any] = {
        "schema_version": PREVIEW_SCHEMA_VERSION,
        "preview_mode": "runtime_visual_story_v2",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "title": plan.title,
        "plan_sha256": plan_sha,
        "renderer": {
            "renderer_id": PROFESSIONAL_RENDERER_ID,
            "renderer_version": capability.renderer_version,
            "capability_registry_version": REGISTRY_VERSION,
        },
        "scope": {
            "audio_timing_used": False,
            "wav_files_read": False,
            "network_asset_resolution": False,
            "external_http_requests_allowed": False,
            "ffmpeg_used": False,
            "mp4_rendered": False,
            "image_video_assets_allowed": False,
            "authoring_to_runtime_adapter_used": False,
        },
        "policy_diagnostics": policy_issues,
        "object_inventory": object_inventory,
        "shots": [],
        "outputs": {"contact_sheet": None, "snapshot_count": 0},
        "status": "blocked" if policy_issues else "pending",
    }

    if policy_issues:
        report_path = run_dir / "preview_audit.json"
        report["outputs"]["audit"] = str(report_path)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report

    if renderer is None:
        renderer = ProfessionalBrowserRenderer()

    prepared_shots: list[dict[str, Any]] = []
    all_snapshots: list[dict[str, Any]] = []
    failed = False
    warnings_present = False

    for shot in plan.shots:
        shot_report: dict[str, Any] = {
            "shot_id": shot.shot_id,
            "status": "pending",
            "event_schedule": _event_schedule(shot),
            "capability_report": None,
            "layout_manifest": None,
            "geometry_report": None,
            "snapshots": [],
            "errors": [],
        }
        capability_report = validate_shot_capabilities(shot, PROFESSIONAL_RENDERER_ID)
        shot_report["capability_report"] = capability_report.as_dict()
        if not capability_report.supported:
            shot_report["status"] = "blocked"
            shot_report["errors"].extend(
                item.message for item in capability_report.diagnostics if item.severity == "error"
            )
            report["shots"].append(shot_report)
            failed = True
            continue

        try:
            preview_duration = _preview_duration(shot)
            prepared = renderer.preflight_shot(
                plan,
                shot,
                {},
                preview_duration,
                work_dir=run_dir / "preflight" / _safe_name(shot.shot_id),
                event_offsets=None,
            )
            if prepared.get("served_assets"):
                raise RuntimeError(
                    f"{shot.shot_id}: preflight unexpectedly registered media assets; "
                    "CP-04 preview is asset-free"
                )
            layout_manifest = prepared.get("layout_manifest")
            geometry_report = prepared.get("geometry_report")
            shot_report["layout_manifest"] = layout_manifest
            shot_report["geometry_report"] = geometry_report
            if not isinstance(layout_manifest, dict) or "unresolved_count" not in layout_manifest:
                shot_report["errors"].append("AutoFit preflight did not return unresolved_count")
                layout_unresolved = 0
            else:
                layout_unresolved = int(layout_manifest.get("unresolved_count") or 0)
            if not isinstance(geometry_report, dict) or "errors" not in geometry_report:
                shot_report["errors"].append("Browser geometry preflight did not return an errors list")
                geometry_errors = []
            else:
                geometry_errors = list(geometry_report.get("errors") or [])
                if not geometry_errors and geometry_report.get("status") not in {"passed", "passed_with_warnings"}:
                    shot_report["errors"].append(
                        f"Browser geometry status is not passing: {geometry_report.get('status')!r}"
                    )
            if layout_unresolved:
                shot_report["errors"].append(
                    f"AutoFit unresolved_count={layout_unresolved}"
                )
            if geometry_errors:
                shot_report["errors"].extend(str(item) for item in geometry_errors)
            if shot_report["errors"]:
                shot_report["status"] = "failed"
                failed = True
                report["shots"].append(shot_report)
                continue
            shot_report["status"] = (
                "passed_with_warnings"
                if geometry_report.get("warnings")
                else "passed"
            )
            warnings_present = warnings_present or shot_report["status"] == "passed_with_warnings"
            prepared_shots.append({
                "shot": shot,
                "html_document": str(prepared["html_document"]),
                "report": shot_report,
            })
            report["shots"].append(shot_report)
        except Exception as exc:
            shot_report["status"] = "failed"
            shot_report["errors"].append(f"{type(exc).__name__}: {exc}")
            report["shots"].append(shot_report)
            failed = True

    snapshot_dir = run_dir / "frames"
    if prepared_shots:
        try:
            all_snapshots = _capture_prepared_shots(
                prepared_shots,
                plan=plan,
                snapshot_dir=snapshot_dir,
            )
        except Exception as exc:
            failed = True
            report.setdefault("capture_errors", []).append(f"{type(exc).__name__}: {exc}")
            for item in prepared_shots:
                item["report"]["status"] = "failed"
                item["report"]["errors"].append(
                    f"Snapshot capture failed: {type(exc).__name__}: {exc}"
                )

    contact_sheet_path: Path | None = None
    if all_snapshots:
        # Only files from this execution are supplied; stale files in a reused
        # output directory are never included in the contact sheet.
        contact_sheet_path = create_contact_sheet(
            [Path(item["file"]) for item in all_snapshots],
            run_dir / "preview_contact_sheet.jpg",
            columns=3,
            thumbnail_width=480,
            thumbnail_height=270,
        )

    if failed:
        report["status"] = "failed"
    elif warnings_present:
        report["status"] = "passed_with_warnings"
    else:
        report["status"] = "passed"

    report["outputs"] = {
        "contact_sheet": str(contact_sheet_path) if contact_sheet_path else None,
        "snapshot_count": len(all_snapshots),
        "snapshots": all_snapshots,
        "audit": str(run_dir / "preview_audit.json"),
    }
    report_path = run_dir / "preview_audit.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CP-04: create a bounded, asset-free preview of a runtime Visual Story V2 plan"
    )
    parser.add_argument("plan", type=Path, help="JSON file using the existing runtime visual_story_v2 schema")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/visual_previews"),
        help="Base directory for preview artifacts (default: output/visual_previews)",
    )
    args = parser.parse_args(argv)

    try:
        payload = json.loads(args.plan.read_text(encoding="utf-8-sig"))
        from .visual_story_v2 import VisualStoryPlan
        plan = VisualStoryPlan.model_validate(payload)
        report = run_preview(plan, args.output_dir)
    except Exception as exc:
        print(f"CP-04 preview failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(json.dumps({
        "status": report.get("status"),
        "plan_sha256": report.get("plan_sha256"),
        "outputs": report.get("outputs"),
        "policy_diagnostics": report.get("policy_diagnostics"),
    }, ensure_ascii=False, indent=2))
    return 0 if report.get("status") in {"passed", "passed_with_warnings"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
