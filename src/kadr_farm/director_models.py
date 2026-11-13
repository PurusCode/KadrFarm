"""Typed contracts for AI Video Factory's autonomous production director."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

VisualType = Literal["text", "diagram", "photo", "video", "scheme", "broll", "joke", "screencast", "illustration", "quote", "comparison"]
MotionType = Literal["static", "slow_zoom", "slow_pan", "zoom_out"]
TransitionType = Literal["cut", "fade"]

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class SFXIdea(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    purpose: str = Field(min_length=3, max_length=300)

class ClaimCandidate(StrictModel):
    text: str = Field(min_length=5, max_length=700)
    source_urls: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("source_urls")
    @classmethod
    def urls_must_be_http(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value.startswith(("https://", "http://")):
                raise ValueError(f"Источник должен быть HTTP(S) URL: {value}")
        return values

class DirectorBeat(StrictModel):
    """One exact spoken phrase and its matching visual shot."""
    spoken_text: str = Field(min_length=1, max_length=3000)
    visual_type: VisualType
    visual_description: str = Field(min_length=10, max_length=1500)
    asset_strategy: Literal["stock", "rendered"]
    provider_id: Literal["pixabay", "internal_renderer"]
    gen_prompt: str | None = Field(default=None, max_length=2000)
    search_queries: list[str] = Field(default_factory=list, max_length=4)
    on_screen_text: list[str] = Field(default_factory=list, max_length=4)
    motion: MotionType = "static"
    transition_in: TransitionType = "cut"
    audio_sfx: list[SFXIdea] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def provider_matches_strategy(self):
        expected = "pixabay" if self.asset_strategy == "stock" else "internal_renderer"
        if self.provider_id != expected:
            raise ValueError(f"{self.asset_strategy} requires provider_id={expected}")
        if self.asset_strategy == "stock" and not self.search_queries:
            raise ValueError("Для stock beat нужны конкретные search_queries")
        if self.asset_strategy == "stock" and self.visual_type not in ("photo", "video", "broll"):
            raise ValueError("Для Pixabay выбери visual_type photo, video или broll")
        if self.asset_strategy == "rendered" and self.visual_type in ("photo", "video", "broll"):
            raise ValueError("Для локальной графики выбери diagram, scheme, text, illustration, screencast, quote, joke или comparison")
        if any(len(x) > 40 for x in self.on_screen_text):
            raise ValueError("Экранный текст не должен превышать 40 символов")
        return self

class DirectorScenePlan(StrictModel):
    scene_id: str = Field(min_length=1, max_length=30)
    editorial_intent: str = Field(min_length=5, max_length=600)
    beats: list[DirectorBeat] = Field(min_length=1, max_length=6)
    claims: list[ClaimCandidate] = Field(default_factory=list, max_length=8)
    music_cue: str = Field(default="", max_length=300)

class DirectorBatch(StrictModel):
    plans: list[DirectorScenePlan] = Field(min_length=1)

class SceneCritique(StrictModel):
    scene_id: str
    verdict: Literal["accept", "revise"]
    relevance_score: int = Field(ge=0, le=5)
    feasibility_score: int = Field(ge=0, le=5)
    narration_coverage_score: int = Field(ge=0, le=5)
    factual_risk: Literal["low", "medium", "high"]
    problems: list[str] = Field(default_factory=list, max_length=10)
    revision_brief: str = Field(default="", max_length=1500)

class CritiqueBatch(StrictModel):
    reviews: list[SceneCritique] = Field(min_length=1)

class SourceEvidence(StrictModel):
    source_id: str
    verdict: Literal["supports", "contradicts", "insufficient", "unavailable"]
    evidence_quote: str = Field(default="", max_length=500)
    reasoning: str = Field(min_length=5, max_length=1000)

class SourceEvidenceBatch(StrictModel):
    assessments: list[SourceEvidence] = Field(min_length=1)
