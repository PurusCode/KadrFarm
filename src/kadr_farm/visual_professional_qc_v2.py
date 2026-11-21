from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable


class VisualProfessionalQCError(ValueError):
    """A deterministic pre-render or visual-output quality failure."""


# Actions that write the same browser property cannot overlap on the same target.
# Different table rows are independent subtargets; text mutations share one content slot.
_ACTION_PROPERTIES: dict[str, tuple[str, ...]] = {
    "fade_in": ("opacity", "visibility"),
    "fade_out": ("opacity", "visibility"),
    "appear": ("opacity", "visibility"),
    "move": ("left", "top"),
    "scale": ("scale",),
    "highlight": ("filter",),
    "draw": ("clip-path", "draw-path.stroke-dashoffset", "draw-mark.transform"),
    "reveal_row": (),
    "count_up": ("text-content",),
    "update_text": ("text-content",),
}


def _event_interval(event: Any, event_offsets: dict[str, float] | None) -> tuple[float, float]:
    raw_start = (event_offsets or {}).get(event.event_id, event.anchor.offset_sec)
    try:
        start = float(raw_start)
        duration = 0.001 if event.action == "update_text" else max(0.001, float(event.duration_sec))
    except (TypeError, ValueError, OverflowError) as exc:
        raise VisualProfessionalQCError(
            f"{event.event_id}: non-numeric event timing"
        ) from exc
    if not math.isfinite(start) or not math.isfinite(duration) or start < 0:
        raise VisualProfessionalQCError(
            f"{event.event_id}: event timing must be finite and non-negative"
        )
    return start, start + duration


def _event_properties(event: Any) -> tuple[str, ...]:
    action = str(event.action)
    properties = _ACTION_PROPERTIES.get(action)
    if properties is None:
        raise VisualProfessionalQCError(
            f"{event.event_id}: unsupported action for professional renderer: {action}"
        )
    if action == "reveal_row":
        row_index = int(event.params.get("row_index", -1))
        if row_index < 0:
            raise VisualProfessionalQCError(
                f"{event.event_id}: reveal_row requires a non-negative row_index"
            )
        return (f"row[{row_index}].opacity", f"row[{row_index}].transform")
    return properties


def validate_event_conflicts(
    shot: Any,
    event_offsets: dict[str, float] | None = None,
) -> None:
    """Reject overlapping writes to the same object property/sub-element.

    Sequential animations are allowed. The CSS generator applies backwards fill
    only to the first animation for each property group; later sequential animations
    use forwards-only fill, explicit keyframe start values, and chronological order.
    """
    by_target: dict[str, list[tuple[float, float, Any, tuple[str, ...]]]] = {}
    for event in shot.events:
        start, end = _event_interval(event, event_offsets)
        properties = _event_properties(event)
        by_target.setdefault(event.target_id, []).append(
            (start, end, event, properties)
        )

    for target_id, entries in by_target.items():
        entries.sort(key=lambda item: (item[0], item[1], item[2].event_id))
        for index, left in enumerate(entries):
            left_start, left_end, left_event, left_props = left
            for right in entries[index + 1:]:
                right_start, right_end, right_event, right_props = right
                if right_start >= left_end - 1e-9:
                    break
                overlap = set(left_props).intersection(right_props)
                if overlap and left_start < right_end - 1e-9:
                    names = ", ".join(sorted(overlap))
                    raise VisualProfessionalQCError(
                        f"{shot.shot_id}/{target_id}: overlapping events "
                        f"{left_event.event_id} [{left_start:.3f},{left_end:.3f}] and "
                        f"{right_event.event_id} [{right_start:.3f},{right_end:.3f}] "
                        f"both write {names}; stagger or redesign the events"
                    )


def validate_hidden_objects_are_revealed(shot: Any) -> None:
    """Every hidden object, including group children, must have a reveal path."""
    reveal_targets = {
        event.target_id
        for event in shot.events
        if event.action in {"appear", "fade_in"}
    }
    from .visual_story_v2 import VisualObject

    def visit(obj: Any, parent_id: str | None = None, depth: int = 0) -> None:
        if depth > 8:
            raise VisualProfessionalQCError(
                f"{shot.shot_id}/{obj.object_id}: group nesting exceeds 8 levels"
            )
        if obj.initial_state == "hidden" and obj.object_id not in reveal_targets:
            location = f" inside group {parent_id}" if parent_id else ""
            raise VisualProfessionalQCError(
                f"{shot.shot_id}/{obj.object_id}{location}: initial_state is hidden but no "
                "appear/fade_in event makes the object visible"
            )
        if obj.kind == "group":
            for child_data in obj.content.get("children", []):
                visit(VisualObject.model_validate(child_data), obj.object_id, depth + 1)

    for obj in shot.objects:
        visit(obj)


