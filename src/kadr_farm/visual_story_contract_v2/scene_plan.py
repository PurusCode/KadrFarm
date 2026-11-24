"""Per-scene composition plan contract (Scene Director output)."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import ID_PATTERN, SourceRef, StrictModel, ensure_unique
from .events import SpeechAnchor, VisualEvent
from .objects import (
    CalloutObject,
    ChartObject,
    CompositionPlan,
    CounterObject,
    FormulaObject,
    GroupObject,
    ImageObject,
    NodeGraphObject,
    ShapeObject,
    TableObject,
    TextObject,
    VideoObject,
    VisualObject,
)


class ContinuityLink(StrictModel):
    relation: Literal[
        "object", "composition", "action", "space", "visual_metaphor",
        "contrast", "callback", "resolution", "transition",
    ]
    reference: SourceRef
    reason: str = Field(min_length=3, max_length=600)
    continuity_id: str | None = Field(default=None, pattern=ID_PATTERN)
    entry_state: Literal[
        "hidden", "visible", "emphasized", "dimmed", "collapsed", "expanded",
        "transformed", "removed",
    ] | None = None
    exit_state: Literal[
        "hidden", "visible", "emphasized", "dimmed", "collapsed", "expanded",
        "transformed", "removed",
    ] | None = None

    @model_validator(mode="after")
    def validate_object_relation(self):
        if self.relation == "object" and (self.continuity_id is None or self.reference.object_id is None):
            raise ValueError("object continuity requires continuity_id and reference.object_id")
        if (self.entry_state is not None or self.exit_state is not None) and self.continuity_id is None:
            raise ValueError("continuity states require continuity_id")
        return self


class ShotContinuity(StrictModel):
    incoming: list[ContinuityLink] = Field(default_factory=list, max_length=50)
    outgoing: list[ContinuityLink] = Field(default_factory=list, max_length=50)


class FallbackPolicy(StrictModel):
    asset_miss: Literal["retry_search", "alternative_asset", "revise_director_plan", "manual_review"] = "revise_director_plan"
    unsupported_renderer_action: Literal["block_execution"] = "block_execution"
    approved_visual_substitution: bool = False

    @model_validator(mode="after")
    def validate_substitution(self):
        if self.approved_visual_substitution and self.asset_miss not in {"alternative_asset", "revise_director_plan"}:
            raise ValueError("visual substitution requires an explicit asset/plan revision policy")
        return self


class ShotPlan(StrictModel):
    shot_id: str = Field(pattern=ID_PATTERN)
    editorial_purpose: str = Field(min_length=5, max_length=1000)
    visual_summary: str = Field(min_length=10, max_length=2000)
    speech_anchors: list[SpeechAnchor] = Field(min_length=1, max_length=100)
    composition: CompositionPlan
    events: list[VisualEvent] = Field(default_factory=list, max_length=200)
    continuity: ShotContinuity = Field(default_factory=ShotContinuity)
    fallback_policy: FallbackPolicy = Field(default_factory=FallbackPolicy)

    @model_validator(mode="after")
    def validate_shot_references(self):
        anchors = [anchor.anchor_id for anchor in self.speech_anchors]
        ensure_unique(anchors, "speech anchor id in shot")
        events = [event.event_id for event in self.events]
        ensure_unique(events, "event_id in shot")
        anchor_registry = set(anchors)
        object_registry = {obj.object_id: obj for obj in self.composition.objects}
        for event in self.events:
            if event.target.object_id not in object_registry:
                raise ValueError(f"{self.shot_id}/{event.event_id}: event target object does not exist")
            if event.trigger.anchor_id not in anchor_registry:
                raise ValueError(f"{self.shot_id}/{event.event_id}: trigger references unknown speech anchor")
            if event.timing.hold_until_anchor_id and event.timing.hold_until_anchor_id not in anchor_registry:
                raise ValueError(f"{self.shot_id}/{event.event_id}: hold_until references unknown anchor")
            target = object_registry[event.target.object_id]
            self._validate_event_compatibility(event, target, object_registry)
            self._validate_subtarget(event, target)
        return self

    @staticmethod
    def _validate_subtarget(event: VisualEvent, target: VisualObject) -> None:
        subtarget = event.target.subtarget_id
        if subtarget is None:
            return
        if isinstance(target, NodeGraphObject):
            valid = {
                *(node.node_id for node in target.content.nodes),
                *(edge.edge_id for edge in target.content.edges),
            }
        elif isinstance(target, TableObject):
            valid = {
                *(row.row_id for row in target.content.rows),
                *(column.column_id for column in target.content.columns),
            }
        elif isinstance(target, ChartObject):
            valid = {series.series_id for series in target.content.series}
        elif isinstance(target, GroupObject):
            valid = set(target.content.child_object_ids)
        else:
            raise ValueError(f"{target.kind} does not support subtarget addressing")
        if subtarget not in valid:
            raise ValueError(f"{event.event_id}: unknown {target.kind} subtarget {subtarget!r}")

    @staticmethod
    def _validate_event_compatibility(event: VisualEvent, target: VisualObject, object_registry: dict[str, VisualObject]) -> None:
        action = event.action
        subtarget = event.target.subtarget_id
        if action in {"pan", "zoom", "ken_burns", "play", "freeze_frame", "crossfade"} and not isinstance(target, (ImageObject, VideoObject)):
            raise ValueError(f"{action} requires an image or video target")
        if action == "reveal_node":
            if not isinstance(target, NodeGraphObject) or subtarget not in {n.node_id for n in target.content.nodes}:
                raise ValueError("reveal_node requires a declared node_graph node subtarget")
        if action == "draw_edge":
            if not isinstance(target, NodeGraphObject) or subtarget not in {e.edge_id for e in target.content.edges}:
                raise ValueError("draw_edge requires a declared node_graph edge subtarget")
        if action in {"reveal_row", "reveal_column"}:
            if not isinstance(target, TableObject):
                raise ValueError(f"{action} requires a table target")
            valid_ids = {row.row_id for row in target.content.rows} if action == "reveal_row" else {column.column_id for column in target.content.columns}
            if subtarget not in valid_ids:
                raise ValueError(f"{action} requires a declared table subtarget")
        if action == "grow_bar":
            if not isinstance(target, ChartObject) or target.content.chart_type != "bar" or subtarget not in {s.series_id for s in target.content.series}:
                raise ValueError("grow_bar requires a bar chart series subtarget")
        if action == "plot_line":
            if not isinstance(target, ChartObject) or target.content.chart_type not in {"line", "area"}:
                raise ValueError("plot_line requires a line/area chart target")
        if action == "count_up" and not isinstance(target, CounterObject):
            raise ValueError("count_up requires a counter target")
        if action == "update_text" and not isinstance(target, TextObject):
            raise ValueError("update_text requires a text target")
        if action in {"update_value", "fill_progress"} and not isinstance(target, (CounterObject, ChartObject, TableObject, FormulaObject)):
            raise ValueError(f"{action} is incompatible with target object kind {target.kind}")
        if action == "draw_path" and not isinstance(target, ShapeObject):
            raise ValueError("draw_path requires a shape target")
        if action == "swap_content":
            replacement_id = event.replacement_object_id
            if replacement_id not in object_registry or replacement_id == target.object_id:
                raise ValueError("swap_content requires another declared replacement object")
        if action in {"transform", "morph"}:
            if event.target_object_id not in object_registry:
                raise ValueError(f"{action} references an unknown target object")
            if event.target_object_id == target.object_id:
                raise ValueError(f"{action} target_object_id must differ from the source object")


class SceneCompositionPlan(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    scene_id: str = Field(pattern=ID_PATTERN)
    source_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scene_purpose: str = Field(min_length=5, max_length=1000)
    shots: list[ShotPlan] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_scene_plan(self):
        shot_ids = [shot.shot_id for shot in self.shots]
        ensure_unique(shot_ids, "shot_id in scene plan")
        anchors = [anchor.anchor_id for shot in self.shots for anchor in shot.speech_anchors]
        ensure_unique(anchors, "speech anchor id in scene plan")
        events = [event.event_id for shot in self.shots for event in shot.events]
        ensure_unique(events, "event_id in scene plan")
        for shot in self.shots:
            for anchor in shot.speech_anchors:
                if anchor.source_scene_id != self.scene_id:
                    raise ValueError(f"{shot.shot_id}: speech anchor source_scene_id must match SceneCompositionPlan")
            for link in [*shot.continuity.incoming, *shot.continuity.outgoing]:
                if link.reference.scene_id == self.scene_id and link.reference.shot_id == shot.shot_id:
                    raise ValueError("shot continuity cannot reference itself")
        return self
