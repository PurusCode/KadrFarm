"""Speech-anchored semantic event intents; no author-supplied wall-clock times."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from .common import ID_PATTERN, StrictModel


class SpeechAnchor(StrictModel):
    anchor_id: str = Field(pattern=ID_PATTERN)
    source_scene_id: str = Field(pattern=ID_PATTERN)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    text_excerpt: str = Field(min_length=1, max_length=2000)
    role: Literal[
        "opening", "subject", "action", "contrast", "emphasis", "conclusion",
        "pause", "illustration", "transition",
    ] = "illustration"

    @model_validator(mode="after")
    def validate_range_shape(self):
        if self.char_end <= self.char_start:
            raise ValueError("speech anchor char_end must exceed char_start")
        if self.char_end - self.char_start != len(self.text_excerpt):
            raise ValueError("speech anchor range length must match text_excerpt")
        return self


class EventTarget(StrictModel):
    object_id: str = Field(pattern=ID_PATTERN)
    subtarget_id: str | None = Field(default=None, pattern=ID_PATTERN)


class EventTrigger(StrictModel):
    kind: Literal["speech_anchor"] = "speech_anchor"
    anchor_id: str = Field(pattern=ID_PATTERN)


class TimingIntent(StrictModel):
    duration: Literal["instant", "brief", "medium", "long", "hold"] = "medium"
    easing: Literal["linear", "ease_in", "ease_out", "ease_in_out"] = "ease_out"
    stagger: Literal["none", "sequential", "grouped"] = "none"
    allow_overlap: bool = False
    hold_until_anchor_id: str | None = Field(default=None, pattern=ID_PATTERN)


class EventBase(StrictModel):
    event_id: str = Field(pattern=ID_PATTERN)
    target: EventTarget
    trigger: EventTrigger
    timing: TimingIntent = Field(default_factory=TimingIntent)


class VisibilityEvent(EventBase):
    action: Literal["appear", "reveal", "fade_in", "fade_out", "hide", "remove"]


class EmphasisEvent(EventBase):
    action: Literal["highlight", "dim", "pulse", "outline", "shake"]
    tone: Literal["neutral", "accent", "positive", "warning", "negative"] = "accent"
    intensity: Literal["subtle", "normal", "strong"] = "normal"


class TransformEvent(EventBase):
    action: Literal["move", "scale", "rotate", "transform", "morph", "crop_reframe"]
    destination_region: Literal[
        "background", "full_frame", "left", "center", "right", "top",
        "bottom", "foreground", "picture_in_picture",
    ] | None = None
    destination_anchor: Literal[
        "center", "top_left", "top_center", "top_right", "center_left",
        "center_right", "bottom_left", "bottom_center", "bottom_right",
    ] | None = None
    target_size: Literal["tiny", "small", "medium", "large", "hero", "full"] | None = None
    rotation_intent: Literal["quarter_turn_clockwise", "quarter_turn_counterclockwise", "half_turn"] | None = None
    target_object_id: str | None = Field(default=None, pattern=ID_PATTERN)
    crop_intent: str | None = Field(default=None, min_length=3, max_length=300)

    @model_validator(mode="after")
    def validate_action_parameters(self):
        required = {
            "move": (self.destination_region, self.destination_anchor),
            "scale": (self.target_size,),
            "rotate": (self.rotation_intent,),
            "transform": (self.target_object_id,),
            "morph": (self.target_object_id,),
            "crop_reframe": (self.crop_intent,),
        }
        if any(value is None for value in required[self.action]):
            raise ValueError(f"{self.action} is missing its required destination/target parameter")

        allowed_by_action = {
            "move": {"destination_region", "destination_anchor"},
            "scale": {"target_size"},
            "rotate": {"rotation_intent"},
            "transform": {"target_object_id"},
            "morph": {"target_object_id"},
            "crop_reframe": {"crop_intent"},
        }[self.action]
        supplied = {
            "destination_region": self.destination_region,
            "destination_anchor": self.destination_anchor,
            "target_size": self.target_size,
            "rotation_intent": self.rotation_intent,
            "target_object_id": self.target_object_id,
            "crop_intent": self.crop_intent,
        }
        extras = [name for name, value in supplied.items() if value is not None and name not in allowed_by_action]
        if extras:
            raise ValueError(f"{self.action} does not accept parameters: {extras}")
        return self


class GraphicEvent(EventBase):
    action: Literal[
        "draw_path", "draw_edge", "reveal_node", "reveal_row", "reveal_column",
        "fill_progress", "grow_bar", "plot_line",
    ]
    progress_intent: Literal["to_first_quarter", "to_half", "to_three_quarters", "to_complete"] | None = None

    @model_validator(mode="after")
    def validate_target(self):
        requires_subtarget = {
            "draw_edge", "reveal_node", "reveal_row", "reveal_column", "grow_bar",
        }
        if self.action in requires_subtarget and self.target.subtarget_id is None:
            raise ValueError(f"{self.action} requires target.subtarget_id")
        if self.action == "fill_progress" and self.progress_intent is None:
            raise ValueError("fill_progress requires progress_intent")
        if self.action != "fill_progress" and self.progress_intent is not None:
            raise ValueError(f"{self.action} does not accept progress_intent")
        return self


class ContentEvent(EventBase):
    action: Literal["count_up", "update_text", "update_value", "swap_content"]
    from_value_ref: str | None = Field(default=None, min_length=1, max_length=200)
    to_value_ref: str | None = Field(default=None, min_length=1, max_length=200)
    content_ref: str | None = Field(default=None, min_length=1, max_length=200)
    value_ref: str | None = Field(default=None, min_length=1, max_length=200)
    replacement_object_id: str | None = Field(default=None, pattern=ID_PATTERN)

    @model_validator(mode="after")
    def validate_action_parameters(self):
        required = {
            "count_up": (self.from_value_ref, self.to_value_ref),
            "update_text": (self.content_ref,),
            "update_value": (self.value_ref,),
            "swap_content": (self.replacement_object_id,),
        }
        if any(value is None for value in required[self.action]):
            raise ValueError(f"{self.action} is missing its required reference parameter")
        supplied = {
            "from_value_ref": self.from_value_ref,
            "to_value_ref": self.to_value_ref,
            "content_ref": self.content_ref,
            "value_ref": self.value_ref,
            "replacement_object_id": self.replacement_object_id,
        }
        allowed_by_action = {
            "count_up": {"from_value_ref", "to_value_ref"},
            "update_text": {"content_ref"},
            "update_value": {"value_ref"},
            "swap_content": {"replacement_object_id"},
        }[self.action]
        extras = [name for name, value in supplied.items() if value is not None and name not in allowed_by_action]
        if extras:
            raise ValueError(f"{self.action} does not accept parameters: {extras}")
        return self


class MediaEvent(EventBase):
    action: Literal["pan", "zoom", "ken_burns", "play", "freeze_frame", "crossfade"]
    movement: Literal["left", "right", "up", "down", "in", "out", "auto"] | None = None
    focus_region: Literal[
        "background", "full_frame", "left", "center", "right", "top", "bottom",
    ] | None = None

    @model_validator(mode="after")
    def validate_action_parameters(self):
        if self.action in {"pan", "zoom", "ken_burns"} and self.movement is None and self.focus_region is None:
            raise ValueError(f"{self.action} requires movement or focus_region")
        if self.action in {"play", "freeze_frame", "crossfade"} and (self.movement is not None or self.focus_region is not None):
            raise ValueError(f"{self.action} does not accept movement/focus_region")
        return self


VisualEvent = Annotated[
    Union[VisibilityEvent, EmphasisEvent, TransformEvent, GraphicEvent, ContentEvent, MediaEvent],
    Field(discriminator="action"),
]
