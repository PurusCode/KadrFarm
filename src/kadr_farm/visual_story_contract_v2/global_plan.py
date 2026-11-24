"""Whole-video editorial direction contract (Global Director output)."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, model_validator

from .common import (
    ID_PATTERN,
    StrictModel,
    NarrativeRole,
    VisualMode,
    VisualRelation,
    ensure_unique,
)


class VisualStyle(StrictModel):
    visual_language: str = Field(min_length=3, max_length=100)
    palette: list[str] = Field(default_factory=list, max_length=12)
    typography: str = Field(default="editorial_sans", min_length=2, max_length=100)
    contrast: Literal["low", "medium", "high", "very_high"] = "high"
    texture: str = Field(default="clean", min_length=2, max_length=100)
    camera_language: list[str] = Field(default_factory=list, max_length=12)
    motion_language: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def validate_palette(self):
        for color in self.palette:
            if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
                raise ValueError(f"palette color must be #RRGGBB: {color!r}")
        return self


class SceneRelation(StrictModel):
    kind: VisualRelation
    source_scene_id: str | None = Field(default=None, pattern=ID_PATTERN)
    reason: str = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def validate_source_requirement(self):
        if self.kind == "introduces" and self.source_scene_id is not None:
            raise ValueError("introduces relation must not reference a source scene")
        if self.kind != "introduces" and self.source_scene_id is None:
            raise ValueError(f"{self.kind} relation requires source_scene_id")
        return self


class PlannedShotRange(StrictModel):
    """Editorial estimate only; never a quota or a required exact shot count."""

    minimum: int = Field(default=1, ge=1, le=30)
    maximum: int = Field(default=3, ge=1, le=30)

    @model_validator(mode="after")
    def validate_order(self):
        if self.maximum < self.minimum:
            raise ValueError("planned_shot_count.maximum must be >= minimum")
        return self


class GlobalSceneDirection(StrictModel):
    scene_id: str = Field(pattern=ID_PATTERN)
    narrative_role: NarrativeRole
    visual_purpose: str = Field(min_length=5, max_length=1000)
    visual_strategy: str = Field(min_length=5, max_length=1500)
    dominant_visual_modes: list[VisualMode] = Field(default_factory=list, max_length=8)
    planned_shot_count: PlannedShotRange | None = None
    relation_to_previous: SceneRelation
    motif_refs: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_motif_refs(self):
        ensure_unique(self.motif_refs, "motif reference")
        return self


class VisualArc(StrictModel):
    arc_id: str = Field(pattern=ID_PATTERN)
    purpose: str = Field(min_length=5, max_length=500)
    scene_ids: list[str] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_scene_ids(self):
        ensure_unique(self.scene_ids, "scene id in visual arc")
        return self


class VisualMotif(StrictModel):
    """Optional recurring image/device. Empty motif lists are fully valid."""

    motif_id: str = Field(pattern=ID_PATTERN)
    kind: Literal[
        "physical_object", "graphic_image", "symbol", "photographic_element",
        "visual_metaphor", "diagram_element",
    ]
    description: str = Field(min_length=5, max_length=1000)
    first_scene: str = Field(pattern=ID_PATTERN)
    returns_in: list[str] = Field(default_factory=list, max_length=200)
    purpose: str = Field(min_length=5, max_length=500)

    @model_validator(mode="after")
    def validate_returns(self):
        ensure_unique(self.returns_in, "motif return scene")
        if self.first_scene in self.returns_in:
            raise ValueError("motif first_scene must not be repeated in returns_in")
        return self


class VisualStoryPlan(StrictModel):
    """One complete, source-anchored visual direction for the entire video."""

    schema_version: Literal["2.0"] = "2.0"
    plan_id: str = Field(pattern=ID_PATTERN)
    title: str = Field(min_length=1, max_length=300)
    storyboard_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    visual_thesis: str = Field(min_length=10, max_length=1000)
    arc_summary: str = Field(min_length=10, max_length=2000)
    style: VisualStyle
    arc: list[VisualArc] = Field(default_factory=list, max_length=50)
    motifs: list[VisualMotif] = Field(default_factory=list, max_length=100)
    scenes: list[GlobalSceneDirection] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_story_structure(self):
        scene_ids = [scene.scene_id for scene in self.scenes]
        ensure_unique(scene_ids, "scene_id")
        order = {scene_id: index for index, scene_id in enumerate(scene_ids)}

        for index, scene in enumerate(self.scenes):
            relation = scene.relation_to_previous
            if index == 0 and relation.kind != "introduces":
                raise ValueError("first scene must introduce the visual story")
            if relation.kind == "introduces":
                if relation.source_scene_id is not None:
                    raise ValueError("first/newly introduced scene cannot have a source scene")
            else:
                source_index = order.get(relation.source_scene_id or "")
                if source_index is None or source_index >= index:
                    raise ValueError(
                        f"{scene.scene_id}: relation source must be an existing earlier scene"
                    )

        arc_ids = [arc.arc_id for arc in self.arc]
        ensure_unique(arc_ids, "arc_id")
        for arc in self.arc:
            indexes = [order.get(scene_id) for scene_id in arc.scene_ids]
            if any(index is None for index in indexes):
                raise ValueError(f"{arc.arc_id}: arc references an unknown scene")
            if indexes != sorted(indexes):
                raise ValueError(f"{arc.arc_id}: scenes must follow source order")

        motif_ids = [motif.motif_id for motif in self.motifs]
        ensure_unique(motif_ids, "motif_id")
        motif_registry = {motif.motif_id: motif for motif in self.motifs}
        for motif in self.motifs:
            declared = [motif.first_scene, *motif.returns_in]
            indexes = [order.get(scene_id) for scene_id in declared]
            if any(index is None for index in indexes):
                raise ValueError(f"{motif.motif_id}: motif references an unknown scene")
            if indexes != sorted(indexes):
                raise ValueError(f"{motif.motif_id}: returns_in must follow source order")

        for scene in self.scenes:
            unknown = [ref for ref in scene.motif_refs if ref not in motif_registry]
            if unknown:
                raise ValueError(f"{scene.scene_id}: unknown motif references: {unknown}")

        for motif in self.motifs:
            actual = [scene.scene_id for scene in self.scenes if motif.motif_id in scene.motif_refs]
            expected = [motif.first_scene, *motif.returns_in]
            if actual != expected:
                raise ValueError(
                    f"{motif.motif_id}: scene motif_refs {actual} do not match declaration {expected}"
                )

        return self
