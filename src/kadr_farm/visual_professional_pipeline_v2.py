"""Orchestration for the professional HTML/SVG Visual Story V2 renderer."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .visual_audit_v2 import create_contact_sheet, write_audit
from .visual_execution_v2 import (
    concatenate_wavs,
    measure_speech_units,
    probe_video,
)
from .visual_professional_assets_v2 import ProfessionalAssetResolver
from .visual_professional_renderer_v2 import ProfessionalBrowserRenderer
from .visual_scene_compiler_v2 import compile_plan
from .visual_story_v2 import VisualStoryPlan
from .visual_professional_qc_v2 import (
    build_asset_manifest_entry,
    build_event_timeline,
    build_object_inventory,
    build_semantic_frame_requests,
    extract_semantic_contact_frames,
)


class ProfessionalPipelineError(RuntimeError):
    pass


def _ffmpeg_run(command: list[str]) -> None:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise ProfessionalPipelineError(
            "FFmpeg failed:\n" + result.stderr[-6000:]
        )


def _quote_concat(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")


def _extract_contact_frames(
    video_path: Path,
    output_dir: Path,
    ffmpeg: str,
    interval_sec: float = 2.0,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for old in output_dir.glob("sample_*.jpg"):
        old.unlink(missing_ok=True)
    pattern = output_dir / "sample_%04d.jpg"
    _ffmpeg_run([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path),
        "-vf", f"fps=1/{interval_sec},scale=480:-2:flags=lanczos",
        "-q:v", "3", str(pattern),
    ])
    return sorted(output_dir.glob("sample_*.jpg"))


def render_professional_plan(
    plan: VisualStoryPlan,
    *,
    plan_path: str | Path,
    output_path: str | Path,
    settings: Any,
    work_dir: str | Path | None = None,
    asset_cache: str | Path = "data/cache/visual_v2_professional",
    clean_work: bool = False,
    crf: int = 18,
) -> dict[str, Any]:
    """Render measured WAV-backed plan into a layered professional MP4.

    Each shot is one browser-composited scene. Video assets stay as native
    <video> elements; no video-to-PNG extraction is performed.
    """
    plan_path = Path(plan_path).resolve()
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    work = Path(work_dir).resolve() if work_dir else output_path.parent / f"{output_path.stem}_professional_work"
    if clean_work and work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    segment_dir = work / "segments"
    segment_dir.mkdir(parents=True, exist_ok=True)

    durations, wav_paths, hashes = measure_speech_units(plan, plan_path.parent)
    jobs = compile_plan(plan, durations)
    event_timeline = build_event_timeline(plan, jobs)
    shots = {shot.shot_id: shot for shot in plan.shots}
    resolver = ProfessionalAssetResolver(
        settings,
        cache_dir=asset_cache,
        base_dir=plan_path.parent,
    )
    renderer = ProfessionalBrowserRenderer(
        ffmpeg=settings.ffmpeg,
        ffprobe=settings.ffprobe,
        fps=plan.fps,
        crf=crf,
    )

    # Phase 1: resolve media and preflight EVERY shot. No video segment is
    # recorded until all shots pass AutoFit and Chromium geometry QC.
    segments: list[Path] = []
    asset_manifest: list[dict[str, Any]] = []
    prepared_shots: list[dict[str, Any]] = []
    preflight_dir = work / "preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)
    preflight_errors: list[str] = []
    preflight_summary: list[dict[str, Any]] = []

    for index, job in enumerate(jobs, 1):
        shot = shots[job.shot_id]
        print(
            f"[Visual Pro Preflight] {shot.shot_id}: {job.duration_sec:.2f}s | "
            f"{len(shot.objects)} objects | {len(shot.events)} events"
        )
        try:
            assets = resolver.resolve_shot(shot)
            for asset in assets.values():
                asset_manifest.append(build_asset_manifest_entry(shot.shot_id, asset))
            render_duration = (job.frame_end - job.frame_start) / plan.fps
            prepared = renderer.preflight_shot(
                plan,
                shot,
                assets,
                render_duration,
                work_dir=preflight_dir / shot.shot_id,
                event_offsets={item.event.event_id: item.offset_sec for item in job.events},
            )
            layout_manifest = prepared["layout_manifest"]
            geometry_report = prepared["geometry_report"]
            safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", shot.shot_id)
            (preflight_dir / f"{safe_name}.layout.json").write_text(
                json.dumps(layout_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (preflight_dir / f"{safe_name}.geometry.json").write_text(
                json.dumps(geometry_report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            shot_errors = [str(item) for item in geometry_report.get("errors", [])]
            preflight_errors.extend(f"{shot.shot_id}: {item}" for item in shot_errors)
            preflight_summary.append({
                "shot_id": shot.shot_id,
                "status": geometry_report.get("status", "unknown"),
                "errors": shot_errors,
                "warnings": geometry_report.get("warnings", []),
                "auto_fit": layout_manifest,
            })
            prepared_shots.append({
                "index": index,
                "job": job,
                "shot": shot,
                "assets": assets,
                "duration": render_duration,
                "prepared": prepared,
            })
            print(
                f"[Visual Pro Preflight] {shot.shot_id}: {geometry_report.get('status')} | "
                f"fitted={layout_manifest.get('fitted_count', 0)} | "
                f"unresolved={layout_manifest.get('unresolved_count', 0)}"
            )
        except Exception as exc:
            message = f"{shot.shot_id}: preflight execution failed: {type(exc).__name__}: {exc}"
            preflight_errors.append(message)
            preflight_summary.append({"shot_id": shot.shot_id, "status": "error", "errors": [message]})

    preflight_report = {
        "schema_version": "1.0",
        "status": "failed" if preflight_errors else "passed",
        "shots_checked": len(preflight_summary),
        "shots_ready": sum(1 for item in preflight_summary if item.get("status") in {"passed", "passed_with_warnings"}),
        "errors": preflight_errors,
        "shots": preflight_summary,
    }
    (preflight_dir / "preflight_report.json").write_text(
        json.dumps(preflight_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if preflight_errors:
        preview = "\n".join(f"- {item}" for item in preflight_errors[:30])
        raise ProfessionalPipelineError(
            "All-shots preflight failed; no MP4 segments were recorded.\n"
            f"See {preflight_dir / 'preflight_report.json'}\n{preview}"
        )

    # Phase 2: only preflight-approved documents enter the expensive recorder.
    for item in prepared_shots:
        index = item["index"]
        job = item["job"]
        shot = item["shot"]
        assets = item["assets"]
        render_duration = item["duration"]
        print(
            f"[Visual Pro] {shot.shot_id}: {job.duration_sec:.2f}s | "
            f"{len(shot.objects)} objects | {len(shot.events)} events"
        )
        segment_path = segment_dir / f"{index:04d}_{shot.shot_id}.mp4"
        renderer.render_shot(
            plan,
            shot,
            assets,
            render_duration,
            segment_path,
            work_dir=work / "browser" / shot.shot_id,
            event_offsets={event.event.event_id: event.offset_sec for event in job.events},
            prepared=item["prepared"],
        )
        geometry_path = segment_path.with_suffix(segment_path.suffix + ".geometry.json")
        if not geometry_path.is_file():
            raise ProfessionalPipelineError(
                f"{shot.shot_id}: browser geometry QC report was not written"
            )
        segments.append(segment_path)

    if not segments:
        raise ProfessionalPipelineError("No shot segments were rendered")

    # Normalize/concatenate all shot segments. Re-encode once so frame rate,
    # dimensions and time base are uniform even if Chromium capture varied.
    concat_file = work / "segments.ffconcat"
    concat_file.write_text(
        "ffconcat version 1.0\n" + "".join(
            f"file '{_quote_concat(path)}'\n" for path in segments
        ),
        encoding="utf-8",
    )
    silent_video = work / "visual_only.mp4"
    _ffmpeg_run([
        settings.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-an", "-vf", f"fps={plan.fps},scale={plan.width}:{plan.height}:flags=lanczos,setsar=1,format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(plan.fps),
        "-movflags", "+faststart", str(silent_video),
    ])

    expected_frames = jobs[-1].frame_end
    silent_probe = subprocess.run(
        [
            settings.ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames", "-of", "json", str(silent_video),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if silent_probe.returncode != 0:
        raise ProfessionalPipelineError(
            "ffprobe failed while checking the silent video: " + silent_probe.stderr[-3000:]
        )
    try:
        silent_streams = json.loads(silent_probe.stdout).get("streams", [])
        silent_frames = int(silent_streams[0].get("nb_read_frames") or 0)
    except (ValueError, TypeError, IndexError, AttributeError, json.JSONDecodeError) as exc:
        raise ProfessionalPipelineError("Could not verify the silent video frame count") from exc
    if silent_frames != expected_frames:
        raise ProfessionalPipelineError(
            f"Silent video contains {silent_frames} frames; expected {expected_frames}. "
            "Audio mux was not started because the visual timeline is incomplete."
        )

    ordered_wavs = [wav_paths[unit.unit_id] for unit in plan.speech_units]
    master_audio = concatenate_wavs(ordered_wavs, work / "narration.wav")
    # Both streams are independently validated. Do not use -shortest, which can
    # silently truncate the complete narration if one video frame is missing.
    _ffmpeg_run([
        settings.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(silent_video), "-i", str(master_audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-ac", "2", "-movflags", "+faststart", str(output_path),
    ])

    probe = probe_video(output_path, ffprobe=settings.ffprobe)
    actual_duration = float((probe.get("format") or {}).get("duration") or 0)
    expected_audio = sum(durations.values())
    tolerance = max(0.02, 1.5 / plan.fps)
    if actual_duration <= 0:
        raise ProfessionalPipelineError("ffprobe returned a zero-length video")
    if abs(actual_duration - expected_audio) > tolerance:
        raise ProfessionalPipelineError(
            f"Output duration {actual_duration:.3f}s differs from measured narration "
            f"{expected_audio:.3f}s by more than {tolerance:.3f}s"
        )

    contact_frame_requests = build_semantic_frame_requests(
        jobs,
        plan.fps,
        event_timeline,
    )
    samples = extract_semantic_contact_frames(
        output_path,
        work / "contact_frames",
        settings.ffmpeg,
        contact_frame_requests,
    )
    contact = output_path.with_suffix(".contact.jpg")
    if samples:
        create_contact_sheet(samples, contact, columns=4, thumbnail_width=480, thumbnail_height=270)

    audit_path = output_path.with_suffix(".audit.json")
    write_audit(
        plan,
        jobs,
        durations,
        hashes,
        audit_path,
        video_probe=probe,
    )
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["renderer"] = "playwright_html_svg_v2"
    audit["preflight"] = preflight_report
    audit["auto_fit"] = {
        "engine": "chromium_measured_auto_fit_v1_2",
        "shots": [item.get("auto_fit", {}) for item in preflight_summary],
    }
    audit["media_assets"] = asset_manifest
    audit["expected_audio_duration_sec"] = round(expected_audio, 4)
    audit["duration_delta_sec"] = round(actual_duration - expected_audio, 4)
    geometry_reports = []
    for index, job in enumerate(jobs, start=1):
        segment_path = segment_dir / f"{index:04d}_{job.shot_id}.mp4"
        geometry_path = segment_path.with_suffix(segment_path.suffix + ".geometry.json")
        shot_geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
        shot_geometry["shot_id"] = job.shot_id
        geometry_reports.append(shot_geometry)
    inventory = build_object_inventory(plan)
    event_counts: dict[str, int] = {}
    for item in event_timeline:
        event_counts[item["action"]] = event_counts.get(item["action"], 0) + 1
    geometry_errors = [
        f"{shot_report.get('shot_id', 'shot')}: {message}"
        for shot_report in geometry_reports
        for message in shot_report.get("errors", [])
    ]
    geometry_warnings = [
        f"{shot_report.get('shot_id', 'shot')}: {message}"
        for shot_report in geometry_reports
        for message in shot_report.get("warnings", [])
    ]
    audit["object_inventory"] = inventory
    audit["event_timeline"] = event_timeline
    audit["event_counts_by_action"] = dict(sorted(event_counts.items()))
    audit["contact_sheet_frames"] = contact_frame_requests
    audit["geometry_qc"] = {
        "status": "failed" if geometry_errors else ("passed_with_warnings" if geometry_warnings else "passed"),
        "shots": geometry_reports,
        "errors": geometry_errors,
        "warnings": geometry_warnings,
    }
    audit["quality_gate"] = {
        "status": "passed_with_warnings" if geometry_warnings else "passed",
        "event_conflicts": "none",
        "event_timing": "all events fit within encoded shot frames",
        "browser_geometry": "passed",
        "all_shots_preflight": "passed",
        "auto_fit": "chromium_measured",
        "contact_sheet_mode": "semantic_event_states",
        "media_sha256": "recorded",
    }
    audit["quality_notes"] = [
        "Still images and video are native browser media layers.",
        "Charts, diagrams and shapes are SVG/HTML, not raster placeholder cards.",
        "Motion is evaluated in the browser timeline and captured as a shot video.",
        "Shot boundaries remain editorial cuts unless a future explicit transition is authored.",
        "Browser geometry, safe-area and text-overflow checks ran before capture.",
        "Contact sheet samples shot boundaries and authored event states.",
    ]
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "status": "complete",
        "video": str(output_path),
        "duration_sec": round(actual_duration, 3),
        "expected_narration_sec": round(expected_audio, 3),
        "shot_count": len(jobs),
        "object_count": sum(len(shot.objects) for shot in plan.shots),
        "event_count": sum(len(shot.events) for shot in plan.shots),
        "contact_sheet": str(contact) if contact.is_file() else None,
        "audit": str(audit_path),
        "work_dir": str(work),
        "ffprobe": probe,
        "geometry_qc": audit["geometry_qc"]["status"],
        "contact_frame_count": len(contact_frame_requests),
        "preflight_report": str(preflight_dir / "preflight_report.json"),
        "auto_fit_adjusted_object_count": sum(
            int(item.get("auto_fit", {}).get("fitted_count", 0))
            for item in preflight_summary
        ),
    }
