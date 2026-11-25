"""Deterministic compiler for the Visual Story 2.0 authoring bundle.

This module compiles the CP-01 authoring contracts into the CP-01 ExecutionPlan
model. It is deliberately pure: no filesystem access, network calls, media
resolution, WAV measurement, renderer invocation, or MP4 export occurs here.

The legacy ``visual_scene_compiler_v2.py`` is a separate runtime timeline
compiler and is intentionally not imported or modified.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from .visual_capability_registry_v2 import (
    PILLOW_RENDERER_ID,
    REGISTRY_VERSION,
    get_renderer_capability,
    validate_capability_request,
)
from .visual_story_contract_v2 import (
    AssetRequest,
    Diagnostic,
    ExecutionOutputSpec,
    ExecutionPlan,
    ChartObject,
    GroupObject,
    ImageObject,
    NodeGraphObject,
    RenderTask,
    ShapeObject,
    TableObject,
    RendererTarget,
    SceneCompositionPlan,
    SourceRef,
    VideoObject,
    VisualStoryPlan,
    validate_continuity_references,
    validate_global_plan_against_storyboard,
    validate_scene_plan_coverage,
)
from .visual_story_contract_v2.common import StrictModel, ensure_unique


COMPILER_VERSION = "1.1.0"
CHECKPOINT_SCHEMA_VERSION = "1.0"

# Only exact action-name matches are considered. There is no semantic aliasing
# such as draw_edge -> draw or plot_line -> draw: that would be a hidden change
# in the authored instruction. Geometry/time/value lowering remains a later
# adapter or measured-audio responsibility.
_DIRECT_ACTIONS = frozenset({
    "appear", "fade_in", "fade_out", "highlight", "count_up", "update_text",
})

# CP-01 RenderTask has an object_id field but the existing V2 executors consume
# and emit a complete Shot, not independently rendered object files. This
# reserved ID denotes the whole shot composition, never an authored object.
SHOT_COMPOSITION_OBJECT_ID = "shot_composition"


class CompilationInputError(ValueError):
    """The supplied input cannot be represented as a CP-01 authoring bundle."""


class CheckpointMismatchError(ValueError):
    """A checkpoint does not describe the exact ExecutionPlan being resumed."""


class CompilationCheckpoint(StrictModel):
    """Deterministic identity checkpoint created by the dry-run compiler.

    CP-03 writes no completed render work. ``completed_task_ids`` is therefore
    empty in a fresh compiler result; it exists so a later executor can update
    a checkpoint without changing its identity contract.
    """

    checkpoint_schema_version: Literal["1.0"] = CHECKPOINT_SCHEMA_VERSION
    run_id: str
    execution_plan_sha256: str
    storyboard_sha256: str
    global_plan_sha256: str
    scene_plan_sha256: dict[str, str]
    capability_registry_version: str
    task_fingerprints: dict[str, str]
    completed_task_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_checkpoint_integrity(self):
        for label, value in (
            ("execution_plan_sha256", self.execution_plan_sha256),
            ("storyboard_sha256", self.storyboard_sha256),
            ("global_plan_sha256", self.global_plan_sha256),
        ):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        for task_id, digest in self.task_fingerprints.items():
            if not task_id or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("task_fingerprints must map task IDs to lowercase SHA-256 digests")
        ensure_unique(self.completed_task_ids, "completed task id")
        unknown = sorted(set(self.completed_task_ids) - set(self.task_fingerprints))
        if unknown:
            raise ValueError(f"completed_task_ids contains unknown tasks: {unknown}")
        return self


class TaskAssetBinding(StrictModel):
    """Explicit link from one shot-level render task to its planned media requests."""

    task_id: str
    asset_request_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_requests(self):
        ensure_unique(self.asset_request_ids, "asset request binding")
        return self


@dataclass(frozen=True)
class CompilationResult:
    execution_plan: ExecutionPlan
    checkpoint: CompilationCheckpoint
    asset_bindings: tuple[TaskAssetBinding, ...] = ()

    @property
    def execution_plan_sha256(self) -> str:
        return sha256_model(self.execution_plan)

    def to_canonical_json(self) -> str:
        payload = {
            "execution_plan": self.execution_plan.model_dump(mode="json"),
            "checkpoint": self.checkpoint.model_dump(mode="json"),
            "asset_bindings": [item.model_dump(mode="json") for item in self.asset_bindings],
        }
        return canonical_json(payload)


def canonical_json(value: Any) -> str:
    """Canonical UTF-8-compatible JSON representation used by all fingerprints."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CompilationInputError(f"value cannot be represented as canonical JSON: {exc}") from exc


