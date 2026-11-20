"""Typed executable contract for KadrFarm Visual Story V2.

Additive implementation. Does not replace visual_story.py.
Wall-clock timing is derived exclusively from measured WAV files.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ObjectKind = Literal[
    "image",
    "video",
    "text",
    "shape",
    "icon",
    "callout",
    "group",
    "node_graph",
    "chart",
    "table",
    "formula",
    "counter",
]

Zone = Literal[
    "background",
    "full_frame",
    "left",
    "center",
    "right",
    "top",
    "bottom",
    "foreground",
    "picture_in_picture",
]

ActionKind = Literal[
    "appear",
    "fade_in",
    "fade_out",
    "highlight",
    "move",
    "scale",
    "draw",
    "reveal_row",
    "count_up",
    "update_text",
]

Relation = Literal[
    "introduces",
    "extends",
    "contrasts",
    "resolves",
    "callbacks",
]

_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{0,99}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class StrictModel(BaseModel):
    """Base model: unknown fields are errors."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )


class SpeechUnit(StrictModel):
    """One separately measured speech unit.

    A unit must correspond to a separate WAV file.
    Do not estimate its duration from text length.
    """

    unit_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=5000)

    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)

    # Filled by the TTS stage.
    wav_path: str | None = None
    sha256: str | None = None

    @model_validator(mode="after")
    def validate_character_span(self):
        if self.char_end <= self.char_start:
            raise ValueError("char_end must exceed char_start")

        if self.char_end - self.char_start != len(self.text):
            raise ValueError(
                f"{self.unit_id}: character range does not match text length"
            )

        if self.sha256 is not None:
            if not _SHA256_PATTERN.fullmatch(self.sha256):
                raise ValueError("sha256 must contain exactly 64 hexadecimal characters")

        return self


class SpeechAnchor(StrictModel):
    """Position inside one measured speech unit."""

    unit_id: str
    offset_sec: float = Field(default=0.0, ge=0.0)


class Layer(StrictModel):
    """Normalized geometry. No pixel coordinates are authored by the LLM."""

    z_index: int = 0

    region: Zone = "center"

    # Object center in normalized canvas coordinates.
    x: float = Field(default=0.5, ge=0.0, le=1.0)
    y: float = Field(default=0.5, ge=0.0, le=1.0)

    # Object dimensions relative to the canvas.
    width: float = Field(default=0.8, gt=0.0, le=1.0)
    height: float = Field(default=0.5, gt=0.0, le=1.0)

    opacity: float = Field(default=1.0, ge=0.0, le=1.0)

    fit: Literal["contain", "cover", "stretch"] = "contain"


class VisualObject(StrictModel):
    """One independently addressable visual object."""

    object_id: str = Field(min_length=1, max_length=100)

    continuity_id: str | None = None

    kind: ObjectKind

    role: str = "supporting"

    layer: Layer = Field(default_factory=Layer)

    # Type-specific payload.
    content: dict[str, Any] = Field(default_factory=dict)

    appearance: dict[str, Any] = Field(default_factory=dict)

    initial_state: Literal["visible", "hidden"] = "visible"

    @model_validator(mode="after")
    def validate_content(self):
        c = self.content

        if self.kind in ("image", "video"):
            if not (
                c.get("path")
                or c.get("url")
                or c.get("search_queries")
            ):
                raise ValueError(
                    f"{self.kind} requires path, url or search_queries"
                )

        elif self.kind == "text":
            if not isinstance(c.get("text"), str):
                raise ValueError("text object requires content.text")

        elif self.kind == "shape":
            allowed = {
                "rectangle",
                "rounded_rectangle",
                "circle",
                "ellipse",
                "line",
                "arrow",
                "polygon",
            }

            if c.get("shape_type") not in allowed:
                raise ValueError(
                    f"Unsupported shape_type: {c.get('shape_type')!r}"
                )

        elif self.kind == "icon":
            if not c.get("icon_id"):
                raise ValueError("icon requires icon_id")

        elif self.kind == "chart":
            if c.get("chart_type") not in {
                "bar",
                "line",
                "pie",
                "donut",
                "area",
            }:
                raise ValueError("Unsupported chart_type")

            if c.get("data_mode") not in {
                "provided",
                "derived",
                "illustrative",
            }:
                raise ValueError("chart.data_mode must be explicit")

            if not isinstance(c.get("series"), list) or not c["series"]:
                raise ValueError("chart requires at least one series")

        elif self.kind == "table":
            if not c.get("columns") or not c.get("rows"):
                raise ValueError("table requires columns and rows")

        elif self.kind == "node_graph":
            if not c.get("nodes"):
                raise ValueError("node_graph requires nodes")

            if not isinstance(c.get("edges", []), list):
                raise ValueError("node_graph edges must be a list")

        elif self.kind == "formula":
            if not c.get("expression"):
                raise ValueError("formula requires expression")

        elif self.kind == "counter":
            if "value" not in c:
                raise ValueError("counter requires value")

        elif self.kind == "group":
            if not isinstance(c.get("children"), list) or not c["children"]:
                raise ValueError("group requires children")

        return self


