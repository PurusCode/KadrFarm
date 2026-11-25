"""Evidence-based renderer capability registry for Visual Story runtime V2.

The registry describes only the existing executable runtime contract in
``visual_story_v2.py``. It does not claim that the new authoring contract in
``visual_story_contract_v2`` can be passed to these renderers directly.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

REGISTRY_VERSION = "1.0.0"
RUNTIME_CONTRACT_ID = "kadrfarm.visual_story_v2"

PROFESSIONAL_RENDERER_ID = "professional_browser_v2"
PILLOW_RENDERER_ID = "pillow_animation_v2"

RUNTIME_OBJECT_KINDS = (
    "image", "video", "text", "shape", "icon", "callout", "group",
    "node_graph", "chart", "table", "formula", "counter",
)
RUNTIME_ACTIONS = (
    "appear", "fade_in", "fade_out", "highlight", "move", "scale",
    "draw", "reveal_row", "count_up", "update_text",
)
SHAPE_TYPES = (
    "rectangle", "rounded_rectangle", "circle", "ellipse", "line", "arrow", "polygon",
)
CHART_TYPES = ("bar", "line", "pie", "donut", "area")
PILLOW_CHART_TYPES = ("bar", "line", "pie", "donut")
ICON_IDS = ("check", "cross", "warning", "play")
GRAPH_LAYOUTS = ("left_to_right", "top_to_bottom")


@dataclass(frozen=True)
class KindCapability:
    kind: str
    subtypes: tuple[tuple[str, tuple[str, ...]], ...] = ()
    notes: tuple[str, ...] = ()

    def values_for(self, field_name: str) -> tuple[str, ...] | None:
        for name, values in self.subtypes:
            if name == field_name:
                return values
        return None


@dataclass(frozen=True)
class ActionCapability:
    action: str
    target_kinds: tuple[str, ...]
    target_subtypes: tuple[tuple[str, tuple[str, ...]], ...] = ()
    subtarget_mode: str = "none"
    behavior: str = ""

    def allowed_subtypes_for(self, kind: str) -> tuple[str, ...] | None:
        for name, values in self.target_subtypes:
            if name == kind:
                return values
        return None


@dataclass(frozen=True)
class RendererCapability:
    renderer_id: str
    renderer_version: str
    display_name: str
    runtime_contract: str
    integration_status: str
    kinds: tuple[KindCapability, ...]
    actions: tuple[ActionCapability, ...]
    chart_types: tuple[str, ...]
    output_formats: tuple[str, ...]
    input_media_formats: tuple[str, ...] | None
    input_format_policy: str
    media_sources: tuple[str, ...]
    max_group_depth: int
    max_objects_per_shot: int | None = None
    max_download_bytes: int | None = None
    media_motion: tuple[str, ...] = ("static",)
    notes: tuple[str, ...] = ()

    @property
    def supported_kinds(self) -> tuple[str, ...]:
        return tuple(item.kind for item in self.kinds)

    @property
    def supported_actions(self) -> tuple[str, ...]:
        return tuple(item.action for item in self.actions)

    def kind(self, kind: str) -> KindCapability | None:
        return next((item for item in self.kinds if item.kind == kind), None)

    def action(self, action: str) -> ActionCapability | None:
        return next((item for item in self.actions if item.action == action), None)

    def as_contract_model(self):
        """Return the CP-01 RendererCapabilities projection.

        The CP-01 model is intentionally a compact summary. Use this registry's
        richer per-kind/per-action records for validation.
        """
        from .visual_story_contract_v2.execution import RendererCapabilities

        return RendererCapabilities(
            renderer_id=self.renderer_id,
            renderer_version=self.renderer_version,
            supported_kinds=list(self.supported_kinds),
            supported_actions=list(self.supported_actions),
            supported_chart_types=list(self.chart_types),
            supported_formats=list(self.output_formats),
            max_objects_per_shot=self.max_objects_per_shot,
            max_nesting_depth=self.max_group_depth,
        )


@dataclass(frozen=True)
class CapabilityDiagnostic:
    code: str
    message: str
    renderer_id: str
    target_id: str | None = None
    severity: str = "error"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "renderer_id": self.renderer_id,
            "target_id": self.target_id,
            "message": self.message,
        }


@dataclass(frozen=True)
class CapabilityReport:
    renderer_id: str
    registry_version: str
    diagnostics: tuple[CapabilityDiagnostic, ...] = ()

    @property
    def supported(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)

    def as_dict(self) -> dict[str, Any]:
        return {
            "registry_version": self.registry_version,
            "renderer_id": self.renderer_id,
            "status": "supported" if self.supported else "blocked",
            "diagnostics": [item.as_dict() for item in self.diagnostics],
        }


class CapabilityValidationError(ValueError):
    def __init__(self, report: CapabilityReport):
        self.report = report
        details = "; ".join(item.message for item in report.diagnostics if item.severity == "error")
        super().__init__(details or "Renderer capability validation failed")


def _kind(kind: str, *, subtypes: dict[str, tuple[str, ...]] | None = None, notes: tuple[str, ...] = ()) -> KindCapability:
    return KindCapability(kind, tuple(sorted((subtypes or {}).items())), notes)


def _action(
    action: str,
    target_kinds: tuple[str, ...],
    *,
    target_subtypes: dict[str, tuple[str, ...]] | None = None,
    subtarget_mode: str = "none",
    behavior: str = "",
) -> ActionCapability:
    return ActionCapability(
        action,
        target_kinds,
        tuple(sorted((target_subtypes or {}).items())),
        subtarget_mode,
        behavior,
    )


_ALL_KINDS = RUNTIME_OBJECT_KINDS
_GRAPHIC_KINDS = ("shape", "chart", "node_graph")
_PILLOW_DRAW_SUBTYPES = {
    "shape": ("line", "arrow"),
    "chart": ("bar", "line"),
}

_PROFESSIONAL_KINDS = (
    _kind("shape", subtypes={"shape_type": SHAPE_TYPES}),
) + tuple(
    _kind("icon", subtypes={"icon_id": ICON_IDS}) if name == "icon" else
    _kind("chart", subtypes={"chart_type": CHART_TYPES}) if name == "chart" else
    _kind("node_graph", subtypes={"layout": GRAPH_LAYOUTS}) if name == "node_graph" else
    _kind(name)
    for name in RUNTIME_OBJECT_KINDS if name != "shape"
)

_PILLOW_KINDS = tuple(
    _kind("chart", subtypes={"chart_type": PILLOW_CHART_TYPES})
    if item.kind == "chart" else item
    for item in _PROFESSIONAL_KINDS
)

_PROFESSIONAL_ACTIONS = (
    _action("appear", _ALL_KINDS, behavior="Instant visibility change on the top-level object."),
    _action("fade_in", _ALL_KINDS, behavior="CSS opacity/visibility animation."),
    _action("fade_out", _ALL_KINDS, behavior="CSS opacity animation."),
    _action("highlight", _ALL_KINDS, behavior="CSS drop-shadow highlight."),
    _action("move", _ALL_KINDS, behavior="Animates top-level object left/top in normalized percent coordinates."),
    _action("scale", _ALL_KINDS, behavior="Animates top-level object scale; factor is clamped to 0.1..4.0."),
    _action("draw", _ALL_KINDS, behavior="Container wipe reveal; path/bar details animate only where renderer markup exposes draw hooks. No stable node/edge/series subtarget."),
    _action("reveal_row", ("table",), subtarget_mode="integer_param:row_index", behavior="Reveals the requested table row by index; no column or stable row-id targeting."),
    _action("count_up", ("counter",), behavior="Timed numeric update of the counter value."),
    _action("update_text", ("text", "callout", "counter"), behavior="Updates the primary text/counter node in a supported text-bearing object."),
)

_PILLOW_ACTIONS = (
    _action("appear", _ALL_KINDS, behavior="Sets visibility before group recursion; supported for group wrappers too."),
    _action("fade_in", tuple(k for k in _ALL_KINDS if k != "group"), behavior="Animates opacity; group state is not applied by SceneRenderer."),
    _action("fade_out", tuple(k for k in _ALL_KINDS if k != "group"), behavior="Animates opacity; group state is not applied by SceneRenderer."),
    _action("highlight", tuple(k for k in _ALL_KINDS if k != "group"), behavior="Draws highlight outline around the object."),
    _action("move", tuple(k for k in _ALL_KINDS if k != "group"), behavior="Applies normalized dx/dy to the object layer."),
    _action("scale", tuple(k for k in _ALL_KINDS if k != "group"), behavior="Scales the object layer."),
    _action("draw", _GRAPHIC_KINDS, target_subtypes=_PILLOW_DRAW_SUBTYPES, behavior="Progressive line/arrow, bar/line chart, or graph-edge rendering. Does not progressively draw other shape/chart subtypes; graph nodes remain visible."),
    _action("reveal_row", ("table",), subtarget_mode="integer_param:row_index", behavior="Reveals rows as a prefix through row_index (not an isolated row)."),
    _action("count_up", ("counter",), behavior="Interpolates counter value over event duration."),
    _action("update_text", ("text",), behavior="Changes the text object's text state; callout/formula/table text is not updated."),
)

RENDERER_CAPABILITIES: tuple[RendererCapability, ...] = (
    RendererCapability(
        renderer_id=PROFESSIONAL_RENDERER_ID,
        renderer_version="module-v2; runtime-schema-2.0; source-version-not-declared",
        display_name="Professional Browser Renderer V2",
        runtime_contract=RUNTIME_CONTRACT_ID,
        integration_status="primary_runtime_executor",
        kinds=_PROFESSIONAL_KINDS,
        actions=_PROFESSIONAL_ACTIONS,
        chart_types=CHART_TYPES,
        output_formats=("video/mp4",),
        input_media_formats=None,
        input_format_policy="No repository-level extension/MIME allowlist. Local files, HTTP(S), and Pixabay results are resolved; actual decoding is delegated to Chromium and checked during preflight/capture.",
        media_sources=("local_path", "http_url", "pixabay_search"),
        max_group_depth=8,
        max_objects_per_shot=None,
        max_download_bytes=100 * 1024 * 1024,
        media_motion=("static", "slow_zoom", "zoom_in", "zoom_out", "pan_left", "pan_right"),
        notes=(
            "A shot is composed as one HTML/CSS/SVG scene; Playwright records browser video and FFmpeg normalizes MP4.",
            "Nested group media is recursively resolved; nesting beyond eight levels is rejected by the asset resolver.",
            "Events address top-level shot objects only. Nested group children are not valid event targets in the runtime Shot contract.",
            "Stable subtarget IDs for nodes, edges, rows, columns, chart series and group children are not supported by this runtime event API.",
        ),
    ),
    RendererCapability(
        renderer_id=PILLOW_RENDERER_ID,
        renderer_version="module-v2; runtime-schema-2.0; source-version-not-declared",
        display_name="Pillow SceneRenderer + AnimationEngine V2",
        runtime_contract=RUNTIME_CONTRACT_ID,
        integration_status="alternate_frame_executor",
        kinds=_PILLOW_KINDS,
        actions=_PILLOW_ACTIONS,
        chart_types=PILLOW_CHART_TYPES,
        output_formats=("image/png",),
        input_media_formats=None,
        input_format_policy="No repository-level extension allowlist. Pillow opens image files and FFmpeg extracts video frames. Direct HTTP URL downloads are unbounded in this resolver; Pixabay CDN downloads have a separate 100 MiB limit in pixabay_media.py.",
        media_sources=("local_path", "http_url", "pixabay_search"),
        # Registry guardrail; the Pillow drawing recursion has no equivalent
        # explicit limit, so the validator applies this conservative ceiling.
        max_group_depth=8,
        max_objects_per_shot=None,
        max_download_bytes=None,
        media_motion=("static",),
        notes=(
            "SceneRenderer emits raster frames; AnimationEngine schedules per-frame states and writes PNG files.",
            "The asset resolver prepares media in top-level objects and their direct group children only; deeper nested media is blocked by this registry.",
            "Group drawing recurses. The appear action is supported on a top-level group because visibility is checked before recursion; opacity, highlight, transform and draw state are not applied to group wrappers.",
            "Top-level tables require a reveal_row event targeting the final row; AnimationEngine otherwise initializes revealed_rows=0 and leaves all body rows hidden.",
            "Top-level line/arrow shapes, bar charts with more than one category, line charts with more than two points per series, and node graphs with edges require a draw event; AnimationEngine initializes draw_progress=0 and otherwise leaves these progressive elements incomplete.",
            "Pillow chart rendering supports only bar/line/pie/donut. Area is rendered as a line, so it is deliberately rejected. Bar, pie and donut render only the first series.",
            "Pillow line/arrow shapes use the object-box diagonal and ignore authored content.start/content.end endpoints.",
            "The registry applies an eight-level nesting guardrail; this is a registry policy, not a renderer-enforced Pillow limit.",
        ),
    ),
)

# These historical renderers remain discoverable in the inventory document but
# do not directly consume the VisualStoryPlan runtime contract.
LEGACY_EXECUTOR_INVENTORY = (
    {
        "executor_id": "diagram_renderer_legacy",
        "module": "diagram_renderer.py",
        "input_contract": "legacy scene dictionary",
        "output_formats": ("image/png",),
        "runtime_v2_compatible": False,
    },
    {
        "executor_id": "html_renderer_legacy",
        "module": "html_renderer.py",
        "input_contract": "legacy beat dictionary",
        "output_formats": ("video/mp4",),
        "runtime_v2_compatible": False,
    },
)


def list_renderer_capabilities() -> tuple[RendererCapability, ...]:
    return RENDERER_CAPABILITIES


def get_renderer_capability(renderer_id: str) -> RendererCapability:
    for item in RENDERER_CAPABILITIES:
        if item.renderer_id == renderer_id:
            return item
    known = ", ".join(item.renderer_id for item in RENDERER_CAPABILITIES)
    raise KeyError(f"Unknown renderer_id {renderer_id!r}; registered: {known}")


def validate_capability_request(
    renderer_id: str,
    *,
    kind: str,
    action: str | None = None,
    subtype_field: str | None = None,
    subtype: str | None = None,
    subtarget_id: str | None = None,
) -> tuple[CapabilityDiagnostic, ...]:
    """Check one kind/action request without rendering or substituting anything."""
    caps = get_renderer_capability(renderer_id)
    diagnostics: list[CapabilityDiagnostic] = []
    kind_caps = caps.kind(kind)
    if kind_caps is None:
        return (CapabilityDiagnostic("unsupported_kind", f"{renderer_id} does not support object kind {kind!r}.", renderer_id),)

    if subtype_field and subtype is not None:
        allowed = kind_caps.values_for(subtype_field)
        if allowed is None:
            diagnostics.append(CapabilityDiagnostic(
                "unknown_subtype_field",
                f"{renderer_id} has no registered subtype field {kind}.{subtype_field}.",
                renderer_id,
            ))
        elif subtype not in allowed:
            diagnostics.append(CapabilityDiagnostic(
                "unsupported_subtype",
                f"{renderer_id} does not support {kind}.{subtype_field}={subtype!r}; supported: {', '.join(allowed)}.",
                renderer_id,
            ))

    if action is not None:
        action_caps = caps.action(action)
        if action_caps is None:
            diagnostics.append(CapabilityDiagnostic(
                "unsupported_action", f"{renderer_id} does not implement action {action!r}.", renderer_id,
            ))
        elif kind not in action_caps.target_kinds:
            diagnostics.append(CapabilityDiagnostic(
                "incompatible_action_target",
                f"{renderer_id} action {action!r} cannot target object kind {kind!r}; supported targets: {', '.join(action_caps.target_kinds)}.",
                renderer_id,
            ))
        else:
            allowed_targets = action_caps.allowed_subtypes_for(kind)
            if subtype is not None and allowed_targets is not None and subtype not in allowed_targets:
                diagnostics.append(CapabilityDiagnostic(
                    "incompatible_action_subtype",
                    f"{renderer_id} action {action!r} does not support {kind} subtype {subtype!r}; supported: {', '.join(allowed_targets)}.",
                    renderer_id,
                ))
            if subtarget_id is not None and action_caps.subtarget_mode != "stable_id":
                diagnostics.append(CapabilityDiagnostic(
                    "unsupported_subtarget",
                    f"{renderer_id} action {action!r} does not support stable subtarget_id addressing.",
                    renderer_id,
                ))

    return tuple(diagnostics)


def _finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def validate_shot_capabilities(shot: Any, renderer_id: str = PROFESSIONAL_RENDERER_ID) -> CapabilityReport:
    """Validate a runtime ``Shot`` against a concrete executor capability record."""
    caps = get_renderer_capability(renderer_id)
    diagnostics: list[CapabilityDiagnostic] = []
    object_registry: dict[str, Any] = {}

    def add(code: str, message: str, target_id: str | None = None) -> None:
        diagnostics.append(CapabilityDiagnostic(code, message, renderer_id, target_id))

    def inspect_object(obj: Any, *, depth: int, media_depth: int) -> None:
        object_id = str(getattr(obj, "object_id", "(unknown)"))
        kind = str(getattr(obj, "kind", "(unknown)"))
        if object_id in object_registry:
            add("duplicate_nested_object_id", f"Object id {object_id!r} is duplicated in the nested composition.", object_id)
        else:
            object_registry[object_id] = obj

        if caps.kind(kind) is None:
            add("unsupported_kind", f"{renderer_id} does not support object kind {kind!r}.", object_id)
            return

        if depth > caps.max_group_depth:
            add("group_nesting_limit", f"Group nesting depth {depth} exceeds registry limit {caps.max_group_depth}.", object_id)
            return

        content = getattr(obj, "content", {}) or {}
        subtype_field = None
        subtype = None
        if kind == "shape":
            subtype_field, subtype = "shape_type", content.get("shape_type")
            if subtype == "polygon":
                points = content.get("points")
                if not isinstance(points, (list, tuple)) or len(points) < 3:
                    add("invalid_polygon_points", "Polygon requires at least three coordinate pairs.", object_id)
                elif len(points) > 64:
                    add("polygon_point_limit", "Polygon exceeds the Professional Renderer limit of 64 points.", object_id)
                else:
                    for point_index, point in enumerate(points):
                        if (
                            not isinstance(point, (list, tuple))
                            or len(point) != 2
                            or any(not _finite_number(value) or not 0.0 <= float(value) <= 1.0 for value in point)
                        ):
                            add("invalid_polygon_point", f"Polygon point {point_index} must be a finite normalized [x, y] pair.", object_id)
                            break
            elif subtype in {"line", "arrow"}:
                defaults = {"start": (0.04, 0.5), "end": (0.96, 0.5)}
                for endpoint, default in defaults.items():
                    point = content.get(endpoint, default)
                    if (
                        not isinstance(point, (list, tuple))
                        or len(point) != 2
                        or any(not _finite_number(value) or not 0.0 <= float(value) <= 1.0 for value in point)
                    ):
                        add("invalid_shape_endpoint", f"{endpoint} must be a finite normalized [x, y] pair.", object_id)
                        break
                if renderer_id == PILLOW_RENDERER_ID and ("start" in content or "end" in content):
                    add("unsupported_shape_endpoints", "Pillow line/arrow rendering ignores content.start/content.end and always uses the object-box diagonal.", object_id)
        elif kind == "icon":
            subtype_field, subtype = "icon_id", content.get("icon_id")
        elif kind == "chart":
            subtype_field, subtype = "chart_type", content.get("chart_type")
            series_items = content.get("series")
            if not isinstance(series_items, list) or not series_items:
                add("invalid_chart_series", "Chart requires a non-empty series list.", object_id)
            else:
                if subtype in {"pie", "donut"} and len(series_items) != 1:
                    add("unsupported_chart_series_count", f"{subtype} rendering consumes exactly one series; received {len(series_items)}.", object_id)
                if renderer_id == PILLOW_RENDERER_ID and subtype == "bar" and len(series_items) != 1:
                    add("unsupported_chart_series_count", f"Pillow bar rendering consumes only the first series; received {len(series_items)}.", object_id)
                for series_index, series_item in enumerate(series_items):
                    if not isinstance(series_item, dict):
                        add("invalid_chart_series", f"Chart series {series_index} must be an object.", object_id)
                        continue
                    values = series_item.get("values")
                    if not isinstance(values, list) or not values:
                        add("empty_chart_series", f"Chart series {series_index} requires a non-empty values list.", object_id)
                        continue
                    if any(not _finite_number(value) for value in values):
                        add("invalid_chart_value", f"Chart series {series_index} contains a non-finite or non-numeric value.", object_id)
                    if subtype in {"line", "area"} and len(values) < 2:
                        add("insufficient_chart_points", f"{subtype} chart series {series_index} requires at least two values.", object_id)
                if subtype in {"pie", "donut"} and isinstance(series_items[0], dict):
                    values = series_items[0].get("values")
                    if isinstance(values, list) and values and all(_finite_number(value) for value in values):
                        if sum(max(0.0, float(value)) for value in values) <= 0:
                            add("empty_pie_data", f"{subtype} chart requires a positive total in its first series.", object_id)
        elif kind == "node_graph":
            subtype_field, subtype = "layout", content.get("layout", "left_to_right")
            raw_nodes = content.get("nodes")
            node_ids: set[str] = set()
            if not isinstance(raw_nodes, list) or not raw_nodes:
                add("invalid_graph_nodes", "Node graph requires a non-empty nodes list.", object_id)
            else:
                for node_index, node in enumerate(raw_nodes):
                    if not isinstance(node, dict):
                        add("invalid_graph_node", f"Graph node at index {node_index} is not an object.", object_id)
                        continue
                    node_id = node.get("node_id")
                    if not isinstance(node_id, str) or not node_id.strip():
                        add("invalid_graph_node_id", f"Graph node at index {node_index} requires a non-empty string node_id.", object_id)
                        continue
                    if node_id in node_ids:
                        add("duplicate_graph_node_id", f"Graph node_id {node_id!r} is duplicated.", object_id)
                    node_ids.add(node_id)
            raw_edges = content.get("edges", [])
            if not isinstance(raw_edges, list):
                add("invalid_graph_edges", "Graph edges must be a list.", object_id)
            else:
                for edge_index, edge in enumerate(raw_edges):
                    if not isinstance(edge, dict):
                        add("invalid_graph_edge_reference", f"Graph edge at index {edge_index} is not an object.", object_id)
                        continue
                    start_id = edge.get("from")
                    end_id = edge.get("to")
                    if (
                        not isinstance(start_id, str)
                        or not isinstance(end_id, str)
                        or start_id not in node_ids
                        or end_id not in node_ids
                    ):
                        add(
                            "invalid_graph_edge_reference",
                            f"Graph edge at index {edge_index} references unknown or invalid node(s): from={start_id!r}, to={end_id!r}.",
                            object_id,
                        )
        elif kind == "table":
            columns = content.get("columns")
            rows = content.get("rows")
            if not isinstance(columns, list) or not columns or any(not isinstance(col, dict) for col in columns):
                add("invalid_table_columns", "Table columns must be a non-empty list of objects.", object_id)
            if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) or not isinstance(row.get("cells"), list) for row in rows):
                add("invalid_table_rows", "Table rows must be a non-empty list of objects with a cells list.", object_id)

        for issue in validate_capability_request(
            renderer_id, kind=kind, subtype_field=subtype_field, subtype=subtype,
        ):
            diagnostics.append(CapabilityDiagnostic(issue.code, issue.message, renderer_id, object_id))

        if kind in {"image", "video"}:
            camera_motion = content.get("camera_motion", "static")
            if camera_motion not in caps.media_motion:
                add("unsupported_media_motion", f"{renderer_id} does not support camera_motion={camera_motion!r}; supported: {', '.join(caps.media_motion)}.", object_id)
            if renderer_id == PILLOW_RENDERER_ID and media_depth > 1:
                add("nested_media_depth_unsupported", "Pillow asset preparation resolves media only in top-level objects and direct group children.", object_id)

        if kind == "group":
            children = content.get("children", [])
            if not isinstance(children, list) or not children:
                add("invalid_group_children", "Group requires a non-empty children list.", object_id)
                return
            from .visual_story_v2 import VisualObject
            for raw_child in children:
                try:
                    child = raw_child if isinstance(raw_child, VisualObject) else VisualObject.model_validate(raw_child)
                except Exception as exc:
                    add("invalid_nested_object", f"Could not validate nested group object: {exc}", object_id)
                    continue
                inspect_object(child, depth=depth + 1, media_depth=media_depth + 1)

    for obj in getattr(shot, "objects", ()):
        inspect_object(obj, depth=0, media_depth=0)

    root_objects = {str(obj.object_id): obj for obj in getattr(shot, "objects", ())}
    for event in getattr(shot, "events", ()):
        target_id = str(event.target_id)
        target = root_objects.get(target_id)
        if target is None:
            add("event_target_not_top_level", "Runtime events must target a top-level shot object; nested group children are not addressable.", target_id)
            continue

        kind = str(target.kind)
        subtype_field = None
        subtype = None
        if kind == "shape":
            subtype_field, subtype = "shape_type", target.content.get("shape_type")
        elif kind == "chart":
            subtype_field, subtype = "chart_type", target.content.get("chart_type")
        elif kind == "icon":
            subtype_field, subtype = "icon_id", target.content.get("icon_id")
        elif kind == "node_graph":
            subtype_field, subtype = "layout", target.content.get("layout", "left_to_right")

        issues = validate_capability_request(
            renderer_id,
            kind=kind,
            action=str(event.action),
            subtype_field=subtype_field,
            subtype=subtype,
        )
        for issue in issues:
            diagnostics.append(CapabilityDiagnostic(issue.code, issue.message, renderer_id, target_id))

        if event.action == "reveal_row":
            row_index = event.params.get("row_index")
            rows = target.content.get("rows", [])
            if (
                isinstance(row_index, bool)
                or not isinstance(row_index, int)
                or row_index < 0
                or not isinstance(rows, list)
                or row_index >= len(rows)
                or (renderer_id == PROFESSIONAL_RENDERER_ID and row_index > 1000)
            ):
                add("invalid_row_subtarget", f"reveal_row row_index must address an existing table row and stay within renderer limits (0..{min(1000, max(0, len(rows)-1)) if isinstance(rows, list) else 0}).", target_id)

        if (
            renderer_id == PILLOW_RENDERER_ID
            and event.action == "draw"
            and kind == "node_graph"
            and not (target.content.get("edges") or [])
        ):
            add(
                "empty_draw_target",
                "Pillow draw on node_graph requires at least one edge; graph nodes are not progressively revealed.",
                target_id,
            )

    if renderer_id == PILLOW_RENDERER_ID:
        events_by_target: dict[str, list[Any]] = {}
        for event in getattr(shot, "events", ()):
            events_by_target.setdefault(str(event.target_id), []).append(event)

        makes_visible = {"appear", "fade_in", "fade_out", "move", "scale", "draw", "reveal_row", "count_up", "update_text"}
        for obj in getattr(shot, "objects", ()):
            object_id = str(obj.object_id)
            target_events = events_by_target.get(object_id, [])
            becomes_visible = obj.initial_state == "visible" or any(event.action in makes_visible for event in target_events)
            if not becomes_visible:
                continue

            needs_draw = (
                (obj.kind == "shape" and obj.content.get("shape_type") in {"line", "arrow"})
                or (
                    obj.kind == "chart"
                    and (
                        (obj.content.get("chart_type") == "bar" and any(
                            len(item.get("values", [])) > 1
                            for item in (obj.content.get("series") or [])
                            if isinstance(item, dict) and isinstance(item.get("values"), list)
                        ))
                        or (obj.content.get("chart_type") == "line" and any(
                            len(item.get("values", [])) > 2
                            for item in (obj.content.get("series") or [])
                            if isinstance(item, dict) and isinstance(item.get("values"), list)
                        ))
                    )
                )
                or (obj.kind == "node_graph" and bool(obj.content.get("edges")))
            )
            if needs_draw and not any(event.action == "draw" for event in target_events):
                add("required_draw_event_missing", "Pillow initializes draw_progress=0 for top-level objects; this progressive graphic requires a draw event to reach its complete state.", object_id)

            if obj.kind == "table":
                rows = obj.content.get("rows", [])
                if isinstance(rows, list) and rows:
                    final_row = len(rows) - 1
                    reveals_final_row = any(
                        event.action == "reveal_row"
                        and event.params.get("row_index") == final_row
                        and not isinstance(event.params.get("row_index"), bool)
                        for event in target_events
                    )
                    if not reveals_final_row:
                        add("required_final_row_reveal_missing", "Pillow initializes revealed_rows=0 for top-level tables; a reveal_row event targeting the final row is required to display the complete table body.", object_id)

    # Keep diagnostics stable for reproducible dry-runs and snapshots.
    diagnostics.sort(key=lambda item: (item.target_id or "", item.code, item.message))
    return CapabilityReport(renderer_id, REGISTRY_VERSION, tuple(diagnostics))


def require_shot_capabilities(shot: Any, renderer_id: str = PROFESSIONAL_RENDERER_ID) -> CapabilityReport:
    report = validate_shot_capabilities(shot, renderer_id)
    if not report.supported:
        raise CapabilityValidationError(report)
    return report