def sha256_model(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _as_global_plan(value: VisualStoryPlan | Mapping[str, Any]) -> VisualStoryPlan:
    try:
        payload = value.model_dump(mode="python") if isinstance(value, VisualStoryPlan) else value
        return VisualStoryPlan.model_validate(payload)
    except ValidationError as exc:
        raise CompilationInputError(f"global_plan schema validation failed: {exc}") from exc


def _as_scene_plans(values: Sequence[SceneCompositionPlan | Mapping[str, Any]]) -> list[SceneCompositionPlan]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or not values:
        raise CompilationInputError("scene_plans must be a non-empty sequence")
    result: list[SceneCompositionPlan] = []
    for index, value in enumerate(values):
        try:
            payload = value.model_dump(mode="python") if isinstance(value, SceneCompositionPlan) else value
            result.append(SceneCompositionPlan.model_validate(payload))
        except ValidationError as exc:
            raise CompilationInputError(f"scene_plans[{index}] schema validation failed: {exc}") from exc
    return result


def _as_output_spec(value: ExecutionOutputSpec | Mapping[str, Any]) -> ExecutionOutputSpec:
    try:
        payload = value.model_dump(mode="python") if isinstance(value, ExecutionOutputSpec) else value
        return ExecutionOutputSpec.model_validate(payload)
    except ValidationError as exc:
        raise CompilationInputError(f"output_spec schema validation failed: {exc}") from exc


def _diagnostic(
    code: str,
    message: str,
    *,
    severity: Literal["info", "warning", "error"] = "error",
    scene_id: str | None = None,
    shot_id: str | None = None,
    object_id: str | None = None,
) -> Diagnostic:
    source_ref = None
    if scene_id is not None:
        source_ref = SourceRef(scene_id=scene_id, shot_id=shot_id, object_id=object_id)
    return Diagnostic(code=code, severity=severity, message=message[:2000], source_ref=source_ref)


def _model_payload(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _object_subtype(obj: Any) -> tuple[str | None, str | None]:
    content = _model_payload(obj.content)
    if obj.kind == "shape":
        return "shape_type", content.get("shape_type")
    if obj.kind == "icon":
        return "icon_id", content.get("icon_id")
    if obj.kind == "chart":
        return "chart_type", content.get("chart_type")
    if obj.kind == "node_graph":
        return "layout", content.get("layout", "auto")
    return None, None


def _group_depth(object_id: str, objects: Mapping[str, Any], memo: dict[str, int]) -> int:
    if object_id in memo:
        return memo[object_id]
    obj = objects[object_id]
    if not isinstance(obj, GroupObject):
        memo[object_id] = 0
        return 0
    children = obj.content.child_object_ids
    depth = 1 + max((_group_depth(child, objects, memo) for child in children), default=0)
    memo[object_id] = depth
    return depth


def _explicitly_sequential(events: Sequence[Any]) -> bool:
    """True only when the author explicitly orders same-anchor events."""
    return len(events) > 1 and all(
        event.timing.stagger == "sequential" and not event.timing.allow_overlap
        for event in events
    )


def _check_event_conflicts(
    scene_id: str,
    shot_id: str,
    events: Sequence[Any],
    diagnostics: list[Diagnostic],
) -> None:
    by_target_anchor: dict[tuple[str, str], list[Any]] = {}
    for event in events:
        by_target_anchor.setdefault((event.target.object_id, event.trigger.anchor_id), []).append(event)

    visibility_actions = {"appear", "reveal", "fade_in", "fade_out", "hide", "remove"}
    for (target_id, anchor_id), group in by_target_anchor.items():
        if _explicitly_sequential(group):
            # Event list order + explicit sequential stagger is an intentional
            # authored sequence, not an unresolved simultaneous conflict.
            continue
        visibility = [event for event in group if event.action in visibility_actions]
        distinct_visibility = {event.action for event in visibility}
        if len(distinct_visibility) > 1:
            diagnostics.append(_diagnostic(
                "conflicting_visibility_events",
                f"Object {target_id!r} has incompatible visibility actions {sorted(distinct_visibility)} at anchor {anchor_id!r}; use explicit sequential stagger to order them.",
                scene_id=scene_id, shot_id=shot_id, object_id=target_id,
            ))

        moves = [event for event in group if event.action == "move"]
        move_destinations = {
            (getattr(event, "destination_region", None), getattr(event, "destination_anchor", None))
            for event in moves
        }
        if len(move_destinations) > 1:
            diagnostics.append(_diagnostic(
                "conflicting_move_events",
                f"Object {target_id!r} has different move destinations at anchor {anchor_id!r}; use explicit sequential stagger to order them.",
                scene_id=scene_id, shot_id=shot_id, object_id=target_id,
            ))

        scales = [event for event in group if event.action == "scale"]
        scale_targets = {getattr(event, "target_size", None) for event in scales}
        if len(scale_targets) > 1:
            diagnostics.append(_diagnostic(
                "conflicting_scale_events",
                f"Object {target_id!r} has different target sizes at anchor {anchor_id!r}; use explicit sequential stagger to order them.",
                scene_id=scene_id, shot_id=shot_id, object_id=target_id,
            ))


def _group_child_ids(objects: Mapping[str, Any]) -> set[str]:
    return {
        child_id
        for obj in objects.values()
        if isinstance(obj, GroupObject)
        for child_id in obj.content.child_object_ids
    }


def _pillow_nested_media_ids(objects: Mapping[str, Any]) -> set[str]:
    """Find media below direct group children, which Pillow cannot resolve."""
    blocked: set[str] = set()

    def visit(object_id: str, group_depth: int, active: set[str]) -> None:
        if object_id in active:
            return  # CP-01 already rejects cycles; defensive only.
        obj = objects[object_id]
        if isinstance(obj, (ImageObject, VideoObject)) and group_depth > 1:
            blocked.add(object_id)
        if isinstance(obj, GroupObject):
            next_active = set(active)
            next_active.add(object_id)
            for child_id in obj.content.child_object_ids:
                visit(child_id, group_depth + 1, next_active)

    for object_id, obj in objects.items():
        if not isinstance(obj, GroupObject):
            # Root media has depth 0 and direct group-child media depth 1.
            continue
        visit(object_id, 0, set())
    return blocked


def _pillow_progressive_objects(objects: Mapping[str, Any], events: Sequence[Any]) -> list[tuple[str, str]]:
    """Return top-level objects whose complete state requires runtime draw/reveal_row.

    The authoring contract has no exact `draw` runtime action and uses stable row
    IDs while the runtime requires integer row_index. CP-03 therefore blocks
    these combinations instead of claiming an incomplete frame sequence is ready.
    """
    events_by_target: dict[str, list[Any]] = {}
    for event in events:
        events_by_target.setdefault(event.target.object_id, []).append(event)

    makes_visible = {"appear", "reveal", "fade_in", "fade_out", "hide", "remove",
                     "move", "scale", "draw_path", "draw_edge", "reveal_node",
                     "reveal_row", "reveal_column", "fill_progress", "grow_bar",
                     "plot_line", "count_up", "update_text", "update_value",
                     "swap_content", "highlight", "dim", "pulse", "outline", "shake"}
    issues: list[tuple[str, str]] = []
    nested_object_ids = _group_child_ids(objects)
    for object_id, obj in objects.items():
        # Nested children receive no AnimationEngine top-level state map. The
        # SceneRenderer therefore uses their complete-content defaults.
        if object_id in nested_object_ids:
            continue
        target_events = events_by_target.get(object_id, [])
        becomes_visible = obj.initial_state == "visible" or any(
            event.action in makes_visible for event in target_events
        )
        if not becomes_visible:
            continue
        if isinstance(obj, TableObject):
            issues.append((object_id, "Pillow starts top-level tables with zero revealed rows; authoring row IDs cannot be lowered to the runtime integer row_index."))
        elif isinstance(obj, ShapeObject) and obj.content.shape_type in {"line", "arrow"}:
            issues.append((object_id, "Pillow line/arrow rendering starts with draw_progress=0; the authoring contract has no exact runtime draw action."))
        elif isinstance(obj, ChartObject):
            series_lengths = [
                len(series.illustrative_values or series.value_refs)
                for series in obj.content.series
            ]
            if obj.content.chart_type == "bar" and any(length > 1 for length in series_lengths):
                issues.append((object_id, "Pillow multi-category bar charts require runtime draw, which has no exact authoring action mapping."))
            elif obj.content.chart_type == "line" and any(length > 2 for length in series_lengths):
                issues.append((object_id, "Pillow line charts with more than two points require runtime draw, which has no exact authoring action mapping."))
        elif isinstance(obj, NodeGraphObject) and obj.content.edges:
            issues.append((object_id, "Pillow node graphs with edges require runtime draw; authoring draw_edge is not an exact replacement."))
    return issues


def _asset_description(obj: ImageObject | VideoObject) -> str:
    content = obj.content
    parts = [content.subject]
    for value in (
        content.scene,
        content.action,
        content.shot_type,
        content.camera_angle,
        content.mood,
    ):
        if value:
            parts.append(value)
    if isinstance(obj, VideoObject):
        parts.append(f"Desired video action: {content.desired_action}")
    if content.visual_constraints:
        parts.append("Visual constraints: " + "; ".join(content.visual_constraints))
    return " | ".join(parts)


def _collect_external_reference_warnings(
    scene_id: str,
    shot_id: str,
    obj: Any,
    diagnostics: list[Diagnostic],
) -> None:
    content = _model_payload(obj.content)
    refs: list[str] = []
    if obj.kind == "chart":
        refs.extend(ref for series in content.get("series", []) for ref in series.get("value_refs", []))
    elif obj.kind == "table":
        refs.extend(
            cell["value_ref"]
            for row in content.get("rows", [])
            for cell in row.get("cells", [])
            if cell.get("value_ref")
        )
    elif obj.kind == "formula":
        refs.extend(content.get("value_refs", []))
    elif obj.kind == "counter" and content.get("value_ref"):
        refs.append(content["value_ref"])

    if refs:
        unique_refs = sorted(set(refs))
        diagnostics.append(_diagnostic(
            "external_value_refs_not_resolved",
            "External value/provenance references are preserved in the source fingerprint but are not resolved by CP-03: " + ", ".join(unique_refs),
            severity="warning",
            scene_id=scene_id, shot_id=shot_id, object_id=obj.object_id,
        ))


def _build_asset_request(
    scene_id: str,
    shot_id: str,
    obj: ImageObject | VideoObject,
    provider_allowlist: Sequence[str],
) -> AssetRequest:
    content = obj.content
    kwargs: dict[str, Any] = {
        "request_id": _stable_id("asset", scene_id, shot_id, obj.object_id),
        "scene_id": scene_id,
        "shot_id": shot_id,
        "object_id": obj.object_id,
        "media_kind": obj.kind,
        "description": _asset_description(obj),
        "search_queries": list(content.search_queries),
        "orientation": content.orientation,
        "crop_intent": content.crop_intent,
        "provider_allowlist": list(provider_allowlist),
    }
    if isinstance(obj, VideoObject):
        kwargs["desired_duration"] = content.desired_duration
    return AssetRequest.model_validate(kwargs)


def _make_checkpoint(plan: ExecutionPlan) -> CompilationCheckpoint:
    task_fingerprints = {task.task_id: task.input_fingerprint for task in plan.render_tasks}
    return CompilationCheckpoint(
        run_id=plan.run_id,
        execution_plan_sha256=sha256_model(plan),
        storyboard_sha256=plan.storyboard_sha256,
        global_plan_sha256=plan.global_plan_sha256,
        scene_plan_sha256=dict(plan.scene_plan_sha256),
        capability_registry_version=plan.capability_registry_version,
        task_fingerprints=task_fingerprints,
        completed_task_ids=(),
    )


def _make_asset_bindings(plan: ExecutionPlan) -> tuple[TaskAssetBinding, ...]:
    requests_by_shot: dict[tuple[str, str], list[str]] = {}
    for request in plan.asset_requests:
        requests_by_shot.setdefault((request.scene_id, request.shot_id), []).append(request.request_id)
    return tuple(
        TaskAssetBinding(
            task_id=task.task_id,
            asset_request_ids=requests_by_shot.get((task.scene_id, task.shot_id), []),
        )
        for task in plan.render_tasks
    )


def compile_visual_story_to_execution_plan(
    *,
    storyboard: Mapping[str, Any],
    global_plan: VisualStoryPlan | Mapping[str, Any],
    scene_plans: Sequence[SceneCompositionPlan | Mapping[str, Any]],
    run_id: str,
    renderer_id: str,
    output_spec: ExecutionOutputSpec | Mapping[str, Any],
    asset_provider_allowlist: Sequence[str] = (),
) -> CompilationResult:
    """Compile an authoritative authoring bundle into a deterministic dry-run.

    ``renderer_id`` and ``asset_provider_allowlist`` are explicit inputs. There
    is no renderer/provider fallback. The function does not read or write files
    and has no network or rendering side effects.

    Structural bundle errors return a blocked ExecutionPlan with no tasks. Once
    source/link validation passes, capability errors are retained in a blocked
    plan alongside the supported partial task graph for review. A blocked plan
    must never be executed.
    """
    if not isinstance(storyboard, Mapping):
        raise CompilationInputError("storyboard must be a mapping")
    try:
        storyboard_payload = dict(storyboard)
        canonical_json(storyboard_payload)
    except Exception as exc:
        if isinstance(exc, CompilationInputError):
            raise
        raise CompilationInputError(f"storyboard is not JSON-compatible: {exc}") from exc

    global_model = _as_global_plan(global_plan)
    scenes = _as_scene_plans(scene_plans)
    output_model = _as_output_spec(output_spec)
    if not isinstance(run_id, str) or not run_id:
        raise CompilationInputError("run_id must be a non-empty stable ID")
    try:
        caps = get_renderer_capability(renderer_id)
    except KeyError as exc:
        raise CompilationInputError(str(exc)) from exc

    if not isinstance(asset_provider_allowlist, Sequence) or isinstance(asset_provider_allowlist, (str, bytes)):
        raise CompilationInputError("asset_provider_allowlist must be an ordered sequence of provider IDs")
    try:
        provider_list = list(asset_provider_allowlist)
    except TypeError as exc:
        raise CompilationInputError("asset_provider_allowlist must be a sequence of provider IDs") from exc
    provider_errors: list[str] = []
    valid_provider_strings = [item for item in provider_list if isinstance(item, str) and item.strip()]
    if len(valid_provider_strings) != len(provider_list):
        provider_errors.append("asset_provider_allowlist entries must be non-empty strings")
    if len(valid_provider_strings) != len(set(valid_provider_strings)):
        provider_errors.append("asset_provider_allowlist must not contain duplicates")
    if any(item != item.strip() for item in valid_provider_strings):
        provider_errors.append("asset_provider_allowlist entries must not have leading/trailing whitespace")
    if len(provider_list) > 10:
        provider_errors.append("asset_provider_allowlist must contain at most 10 providers")

    global_digest = sha256_model(global_model)
    scene_digests = {scene.scene_id: sha256_model(scene) for scene in scenes}
    diagnostics: list[Diagnostic] = []

    source_scenes = storyboard_payload.get("scenes")
    structural_errors = False
    if not isinstance(source_scenes, list) or not source_scenes:
        diagnostics.append(_diagnostic(
            "invalid_storyboard_scenes",
            "Authoritative storyboard must contain a non-empty scenes list.",
        ))
        structural_errors = True
    else:
        validators = (
            ("global_plan_storyboard_mismatch", lambda: validate_global_plan_against_storyboard(global_model, storyboard_payload)),
            ("scene_plan_coverage_mismatch", lambda: validate_scene_plan_coverage(scenes, source_scenes)),
            ("continuity_reference_invalid", lambda: validate_continuity_references(scenes)),
        )
        for code, validator in validators:
            try:
                validator()
            except Exception as exc:
                diagnostics.append(_diagnostic(code, str(exc)))
                structural_errors = True

    if structural_errors:
        plan = ExecutionPlan(
            run_id=run_id,
            storyboard_sha256=global_model.storyboard_sha256,
            global_plan_sha256=global_digest,
            scene_plan_sha256=scene_digests,
            capability_registry_version=REGISTRY_VERSION,
            asset_requests=[],
            render_tasks=[],
            output_spec=output_model,
            diagnostics=diagnostics,
            status="blocked",
        )
        return CompilationResult(plan, _make_checkpoint(plan), _make_asset_bindings(plan))

    if provider_errors:
        for message in provider_errors:
            diagnostics.append(_diagnostic("invalid_asset_provider_allowlist", message))

    scene_directions = {direction.scene_id: direction for direction in global_model.scenes}
    asset_requests: list[AssetRequest] = []
    render_tasks: list[RenderTask] = []
    object_supported: dict[tuple[str, str, str], bool] = {}
    object_index: dict[tuple[str, str], dict[str, Any]] = {}

    # First pass: validate the complete authored shot against one explicit
    # runtime executor. The runtime renderers consume a whole Shot; they do not
    # emit an independent MP4/PNG file for each VisualObject.
    for scene in scenes:
        for shot in scene.shots:
            key = (scene.scene_id, shot.shot_id)
            objects = {obj.object_id: obj for obj in shot.composition.objects}
            object_index[key] = objects
            child_ids = _group_child_ids(objects)

            if caps.max_objects_per_shot is not None and len(objects) > caps.max_objects_per_shot:
                diagnostics.append(_diagnostic(
                    "renderer_object_limit_exceeded",
                    f"Shot contains {len(objects)} top-level objects; {renderer_id} limit is {caps.max_objects_per_shot}.",
                    scene_id=scene.scene_id, shot_id=shot.shot_id,
                ))

            depth_memo: dict[str, int] = {}
            if any(_group_depth(object_id, objects, depth_memo) > caps.max_group_depth for object_id in objects):
                diagnostics.append(_diagnostic(
                    "renderer_group_depth_exceeded",
                    f"Composition exceeds {renderer_id} maximum group nesting depth {caps.max_group_depth}.",
                    scene_id=scene.scene_id, shot_id=shot.shot_id,
                ))

            nested_media_ids = _pillow_nested_media_ids(objects) if renderer_id == PILLOW_RENDERER_ID else set()
            for obj in shot.composition.objects:
                object_key = (scene.scene_id, shot.shot_id, obj.object_id)
                supported = True
                subtype_field, subtype = _object_subtype(obj)
                issues = validate_capability_request(
                    renderer_id, kind=obj.kind, subtype_field=subtype_field, subtype=subtype,
                )
                for issue in issues:
                    diagnostics.append(_diagnostic(
                        issue.code, issue.message, scene_id=scene.scene_id,
                        shot_id=shot.shot_id, object_id=obj.object_id,
                    ))
                    if issue.severity == "error":
                        supported = False

                # CP-02's data-shape guardrails must also be applied to the
                # authoring models; validate_capability_request checks only
                # kind/subtype/action and cannot see series cardinality.
                if isinstance(obj, ChartObject):
                    series_count = len(obj.content.series)
                    if obj.content.chart_type in {"pie", "donut"} and series_count != 1:
                        diagnostics.append(_diagnostic(
                            "unsupported_chart_series_count",
                            f"{obj.content.chart_type} rendering consumes exactly one series; received {series_count}.",
                            scene_id=scene.scene_id, shot_id=shot.shot_id, object_id=obj.object_id,
                        ))
                        supported = False
                    elif renderer_id == PILLOW_RENDERER_ID and obj.content.chart_type == "bar" and series_count != 1:
                        diagnostics.append(_diagnostic(
                            "unsupported_chart_series_count",
                            f"Pillow bar rendering consumes only the first series; received {series_count}.",
                            scene_id=scene.scene_id, shot_id=shot.shot_id, object_id=obj.object_id,
                        ))
                        supported = False

                if obj.object_id in nested_media_ids:
                    diagnostics.append(_diagnostic(
                        "nested_media_depth_unsupported",
                        "Pillow asset preparation resolves media only in top-level objects and direct group children; deeper nested media is not supported.",
                        scene_id=scene.scene_id, shot_id=shot.shot_id, object_id=obj.object_id,
                    ))
                    supported = False

                object_supported[object_key] = supported
                _collect_external_reference_warnings(scene.scene_id, shot.shot_id, obj, diagnostics)

            if renderer_id == PILLOW_RENDERER_ID:
                for object_id, message in _pillow_progressive_objects(objects, shot.events):
                    diagnostics.append(_diagnostic(
                        "pillow_progressive_state_unrepresentable", message,
                        scene_id=scene.scene_id, shot_id=shot.shot_id, object_id=object_id,
                    ))
                    object_supported[(scene.scene_id, shot.shot_id, object_id)] = False

            _check_event_conflicts(scene.scene_id, shot.shot_id, shot.events, diagnostics)
            for event in shot.events:
                if event.target.object_id in child_ids:
                    diagnostics.append(_diagnostic(
                        "nested_object_event_target_unsupported",
                        "Runtime events can address only top-level shot objects; a child inside a group cannot be targeted independently.",
                        scene_id=scene.scene_id, shot_id=shot.shot_id, object_id=event.target.object_id,
                    ))
                    continue

                runtime_action = event.action if event.action in _DIRECT_ACTIONS else None
                source_obj = objects[event.target.object_id]
                if runtime_action is None:
                    reason = "no exact runtime action mapping exists; no alias or visual substitution is allowed"
                    if event.action in {"move", "scale"}:
                        reason = "the authoring destination region/anchor or semantic target size cannot be lowered to runtime numeric geometry/scale in CP-03"
                    elif event.action == "reveal_row":
                        reason = "authoring stable row_id cannot be lowered to the runtime integer row_index"
                    diagnostics.append(_diagnostic(
                        "unsupported_action_mapping",
                        f"Authoring action {event.action!r} is blocked for {renderer_id}: {reason}.",
                        scene_id=scene.scene_id, shot_id=shot.shot_id, object_id=event.target.object_id,
                    ))
                    continue

                if event.action == "highlight" and (
                    getattr(event, "tone", "accent") != "accent"
                    or getattr(event, "intensity", "normal") != "normal"
                ):
                    diagnostics.append(_diagnostic(
                        "unsupported_action_parameters",
                        "The runtime highlight capability exposes only its default accent/normal treatment; authored tone or intensity cannot be silently discarded.",
                        scene_id=scene.scene_id, shot_id=shot.shot_id, object_id=event.target.object_id,
                    ))
                    continue

                subtype_field, subtype = _object_subtype(source_obj)
                issues = validate_capability_request(
                    renderer_id, kind=source_obj.kind, action=runtime_action,
                    subtype_field=subtype_field, subtype=subtype,
                    subtarget_id=event.target.subtarget_id,
                )
                for issue in issues:
                    diagnostics.append(_diagnostic(
                        issue.code, issue.message, scene_id=scene.scene_id,
                        shot_id=shot.shot_id, object_id=event.target.object_id,
                    ))

                if event.action == "count_up":
                    diagnostics.append(_diagnostic(
                        "external_value_refs_not_resolved",
                        "count_up value references are retained in the source plan fingerprint but are not resolved by CP-03.",
                        severity="warning", scene_id=scene.scene_id,
                        shot_id=shot.shot_id, object_id=event.target.object_id,
                    ))
                elif event.action == "update_text":
                    diagnostics.append(_diagnostic(
                        "external_content_ref_not_resolved",
                        "update_text content_ref is retained in the source plan fingerprint but is not resolved by CP-03.",
                        severity="warning", scene_id=scene.scene_id,
                        shot_id=shot.shot_id, object_id=event.target.object_id,
                    ))

    # Asset requests are planning records only. Provider names are explicit
    # caller inputs; CP-03 does not validate live provider availability.
    shots_with_media = [
        (scene, shot) for scene in scenes for shot in scene.shots
        if any(isinstance(obj, (ImageObject, VideoObject)) for obj in shot.composition.objects)
    ]
    if shots_with_media and not provider_list:
        for scene, shot in shots_with_media:
            diagnostics.append(_diagnostic(
                "asset_provider_allowlist_required",
                "Image/video objects require an explicit non-empty asset_provider_allowlist; CP-03 does not choose a provider.",
                scene_id=scene.scene_id, shot_id=shot.shot_id,
            ))

    if not provider_errors and provider_list:
        for scene in scenes:
            for shot in scene.shots:
                shot_has_errors = any(
                    item.severity == "error"
                    and item.source_ref is not None
                    and item.source_ref.scene_id == scene.scene_id
                    and item.source_ref.shot_id == shot.shot_id
                    for item in diagnostics
                )
                if shot_has_errors:
                    continue  # Do not emit orphan asset requests for a blocked shot.
                for obj in shot.composition.objects:
                    object_key = (scene.scene_id, shot.shot_id, obj.object_id)
                    if not object_supported.get(object_key, False):
                        continue
                    if isinstance(obj, (ImageObject, VideoObject)):
                        try:
                            asset_requests.append(_build_asset_request(
                                scene.scene_id, shot.shot_id, obj, provider_list,
                            ))
                        except ValidationError as exc:
                            diagnostics.append(_diagnostic(
                                "asset_request_invalid", str(exc),
                                scene_id=scene.scene_id, shot_id=shot.shot_id, object_id=obj.object_id,
                            ))

    if len(caps.output_formats) != 1:
        diagnostics.append(_diagnostic(
            "ambiguous_renderer_output_format",
            f"{renderer_id} must declare exactly one output format for CP-03 task records; declared: {list(caps.output_formats)}.",
        ))
    else:
        expected_media_type = caps.output_formats[0]
        target = RendererTarget(renderer_id=renderer_id, renderer_version=caps.renderer_version)
        asset_ids_by_shot: dict[tuple[str, str], list[str]] = {}
        for request in asset_requests:
            asset_ids_by_shot.setdefault((request.scene_id, request.shot_id), []).append(request.request_id)

        # One RenderTask represents one complete shot composition. Both current
        # V2 renderers consume all objects together; group membership is an
        # in-shot composition relationship, not a separate render dependency.
        for scene in scenes:
            direction = scene_directions[scene.scene_id]
            for shot in scene.shots:
                key = (scene.scene_id, shot.shot_id)
                objects = object_index[key]
                has_media = any(isinstance(obj, (ImageObject, VideoObject)) for obj in objects.values())
                has_shot_error = any(
                    item.severity == "error"
                    and item.source_ref is not None
                    and item.source_ref.scene_id == scene.scene_id
                    and item.source_ref.shot_id == shot.shot_id
                    for item in diagnostics
                )
                if has_shot_error or (has_media and (not provider_list or bool(provider_errors))):
                    continue
                if not all(object_supported.get((scene.scene_id, shot.shot_id, object_id), False) for object_id in objects):
                    continue

                task_id = _stable_id("task", scene.scene_id, shot.shot_id, SHOT_COMPOSITION_OBJECT_ID)
                dependencies: list[str] = []
                fingerprint_payload = {
                    "compiler_version": COMPILER_VERSION,
                    "capability_registry_version": REGISTRY_VERSION,
                    "renderer_id": renderer_id,
                    "renderer_version": caps.renderer_version,
                    "storyboard_sha256": global_model.storyboard_sha256,
                    "global_plan_sha256": global_digest,
                    "scene_plan_sha256": scene_digests[scene.scene_id],
                    "global_scene_direction": _model_payload(direction),
                    "scene_id": scene.scene_id,
                    "shot": _model_payload(shot),
                    "object_id": SHOT_COMPOSITION_OBJECT_ID,
                    "asset_request_ids": asset_ids_by_shot.get(key, []),
                    "asset_provider_allowlist": provider_list,
                    "output_spec": _model_payload(output_model),
                    "depends_on": dependencies,
                }
                render_tasks.append(RenderTask(
                    task_id=task_id,
                    scene_id=scene.scene_id,
                    shot_id=shot.shot_id,
                    object_id=SHOT_COMPOSITION_OBJECT_ID,
                    target=target,
                    depends_on=dependencies,
                    input_fingerprint=hashlib.sha256(canonical_json(fingerprint_payload).encode("utf-8")).hexdigest(),
                    expected_media_type=expected_media_type,
                ))

    if len(render_tasks) > 5000 or len(asset_requests) > 1000:
        diagnostics.append(_diagnostic(
            "execution_plan_limit_exceeded",
            f"Compiled plan exceeds ExecutionPlan limits: render_tasks={len(render_tasks)}/5000, asset_requests={len(asset_requests)}/1000.",
        ))
        render_tasks = []
        asset_requests = []

    diagnostics.sort(key=lambda item: (
        item.source_ref.scene_id if item.source_ref else "",
        item.source_ref.shot_id if item.source_ref and item.source_ref.shot_id else "",
        item.source_ref.object_id if item.source_ref and item.source_ref.object_id else "",
        item.code,
        item.message,
    ))
    if len(diagnostics) > 2000:
        omitted = diagnostics[1999:]
        omitted_errors = sum(item.severity == "error" for item in omitted)
        summary_severity = "error" if omitted_errors else "warning"
        diagnostics = diagnostics[:1999] + [_diagnostic(
            "diagnostics_truncated",
            f"ExecutionPlan diagnostic limit is 2000; omitted {len(omitted)} diagnostics, including {omitted_errors} errors.",
            severity=summary_severity,
        )]
        diagnostics.sort(key=lambda item: (
            item.source_ref.scene_id if item.source_ref else "",
            item.source_ref.shot_id if item.source_ref and item.source_ref.shot_id else "",
            item.source_ref.object_id if item.source_ref and item.source_ref.object_id else "",
            item.code,
            item.message,
        ))
    status = "blocked" if any(item.severity == "error" for item in diagnostics) else "ready"
    plan = ExecutionPlan(
        run_id=run_id,
        storyboard_sha256=global_model.storyboard_sha256,
        global_plan_sha256=global_digest,
        scene_plan_sha256=scene_digests,
        capability_registry_version=REGISTRY_VERSION,
        asset_requests=asset_requests,
        render_tasks=render_tasks,
        output_spec=output_model,
        diagnostics=diagnostics,
        status=status,
    )
    return CompilationResult(plan, _make_checkpoint(plan), _make_asset_bindings(plan))


def validate_checkpoint_compatibility(
    plan: ExecutionPlan,
    checkpoint: CompilationCheckpoint,
) -> None:
    """Raise unless checkpoint identity and every task fingerprint match plan."""
    expected_fingerprints = {task.task_id: task.input_fingerprint for task in plan.render_tasks}
    comparisons = {
        "run_id": (checkpoint.run_id, plan.run_id),
        "storyboard_sha256": (checkpoint.storyboard_sha256, plan.storyboard_sha256),
        "global_plan_sha256": (checkpoint.global_plan_sha256, plan.global_plan_sha256),
        "scene_plan_sha256": (checkpoint.scene_plan_sha256, plan.scene_plan_sha256),
        "capability_registry_version": (checkpoint.capability_registry_version, plan.capability_registry_version),
        "execution_plan_sha256": (checkpoint.execution_plan_sha256, sha256_model(plan)),
        "task_fingerprints": (checkpoint.task_fingerprints, expected_fingerprints),
    }
    mismatches = [name for name, (actual, expected) in comparisons.items() if actual != expected]
    unknown_completed = sorted(set(checkpoint.completed_task_ids) - set(expected_fingerprints))
    if unknown_completed:
        mismatches.append("completed_task_ids")
    if mismatches:
        raise CheckpointMismatchError(
            "checkpoint is not compatible with this ExecutionPlan: " + ", ".join(mismatches)
        )


def serialize_compilation_result(result: CompilationResult) -> str:
    """Return stable JSON for a dry-run artifact; performs no file I/O."""
    return result.to_canonical_json()