class VisualEvent(StrictModel):
    """A semantic animation event anchored to measured speech."""

    event_id: str

    target_id: str

    action: ActionKind

    anchor: SpeechAnchor

    duration_sec: float = Field(default=0.5, ge=0.0)

    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_parameters(self):
        p = self.params

        if self.action == "highlight":
            color = p.get("color", "#F4B860")
            if not isinstance(color, str) or not re.fullmatch(
                r"#[0-9a-fA-F]{6}", color
            ):
                raise ValueError("highlight color must be #RRGGBB")

        elif self.action == "move":
            for key in ("x", "y"):
                if key in p and not 0.0 <= float(p[key]) <= 1.0:
                    raise ValueError(f"move.{key} must be between 0 and 1")

        elif self.action == "scale":
            factor = float(p.get("factor", 1.0))
            if not 0.1 <= factor <= 4.0:
                raise ValueError("scale.factor must be between 0.1 and 4.0")

        elif self.action == "reveal_row":
            if int(p.get("row_index", -1)) < 0:
                raise ValueError("reveal_row requires non-negative row_index")

        elif self.action == "count_up":
            if "from" not in p or "to" not in p:
                raise ValueError("count_up requires from and to")

            float(p["from"])
            float(p["to"])

        elif self.action == "update_text":
            if not isinstance(p.get("text"), str):
                raise ValueError("update_text requires a text parameter")

        return self


class Shot(StrictModel):
    """One continuous visual composition."""

    shot_id: str

    editorial_purpose: str = Field(min_length=3, max_length=500)

    visual_summary: str = Field(min_length=5, max_length=1500)

    speech_unit_ids: list[str] = Field(min_length=1)

    objects: list[VisualObject] = Field(min_length=1)

    events: list[VisualEvent] = Field(default_factory=list)

    relation_to_previous: Relation = "introduces"

    fallback_policy: list[str] = Field(
        default_factory=lambda: [
            "use local graphic fallback",
            "report missing stock asset",
        ]
    )

    @model_validator(mode="after")
    def validate_shot(self):
        object_ids = [obj.object_id for obj in self.objects]

        if len(object_ids) != len(set(object_ids)):
            raise ValueError(f"{self.shot_id}: duplicate object_id")

        object_registry = {
            obj.object_id: obj
            for obj in self.objects
        }

        action_ids = [event.event_id for event in self.events]

        if len(action_ids) != len(set(action_ids)):
            raise ValueError(f"{self.shot_id}: duplicate event_id")

        for event in self.events:
            if event.target_id not in object_registry:
                raise ValueError(
                    f"{self.shot_id}: event target does not exist: "
                    f"{event.target_id}"
                )

            target = object_registry[event.target_id]

            if event.action == "reveal_row" and target.kind != "table":
                raise ValueError("reveal_row can only target a table")

            if event.action == "count_up" and target.kind != "counter":
                raise ValueError("count_up can only target a counter")

        return self


class VisualStoryPlan(StrictModel):
    """Complete executable v2 visual plan."""

    schema_version: Literal["2.0"] = "2.0"

    title: str = Field(min_length=1, max_length=300)

    width: int = Field(default=1920, ge=320, le=3840)
    height: int = Field(default=1080, ge=240, le=2160)

    fps: int = Field(default=25, ge=1, le=60)

    background: str = "#101827"

    style: dict[str, Any] = Field(default_factory=dict)

    speech_units: list[SpeechUnit] = Field(min_length=1)

    shots: list[Shot] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_whole_story(self):
        units = [unit.unit_id for unit in self.speech_units]

        if len(units) != len(set(units)):
            raise ValueError("Duplicate speech unit IDs")

        # Character offsets must form one continuous text.
        cursor = 0

        for unit in self.speech_units:
            if unit.char_start != cursor:
                raise ValueError(
                    f"{unit.unit_id}: speech text has a gap or overlap"
                )

            cursor = unit.char_end

        # Every speech unit is assigned to exactly one shot, in source order.
        assigned = [
            unit_id
            for shot in self.shots
            for unit_id in shot.speech_unit_ids
        ]

        if assigned != units:
            raise ValueError(
                "Shots must cover all speech units exactly once and in order"
            )

        unit_registry = {
            unit.unit_id: unit
            for unit in self.speech_units
        }

        for shot in self.shots:
            for event in shot.events:
                if event.anchor.unit_id not in shot.speech_unit_ids:
                    raise ValueError(
                        f"{shot.shot_id}: event anchor is outside its shot"
                    )

        return self