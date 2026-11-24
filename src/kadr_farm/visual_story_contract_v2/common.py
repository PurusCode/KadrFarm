"""Shared strict primitives for the two-pass Visual Story contract 2.0."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]{0,99}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"

Identifier = str
Sha256 = str


class StrictModel(BaseModel):
    """Base class for all authored and compiled contracts.

    Unknown fields are rejected. Assignment validation remains enabled so an
    already-validated model cannot silently become invalid later in a pipeline.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=True,
        allow_inf_nan=False,
    )


class SourceRef(StrictModel):
    """Stable reference to an authoritative storyboard scene/shot/object."""

    scene_id: str = Field(pattern=ID_PATTERN)
    shot_id: str | None = Field(default=None, pattern=ID_PATTERN)
    object_id: str | None = Field(default=None, pattern=ID_PATTERN)


class Diagnostic(StrictModel):
    code: str = Field(min_length=2, max_length=80, pattern=r"^[a-z][a-z0-9_.-]+$")
    severity: Literal["info", "warning", "error"]
    message: str = Field(min_length=1, max_length=2000)
    source_ref: SourceRef | None = None


def ensure_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")


def is_word_character(value: str) -> bool:
    return bool(value) and (value.isalnum() or value == "_")


def validate_exact_text_span(
    *,
    source_text: str,
    char_start: int,
    char_end: int,
    text_excerpt: str,
    label: str = "speech anchor",
) -> None:
    """Validate an exact Python-string slice and reject spans cutting a word."""
    if char_start < 0 or char_end <= char_start or char_end > len(source_text):
        raise ValueError(f"{label}: character range is outside source text")
    if source_text[char_start:char_end] != text_excerpt:
        raise ValueError(f"{label}: text_excerpt does not match source text exactly")
    if (
        char_start > 0
        and is_word_character(source_text[char_start - 1])
        and is_word_character(source_text[char_start])
    ):
        raise ValueError(f"{label}: char_start splits a word")
    if (
        char_end < len(source_text)
        and is_word_character(source_text[char_end - 1])
        and is_word_character(source_text[char_end])
    ):
        raise ValueError(f"{label}: char_end splits a word")


# Shared editorial vocabulary. These are semantic labels, not renderer guarantees.
NarrativeRole = Literal[
    "hook", "setup", "context", "development", "turn", "reveal",
    "consequence", "resolution", "transition", "call_to_action",
]

VisualRelation = Literal[
    "introduces", "continues", "develops", "contrasts", "resolves",
    "callbacks", "transitions",
]

VisualMode = Literal[
    "stock_image", "stock_video", "typography", "graphic", "diagram",
    "chart", "table", "formula", "screencast", "illustration", "mixed_media",
]

SemanticRegion = Literal[
    "background", "full_frame", "left", "center", "right", "top",
    "bottom", "foreground", "picture_in_picture", "auto",
]

SemanticAnchor = Literal[
    "center", "top_left", "top_center", "top_right", "center_left",
    "center_right", "bottom_left", "bottom_center", "bottom_right", "auto",
]

SemanticSize = Literal["tiny", "small", "medium", "large", "hero", "full", "auto"]

ObjectRole = Literal[
    "background", "environment", "primary_media", "focal", "supporting",
    "annotation", "typography", "decoration",
]

ObjectState = Literal[
    "hidden", "visible", "emphasized", "dimmed", "collapsed", "expanded",
    "transformed", "removed",
]
