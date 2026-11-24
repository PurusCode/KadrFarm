"""Typed visual object payloads for the two-pass Visual Story contract 2.0.

The schema records editorial intent. It deliberately does not contain pixel
coordinates, exact geometry, frame times, arbitrary executable code, or a
renderer implementation choice.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union
import ast
import re

from pydantic import Field, model_validator

from .common import (
    ID_PATTERN,
    ObjectRole,
    ObjectState,
    SemanticAnchor,
    SemanticRegion,
    SemanticSize,
    StrictModel,
    ensure_unique,
)


class LayerSpec(StrictModel):
    z_index: int | None = Field(default=None, ge=-1000, le=1000)
    region: SemanticRegion = "auto"
    anchor: SemanticAnchor = "auto"
    size: SemanticSize = "auto"
    overlap_policy: Literal["opaque", "transparent", "scrim", "mask"] = "transparent"


class AppearanceSpec(StrictModel):
    tone: Literal["neutral", "accent", "positive", "warning", "negative"] = "neutral"
    emphasis: Literal["subtle", "normal", "strong", "hero"] = "normal"
    style_ref: str | None = Field(default=None, min_length=1, max_length=100)


class TextEmphasisSpan(StrictModel):
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    text_excerpt: str = Field(min_length=1, max_length=500)
    emphasis: Literal["bold", "accent", "underline", "highlight"] = "accent"

    @model_validator(mode="after")
    def validate_range(self):
        if self.char_end <= self.char_start or self.char_end - self.char_start != len(self.text_excerpt):
            raise ValueError("text emphasis range must match text_excerpt length")
        return self


class ImageContent(StrictModel):
    subject: str = Field(min_length=3, max_length=500)
    scene: str | None = Field(default=None, max_length=500)
    action: str | None = Field(default=None, max_length=500)
    shot_type: str | None = Field(default=None, max_length=80)
    camera_angle: str | None = Field(default=None, max_length=80)
    mood: str | None = Field(default=None, max_length=100)
    search_queries: list[str] = Field(min_length=1, max_length=8)
    orientation: Literal["landscape", "portrait", "square", "any"] = "landscape"
    crop_intent: str = Field(min_length=3, max_length=300)
    visual_constraints: list[str] = Field(default_factory=list, max_length=20)


class VideoContent(ImageContent):
    desired_action: str = Field(min_length=3, max_length=500)
    motion_intensity: Literal["low", "medium", "high"] = "medium"
    desired_duration: Literal["brief", "medium", "long", "flexible"] = "medium"
    loop_allowed: bool = False


class TextContent(StrictModel):
    text: str = Field(min_length=1, max_length=5000)
    semantic_role: Literal[
        "headline", "label", "caption", "subtitle", "quote", "source_note",
        "number_label", "section_title", "annotation",
    ] = "label"
    emphasis_spans: list[TextEmphasisSpan] = Field(default_factory=list, max_length=30)
    max_lines: int | None = Field(default=None, ge=1, le=20)
    alignment: Literal["left", "center", "right", "auto"] = "auto"
    hierarchy: Literal["primary", "secondary", "tertiary", "micro"] = "secondary"
    text_source_ref: str | None = Field(default=None, min_length=1, max_length=200)
    source_refs: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_emphasis_spans(self):
        ensure_unique(self.source_refs, "text source reference")
        if re.search(r"\d", self.text) and not (self.text_source_ref or self.source_refs):
            raise ValueError("numeric text requires a trusted text_source_ref or source_refs entry")
        ordered = sorted(self.emphasis_spans, key=lambda item: item.char_start)
        previous_end = -1
        for span in ordered:
            if span.char_end > len(self.text):
                raise ValueError("text emphasis span is outside content.text")
            if self.text[span.char_start:span.char_end] != span.text_excerpt:
                raise ValueError("text emphasis excerpt does not match content.text")
            if span.char_start < previous_end:
                raise ValueError("text emphasis spans must not overlap")
            previous_end = span.char_end
        return self


class ShapeContent(StrictModel):
    shape_type: Literal[
        "rectangle", "rounded_rectangle", "circle", "ellipse", "line", "arrow",
        "double_arrow", "polygon", "arc", "bracket", "path",
    ]
    direction: Literal["left", "right", "up", "down", "bidirectional", "none"] = "none"
    fill_role: Literal["none", "surface", "surface_alt", "accent", "positive", "warning", "negative"] = "none"
    stroke_role: Literal["none", "subtle", "primary", "accent", "positive", "warning", "negative"] = "primary"
    stroke_weight: Literal["thin", "normal", "thick", "heavy"] = "normal"
    start_object_id: str | None = Field(default=None, pattern=ID_PATTERN)
    end_object_id: str | None = Field(default=None, pattern=ID_PATTERN)

    @model_validator(mode="after")
    def validate_connectors(self):
        if self.shape_type in {"line", "arrow", "double_arrow"}:
            if (self.start_object_id is None) != (self.end_object_id is None):
                raise ValueError("connector must provide both start_object_id and end_object_id")
        elif self.start_object_id is not None or self.end_object_id is not None:
            raise ValueError("only line/arrow shapes may reference connected objects")
        return self


class IconContent(StrictModel):
    icon_id: str = Field(pattern=ID_PATTERN)
    semantic_label: str = Field(min_length=2, max_length=200)
    color_role: Literal["neutral", "accent", "positive", "warning", "negative"] = "neutral"
    stroke_style: Literal["solid", "outline", "filled", "duotone"] = "outline"


class CalloutContent(StrictModel):
    text: str = Field(min_length=1, max_length=1000)
    tone: Literal["neutral", "warning", "positive", "accent"] = "neutral"
    shape: Literal["box", "pill", "bracket", "speech_bubble"] = "box"
    pointer_target_object_id: str | None = Field(default=None, pattern=ID_PATTERN)
    icon_ref: str | None = Field(default=None, pattern=ID_PATTERN)
    source_refs: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_source_refs(self):
        ensure_unique(self.source_refs, "callout source reference")
        if re.search(r"\d", self.text) and not self.source_refs:
            raise ValueError("numeric callout text requires trusted source_refs")
        return self


class GroupContent(StrictModel):
    child_object_ids: list[str] = Field(min_length=1, max_length=100)
    local_layout: Literal[
        "horizontal", "vertical", "radial", "grid", "layered", "freeform_intent",
    ] = "freeform_intent"
    clip_policy: Literal["none", "clip_children", "mask_children"] = "none"
    transform_origin: SemanticAnchor = "center"

    @model_validator(mode="after")
    def validate_children(self):
        ensure_unique(self.child_object_ids, "group child object id")
        return self


class GraphNode(StrictModel):
    node_id: str = Field(pattern=ID_PATTERN)
    label: str = Field(min_length=1, max_length=200)
    node_type: Literal["default", "source", "process", "decision", "outcome", "warning"] = "default"
    source_ref: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_numeric_label_source(self):
        if re.search(r"\d", self.label) and self.source_ref is None:
            raise ValueError("numeric graph node label requires source_ref")
        return self


class GraphEdge(StrictModel):
    edge_id: str = Field(pattern=ID_PATTERN)
    from_node_id: str = Field(pattern=ID_PATTERN)
    to_node_id: str = Field(pattern=ID_PATTERN)
    label: str | None = Field(default=None, max_length=200)
    source_ref: str | None = Field(default=None, min_length=1, max_length=200)
    relation: Literal["flows_to", "depends_on", "produces", "contains", "contrasts_with", "connects"] = "connects"

    @model_validator(mode="after")
    def validate_numeric_label_source(self):
        if self.label and re.search(r"\d", self.label) and self.source_ref is None:
            raise ValueError("numeric graph edge label requires source_ref")
        return self


class NodeGraphContent(StrictModel):
    layout: Literal["left_to_right", "top_to_bottom", "radial", "hub_and_spoke", "layered", "auto"] = "auto"
    nodes: list[GraphNode] = Field(min_length=1, max_length=100)
    edges: list[GraphEdge] = Field(default_factory=list, max_length=200)
    legend: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_graph(self):
        node_ids = [node.node_id for node in self.nodes]
        edge_ids = [edge.edge_id for edge in self.edges]
        ensure_unique(node_ids, "node_id")
        ensure_unique(edge_ids, "edge_id")
        registry = set(node_ids)
        for edge in self.edges:
            if edge.from_node_id not in registry or edge.to_node_id not in registry:
                raise ValueError(f"{edge.edge_id}: graph edge references an unknown node")
        return self


class ChartSeries(StrictModel):
    series_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    value_refs: list[str] = Field(default_factory=list, max_length=100)
    illustrative_values: list[float] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_sources(self):
        if self.value_refs and self.illustrative_values:
            raise ValueError("chart series cannot mix approved value_refs and illustrative_values")
        ensure_unique(self.value_refs, "chart series value reference")
        return self


class ChartContent(StrictModel):
    chart_type: Literal["bar", "line", "pie", "donut", "area"]
    data_mode: Literal["provided", "derived", "illustrative"]
    title: str | None = Field(default=None, max_length=300)
    categories: list[str] = Field(default_factory=list, max_length=100)
    series: list[ChartSeries] = Field(min_length=1, max_length=20)
    unit: str | None = Field(default=None, max_length=80)
    show_values: bool = False
    show_legend: bool = True
    animation_mode: Literal["none", "reveal", "plot_line", "grow_bars", "auto"] = "auto"
    illustrative_label: str | None = Field(default=None, min_length=3, max_length=100)

    @model_validator(mode="after")
    def validate_series_data(self):
        ensure_unique([series.series_id for series in self.series], "chart series_id")
        if self.data_mode == "illustrative":
            if not self.illustrative_label:
                raise ValueError("illustrative chart requires an explicit illustrative_label")
            if any(not item.illustrative_values or item.value_refs for item in self.series):
                raise ValueError("illustrative chart series must use illustrative_values only")
        else:
            if self.illustrative_label is not None:
                raise ValueError("approved/derived chart must not have illustrative_label")
            if any(not item.value_refs or item.illustrative_values for item in self.series):
                raise ValueError("provided/derived chart series must reference approved values")
        if self.categories:
            for item in self.series:
                value_count = len(item.illustrative_values) if item.illustrative_values else len(item.value_refs)
                if value_count != len(self.categories):
                    raise ValueError("chart series length must match categories length")
        return self


class TableColumn(StrictModel):
    column_id: str = Field(pattern=ID_PATTERN)
    title: str = Field(min_length=1, max_length=200)


class TableCell(StrictModel):
    text: str | None = Field(default=None, max_length=1000)
    value_ref: str | None = Field(default=None, min_length=1, max_length=200)
    illustrative_value: str | None = Field(default=None, max_length=1000)
    source_ref: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_value_choice(self):
        selected = sum(value is not None for value in (self.text, self.value_ref, self.illustrative_value))
        if selected != 1:
            raise ValueError("table cell must contain exactly one of text, value_ref, illustrative_value")
        if self.source_ref is not None and self.text is None:
            raise ValueError("table cell source_ref is only valid for literal text cells")
        if self.text is not None and re.search(r"\d", self.text) and self.source_ref is None:
            raise ValueError("numeric table text requires source_ref; use value_ref for data values")
        return self


class TableRow(StrictModel):
    row_id: str = Field(pattern=ID_PATTERN)
    cells: list[TableCell] = Field(min_length=1, max_length=30)


class TableContent(StrictModel):
    data_mode: Literal["provided", "derived", "illustrative"] = "provided"
    columns: list[TableColumn] = Field(min_length=1, max_length=30)
    rows: list[TableRow] = Field(min_length=1, max_length=100)
    header_visible: bool = True
    reveal_mode: Literal["all", "row_by_row", "column_by_column"] = "all"
    highlighted_row_ids: list[str] = Field(default_factory=list, max_length=100)
    illustrative_label: str | None = Field(default=None, min_length=3, max_length=100)

    @model_validator(mode="after")
    def validate_table(self):
        column_ids = [column.column_id for column in self.columns]
        row_ids = [row.row_id for row in self.rows]
        ensure_unique(column_ids, "table column_id")
        ensure_unique(row_ids, "table row_id")
        if any(len(row.cells) != len(self.columns) for row in self.rows):
            raise ValueError("every table row must contain one cell per column")
        if not set(self.highlighted_row_ids) <= set(row_ids):
            raise ValueError("highlighted_row_ids references an unknown row")
        if self.data_mode == "illustrative":
            if not self.illustrative_label:
                raise ValueError("illustrative table requires illustrative_label")
            if any(cell.value_ref is not None for row in self.rows for cell in row.cells):
                raise ValueError("illustrative table cannot use approved value_ref cells")
        else:
            if self.illustrative_label is not None:
                raise ValueError("provided/derived table must not have illustrative_label")
            if any(cell.illustrative_value is not None for row in self.rows for cell in row.cells):
                raise ValueError("provided/derived table cannot use illustrative_value cells")
        return self


class FormulaContent(StrictModel):
    expression: str = Field(min_length=1, max_length=1000)
    value_refs: list[str] = Field(min_length=1, max_length=30)
    data_mode: Literal["provided", "derived"] = "derived"
    unit: str | None = Field(default=None, max_length=80)
    explanation: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_refs(self):
        ensure_unique(self.value_refs, "formula value reference")
        try:
            tree = ast.parse(self.expression, mode="eval")
        except SyntaxError as exc:
            raise ValueError("formula expression is not valid arithmetic syntax") from exc
        allowed_nodes = (
            ast.Expression, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult,
            ast.Div, ast.Pow, ast.Mod, ast.UAdd, ast.USub, ast.Name, ast.Load,
            ast.Constant, ast.FloorDiv,
        )
        names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, allowed_nodes):
                raise ValueError(f"formula expression contains unsupported syntax: {type(node).__name__}")
            if isinstance(node, ast.Constant):
                raise ValueError("formula numeric constants must be declared as trusted value_refs")
            if isinstance(node, ast.Name):
                names.add(node.id)
        if names != set(self.value_refs):
            raise ValueError("formula identifiers must exactly match value_refs")
        return self


class CounterContent(StrictModel):
    data_mode: Literal["provided", "derived", "illustrative"] = "provided"
    value_ref: str | None = Field(default=None, min_length=1, max_length=200)
    illustrative_value: float | None = None
    prefix: str = Field(default="", max_length=40)
    suffix: str = Field(default="", max_length=40)
    decimals: int = Field(default=0, ge=0, le=6)
    illustrative_label: str | None = Field(default=None, min_length=3, max_length=100)

    @model_validator(mode="after")
    def validate_value_source(self):
        if self.data_mode == "illustrative":
            if self.value_ref is not None or self.illustrative_value is None or not self.illustrative_label:
                raise ValueError("illustrative counter requires illustrative_value and illustrative_label only")
        else:
            if self.value_ref is None or self.illustrative_value is not None or self.illustrative_label is not None:
                raise ValueError("provided/derived counter requires value_ref and forbids illustrative values")
        return self


class VisualObjectBase(StrictModel):
    object_id: str = Field(pattern=ID_PATTERN)
    continuity_id: str | None = Field(default=None, pattern=ID_PATTERN)
    role: ObjectRole = "supporting"
    layer: LayerSpec = Field(default_factory=LayerSpec)
    appearance: AppearanceSpec = Field(default_factory=AppearanceSpec)
    initial_state: ObjectState = "visible"
    entry_state: ObjectState | None = None
    exit_state: ObjectState | None = None
    tags: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_continuity_states(self):
        if self.continuity_id is None and (self.entry_state is not None or self.exit_state is not None):
            raise ValueError("entry_state/exit_state require continuity_id")
        ensure_unique(self.tags, "object tag")
        return self


class ImageObject(VisualObjectBase):
    kind: Literal["image"] = "image"
    content: ImageContent


class VideoObject(VisualObjectBase):
    kind: Literal["video"] = "video"
    content: VideoContent


class TextObject(VisualObjectBase):
    kind: Literal["text"] = "text"
    content: TextContent


class ShapeObject(VisualObjectBase):
    kind: Literal["shape"] = "shape"
    content: ShapeContent


class IconObject(VisualObjectBase):
    kind: Literal["icon"] = "icon"
    content: IconContent


class CalloutObject(VisualObjectBase):
    kind: Literal["callout"] = "callout"
    content: CalloutContent


class GroupObject(VisualObjectBase):
    kind: Literal["group"] = "group"
    content: GroupContent


class NodeGraphObject(VisualObjectBase):
    kind: Literal["node_graph"] = "node_graph"
    content: NodeGraphContent


class ChartObject(VisualObjectBase):
    kind: Literal["chart"] = "chart"
    content: ChartContent


class TableObject(VisualObjectBase):
    kind: Literal["table"] = "table"
    content: TableContent


class FormulaObject(VisualObjectBase):
    kind: Literal["formula"] = "formula"
    content: FormulaContent


class CounterObject(VisualObjectBase):
    kind: Literal["counter"] = "counter"
    content: CounterContent


VisualObject = Annotated[
    Union[
        ImageObject, VideoObject, TextObject, ShapeObject, IconObject,
        CalloutObject, GroupObject, NodeGraphObject, ChartObject,
        TableObject, FormulaObject, CounterObject,
    ],
    Field(discriminator="kind"),
]


class SpatialRelationship(StrictModel):
    from_object_id: str = Field(pattern=ID_PATTERN)
    to_object_id: str = Field(pattern=ID_PATTERN)
    relation: Literal[
        "left_of", "right_of", "above", "below", "inside", "around",
        "connected_to", "aligned_with", "overlaps", "contrasts_with",
    ]
    reason: str = Field(min_length=3, max_length=300)

    @model_validator(mode="after")
    def validate_distinct_objects(self):
        if self.from_object_id == self.to_object_id:
            raise ValueError("spatial relationship must reference two different objects")
        return self


class CompositionPlan(StrictModel):
    layout_intent: str = Field(min_length=5, max_length=1500)
    objects: list[VisualObject] = Field(min_length=1, max_length=200)
    spatial_relationships: list[SpatialRelationship] = Field(default_factory=list, max_length=300)

    @model_validator(mode="after")
    def validate_object_references(self):
        object_ids = [obj.object_id for obj in self.objects]
        ensure_unique(object_ids, "object_id in composition")
        registry = {obj.object_id: obj for obj in self.objects}

        for relationship in self.spatial_relationships:
            if relationship.from_object_id not in registry or relationship.to_object_id not in registry:
                raise ValueError("spatial relationship references an unknown object")

        for obj in self.objects:
            if isinstance(obj, GroupObject):
                for child_id in obj.content.child_object_ids:
                    if child_id not in registry:
                        raise ValueError(f"{obj.object_id}: group references unknown child {child_id}")
                    if child_id == obj.object_id:
                        raise ValueError(f"{obj.object_id}: group cannot contain itself")
            elif isinstance(obj, CalloutObject) and obj.content.pointer_target_object_id:
                if obj.content.pointer_target_object_id not in registry:
                    raise ValueError(f"{obj.object_id}: callout pointer target does not exist")
            elif isinstance(obj, ShapeObject):
                refs = [obj.content.start_object_id, obj.content.end_object_id]
                for ref in (item for item in refs if item is not None):
                    if ref not in registry:
                        raise ValueError(f"{obj.object_id}: connector references unknown object {ref}")

        # Detect group cycles before handing the composition to any renderer.
        group_children = {
            obj.object_id: obj.content.child_object_ids
            for obj in self.objects if isinstance(obj, GroupObject)
        }
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(group_id: str) -> None:
            if group_id in visiting:
                raise ValueError(f"group containment cycle includes {group_id}")
            if group_id in visited:
                return
            visiting.add(group_id)
            for child_id in group_children.get(group_id, []):
                if child_id in group_children:
                    visit(child_id)
            visiting.remove(group_id)
            visited.add(group_id)

        for group_id in group_children:
            visit(group_id)

        return self