def validate_shot_event_contract(
    shot: Any,
    event_offsets: dict[str, float] | None = None,
) -> None:
    validate_hidden_objects_are_revealed(shot)
    validate_event_conflicts(shot, event_offsets)


_BROWSER_GEOMETRY_SCRIPT = r"""(options) => {
  const stage = document.querySelector('.scene-canvas');
  const errors = [];
  const warnings = [];
  const objects = [];
  const media = [];
  if (!stage) {
    return {schema_version: '1.0', status: 'failed', errors: ['scene-canvas is missing'], warnings, objects, media};
  }
  const canvas = stage.getBoundingClientRect();
  const margin = Math.max(0, Math.min(0.15, Number(options.safeAreaMargin ?? 0.04)));
  const safe = {
    left: canvas.left + canvas.width * margin,
    right: canvas.right - canvas.width * margin,
    top: canvas.top + canvas.height * margin,
    bottom: canvas.bottom - canvas.height * margin
  };
  const intersects = (a, b) => Math.min(a.right, b.right) > Math.max(a.left, b.left) &&
    Math.min(a.bottom, b.bottom) > Math.max(a.top, b.top);
  const textSelector = [
    '.type-main', '.type-eyebrow', '.type-subtitle', '.callout-copy',
    '.callout-eyebrow', '.counter-value', '.counter-label',
    '.formula-expression', '.formula-caption', '.graph-node-title',
    '.graph-node-subtitle', '.chart-title', '.chart-subtitle', '.legend-item',
    '.editorial-table th', '.editorial-table td', 'svg text'
  ].join(',');
  const seenErrors = new Set();
  const addError = (message) => { if (!seenErrors.has(message)) { seenErrors.add(message); errors.push(message); } };

  for (const node of document.querySelectorAll('.scene-object')) {
    const id = node.dataset.objectId || '(unnamed)';
    const rect = node.getBoundingClientRect();
    const intersection = {
      left: Math.max(rect.left, canvas.left), right: Math.min(rect.right, canvas.right),
      top: Math.max(rect.top, canvas.top), bottom: Math.min(rect.bottom, canvas.bottom)
    };
    const visibleWidth = Math.max(0, intersection.right - intersection.left);
    const visibleHeight = Math.max(0, intersection.bottom - intersection.top);
    const visibleRatio = rect.width > 0 && rect.height > 0
      ? (visibleWidth * visibleHeight) / (rect.width * rect.height) : 0;
    if (rect.width < 1 || rect.height < 1) addError(`${id}: object has zero-sized geometry`);
    if (visibleWidth < 1 || visibleHeight < 1) addError(`${id}: object is completely outside the canvas`);
    else if (visibleRatio < 0.999) warnings.push(`${id}: object is partially outside the canvas (${(visibleRatio * 100).toFixed(1)}% visible)`);
    objects.push({
      object_id: id,
      x: Number((rect.left - canvas.left).toFixed(2)),
      y: Number((rect.top - canvas.top).toFixed(2)),
      width: Number(rect.width.toFixed(2)),
      height: Number(rect.height.toFixed(2)),
      visible_ratio: Number(visibleRatio.toFixed(4)),
      initial_visibility: getComputedStyle(node).visibility
    });
  }

  for (const element of document.querySelectorAll(textSelector)) {
    if (!String(element.textContent || '').trim()) continue;
    const rect = element.getBoundingClientRect();
    const object = element.closest('.scene-object');
    const id = object?.dataset.objectId || '(unknown object)';
    if (rect.width < 1 || rect.height < 1) {
      addError(`${id}: text element has zero-sized geometry`);
      continue;
    }
    const objectRect = object?.getBoundingClientRect();
    if (objectRect && (rect.left < objectRect.left - 1 || rect.right > objectRect.right + 1 ||
        rect.top < objectRect.top - 1 || rect.bottom > objectRect.bottom + 1)) {
      addError(`${id}: text extends outside its object bounds`);
    }
    // scrollWidth/scrollHeight can include painted glyph overflow even when the
    // element itself allows visible overflow. Treat it as an error only when
    // this element actually clips or scrolls that overflow. Clipping by the
    // main layout containers is checked separately below.
    const textStyle = getComputedStyle(element);
    const clipsHorizontally = ["hidden", "clip", "scroll", "auto"].includes(textStyle.overflowX);
    const clipsVertically = ["hidden", "clip", "scroll", "auto"].includes(textStyle.overflowY);
    const horizontalOverflow = clipsHorizontally && element.scrollWidth > element.clientWidth + 2;
    const verticalOverflow = clipsVertically && element.scrollHeight > element.clientHeight + 2;
    if (horizontalOverflow || verticalOverflow) {
      addError(`${id}: text overflow in ${element.className?.baseVal || element.className || element.tagName}`);
    }
    if (rect.left < safe.left - 1 || rect.right > safe.right + 1 ||
        rect.top < safe.top - 1 || rect.bottom > safe.bottom + 1) {
      addError(`${id}: text crosses the ${Math.round(margin * 100)}% safe-area boundary`);
    }
  }

  for (const box of document.querySelectorAll('.type-layout,.callout,.formula,.counter,.table-shell')) {
    const text = String(box.textContent || '').trim();
    if (!text) continue;
    // scroll dimensions alone do not prove clipping when overflow is visible.
    // Video frames cannot be scrolled, so hidden/clip/auto/scroll are clipping modes.
    const boxStyle = getComputedStyle(box);
    const boxClipsX = ['hidden', 'clip', 'scroll', 'auto'].includes(boxStyle.overflowX);
    const boxClipsY = ['hidden', 'clip', 'scroll', 'auto'].includes(boxStyle.overflowY);
    const clippedX = boxClipsX && box.scrollWidth > box.clientWidth + 2;
    const clippedY = boxClipsY && box.scrollHeight > box.clientHeight + 2;
    if (clippedX || clippedY) {
      const id = box.closest('.scene-object')?.dataset.objectId || '(unknown object)';
      addError(`${id}: content is clipped by its layout container`);
    }
  }

  for (const element of document.querySelectorAll('.media-element')) {
    const object = element.closest('.scene-object');
    const id = object?.dataset.objectId || '(unknown media object)';
    const isVideo = element.tagName.toLowerCase() === 'video';
    const intrinsicWidth = isVideo ? element.videoWidth : element.naturalWidth;
    const intrinsicHeight = isVideo ? element.videoHeight : element.naturalHeight;
    const fit = getComputedStyle(element).objectFit;
    if (!intrinsicWidth || !intrinsicHeight) {
      addError(`${id}: media has no decoded intrinsic dimensions`);
      continue;
    }
    media.push({
      object_id: id,
      media_type: isVideo ? 'video' : 'image',
      intrinsic_width: intrinsicWidth,
      intrinsic_height: intrinsicHeight,
      intrinsic_aspect_ratio: Number((intrinsicWidth / intrinsicHeight).toFixed(5)),
      rendered_aspect_ratio: Number((element.getBoundingClientRect().width / Math.max(1, element.getBoundingClientRect().height)).toFixed(5)),
      object_fit: fit
    });
    if (!['cover', 'contain', 'fill', 'none', 'scale-down'].includes(fit)) {
      addError(`${id}: unsupported object-fit value ${fit}`);
    }
  }

  return {
    schema_version: '1.0',
    status: errors.length ? 'failed' : (warnings.length ? 'passed_with_warnings' : 'passed'),
    viewport: {width: Math.round(canvas.width), height: Math.round(canvas.height)},
    safe_area_margin: margin,
    object_count: objects.length,
    media_count: media.length,
    errors,
    warnings,
    objects,
    media
  };
}"""


async def evaluate_browser_geometry(
    page: Any,
    *,
    safe_area_margin: float = 0.04,
) -> dict[str, Any]:
    """Measure actual Chromium DOM geometry after fonts and media have loaded."""
    margin = float(safe_area_margin)
    if not math.isfinite(margin) or not 0 <= margin <= 0.15:
        raise ValueError("safe_area_margin must be finite and between 0 and 0.15")
    report = await page.evaluate(
        _BROWSER_GEOMETRY_SCRIPT,
        {"safeAreaMargin": margin},
    )
    if not isinstance(report, dict) or "errors" not in report or "warnings" not in report:
        raise VisualProfessionalQCError("Chromium returned an invalid geometry QC report")
    return report


def build_event_timeline(plan: Any, jobs: Iterable[Any]) -> list[dict[str, Any]]:
    """Create the audited global event timeline and reject truncated events.

    The last encoded frame is sampled at (segment_frames - 1) / fps. An event
    must finish by that point, not merely at the exclusive segment end timestamp.
    """
    fps = int(plan.fps)
    if fps <= 0:
        raise VisualProfessionalQCError("fps must be positive")
    shot_by_id = {shot.shot_id: shot for shot in plan.shots}
    report: list[dict[str, Any]] = []
    for job in jobs:
        shot = shot_by_id[job.shot_id]
        offsets = {item.event.event_id: item.offset_sec for item in job.events}
        validate_shot_event_contract(shot, offsets)
        frame_count = int(job.frame_end) - int(job.frame_start)
        if frame_count <= 0:
            raise VisualProfessionalQCError(f"{job.shot_id}: video segment contains no frames")
        segment_duration = frame_count / fps
        last_frame_time = max(0.0, (frame_count - 1) / fps)
        for scheduled in job.events:
            event = scheduled.event
            start = float(scheduled.offset_sec)
            if not math.isfinite(start) or start < 0 or start >= segment_duration:
                raise VisualProfessionalQCError(
                    f"{job.shot_id}/{event.event_id}: event starts outside its video segment"
                )
            duration = 0.0 if event.action == "update_text" else max(0.001, float(event.duration_sec))
            end = start + duration
            if end > last_frame_time + 1e-6:
                raise VisualProfessionalQCError(
                    f"{job.shot_id}/{event.event_id}: event ends at {end:.4f}s, "
                    f"but the last encoded frame is at {last_frame_time:.4f}s; "
                    "shorten the action or extend the shot"
                )
            global_start = int(job.frame_start) / fps + start
            item = {
                "shot_id": job.shot_id,
                "event_id": event.event_id,
                "target_id": event.target_id,
                "action": event.action,
                "anchor_unit_id": event.anchor.unit_id,
                "offset_in_shot_sec": round(start, 6),
                "duration_sec": round(duration, 6),
                "global_video_start_sec": round(global_start, 6),
                "global_video_end_sec": round(global_start + duration, 6),
                "start_frame": int(job.frame_start) + round(start * fps),
                "end_frame": int(job.frame_start) + round((start + duration) * fps),
            }
            report.append(item)
    report.sort(key=lambda item: (item["global_video_start_sec"], item["shot_id"], item["event_id"]))
    return report


def build_semantic_frame_requests(
    jobs: Iterable[Any],
    fps: int,
    event_timeline: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Choose contact-sheet frames at shot and event states, not uniform intervals."""
    fps = int(fps)
    jobs = list(jobs)
    if fps <= 0 or not jobs:
        raise ValueError("positive fps and at least one shot job are required")
    last_frame = max(int(job.frame_end) for job in jobs) - 1
    by_shot: dict[str, list[dict[str, Any]]] = {}
    for event in event_timeline:
        by_shot.setdefault(str(event["shot_id"]), []).append(event)
    requests_by_frame: dict[int, dict[str, Any]] = {}

    def add(frame: int, shot_id: str, state: str, event_id: str | None = None) -> None:
        frame = max(0, min(last_frame, int(frame)))
        request = requests_by_frame.setdefault(frame, {
            "frame_index": frame,
            "timestamp_sec": round(frame / fps, 6),
            "shot_ids": [],
            "event_ids": [],
            "states": [],
        })
        if shot_id not in request["shot_ids"]:
            request["shot_ids"].append(shot_id)
        if event_id and event_id not in request["event_ids"]:
            request["event_ids"].append(event_id)
        if state not in request["states"]:
            request["states"].append(state)

    for job in jobs:
        shot_id = str(job.shot_id)
        first = int(job.frame_start)
        end_exclusive = int(job.frame_end)
        add(first, shot_id, "shot_start")
        add(end_exclusive - 1, shot_id, "shot_end")
        for event in by_shot.get(shot_id, []):
            start = int(event["start_frame"])
            end = int(event["end_frame"])
            event_id = str(event["event_id"])
            add(start, shot_id, "event_start", event_id)
            if end > start:
                add((start + end) // 2, shot_id, "event_middle", event_id)
                add(end, shot_id, "event_end", event_id)

    return [requests_by_frame[index] for index in sorted(requests_by_frame)]


def extract_semantic_contact_frames(
    video_path: str | Path,
    output_dir: str | Path,
    ffmpeg: str,
    requests: list[dict[str, Any]],
) -> list[Path]:
    """Extract exact CFR frame indices with one FFmpeg invocation."""
    video_path = Path(video_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for old in output_dir.glob("semantic_*.jpg"):
        old.unlink(missing_ok=True)
    if not requests:
        raise VisualProfessionalQCError("No semantic contact-sheet frame requests were generated")
    indices = [int(item["frame_index"]) for item in requests]
    if len(indices) != len(set(indices)) or any(index < 0 for index in indices):
        raise VisualProfessionalQCError("Semantic contact-frame indices must be unique and non-negative")
    expression = "+".join(f"eq(n\\,{index})" for index in indices)
    pattern = output_dir / "selected_%04d.jpg"
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path),
        "-vf", f"select={expression},scale=480:-2:flags=lanczos",
        "-vsync", "0", "-frames:v", str(len(requests)), "-q:v", "3", str(pattern),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError("FFmpeg semantic contact-frame extraction failed:\n" + result.stderr[-4000:])
    extracted = sorted(output_dir.glob("selected_*.jpg"))
    if len(extracted) != len(requests):
        raise RuntimeError(
            f"FFmpeg extracted {len(extracted)} semantic frames; expected {len(requests)}"
        )
    final_paths: list[Path] = []
    for index, (source, request) in enumerate(zip(extracted, requests), start=1):
        shots = "-".join(str(item) for item in request["shot_ids"])
        states = "-".join(str(item) for item in request["states"])
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{shots}_{states}")[:72]
        destination = output_dir / f"semantic_{index:04d}_f{int(request['frame_index']):07d}_{safe}.jpg"
        source.replace(destination)
        final_paths.append(destination)
    return final_paths


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items() if isinstance(key, (str, int, float, bool))}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def build_asset_manifest_entry(shot_id: str, asset: Any) -> dict[str, Any]:
    """Record reproducible identity and source information for every resolved asset."""
    path = Path(asset.path).resolve()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    metadata = dict(getattr(asset, "metadata", {}) or {})
    return {
        "shot_id": shot_id,
        "object_id": asset.object_id,
        "path": str(path),
        "media_type": asset.media_type,
        "source": asset.source,
        "source_url": asset.source_url,
        "creator": asset.creator,
        "rights_status": metadata.get("rights_status", "not_applicable"),
        "file_size_bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
        "metadata": _json_safe(metadata),
    }


def build_object_inventory(plan: Any) -> dict[str, Any]:
    objects: list[dict[str, Any]] = []
    kind_counts: dict[str, int] = {}

    def visit(shot_id: str, obj: Any, parent_id: str | None = None, depth: int = 0) -> None:
        if depth > 8:
            raise VisualProfessionalQCError(f"{shot_id}/{obj.object_id}: group nesting exceeds 8")
        record = {
            "shot_id": shot_id,
            "object_id": obj.object_id,
            "parent_id": parent_id,
            "kind": obj.kind,
            "role": obj.role,
            "initial_state": obj.initial_state,
            "continuity_id": obj.continuity_id,
            "z_index": obj.layer.z_index,
            "normalized_bounds": {
                "center_x": obj.layer.x,
                "center_y": obj.layer.y,
                "width": obj.layer.width,
                "height": obj.layer.height,
            },
        }
        objects.append(record)
        kind_counts[obj.kind] = kind_counts.get(obj.kind, 0) + 1
        if obj.kind == "group":
            from .visual_story_v2 import VisualObject
            for child in obj.content.get("children", []):
                visit(shot_id, VisualObject.model_validate(child), obj.object_id, depth + 1)

    for shot in plan.shots:
        for obj in shot.objects:
            visit(shot.shot_id, obj)
    return {"objects": objects, "object_count": len(objects), "counts_by_kind": dict(sorted(kind_counts.items()))}
