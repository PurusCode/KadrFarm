"""Pydantic v2 contracts for the semantic-first production pipeline."""
from __future__ import annotations

from enum import Enum
from math import isfinite
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, use_enum_values=False)


class AssetStrategy(str, Enum):
    STOCK = "stock"
    RENDERED = "rendered"
    GENERATED = "generated"
    MANUAL = "manual"
    SCREENCAST = "screencast"


class VisualType(str, Enum):
    TEXT = "text"
    DIAGRAM = "diagram"
    PHOTO = "photo"
    VIDEO = "video"
    SCHEME = "scheme"
    BROLL = "broll"
    JOKE = "joke"
    CHART = "chart"
    SCREENCAST = "screencast"
    ILLUSTRATION = "illustration"
    QUOTE = "quote"
    COMPARISON = "comparison"


class Pacing(str, Enum):
    SLOW = "slow"
    NORMAL = "normal"
    FAST = "fast"


class CameraMove(str, Enum):
    STATIC = "static"
    SLOW_ZOOM = "slow_zoom"
    SLOW_PAN = "slow_pan"
    ZOOM_IN = "zoom_in"
    ZOOM_OUT = "zoom_out"
    KEN_BURNS = "ken_burns"


class Transition(str, Enum):
    CUT = "cut"
    FADE = "fade"
    WIPE_LEFT = "wipe_left"
    WIPE_RIGHT = "wipe_right"
    DISSOLVE = "dissolve"


class ClaimStatus(str, Enum):
    NO_SOURCE = "no_source"
    EDITORIAL_ESTIMATE = "editorial_estimate"
    VERIFY_BEFORE_PUBLISH = "verify_before_publish"
    VERIFIED = "verified"


class SourceAvailability(str, Enum):
    NOT_CHECKED = "not_checked"
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"


class SceneSegmentDraft(StrictModel):
    """Only semantic segmentation; visual decisions belong to visual_planner."""
    voice_text: str = Field(min_length=1)
    meaning: str = Field(min_length=3, max_length=500)
    purpose: str = Field(min_length=3, max_length=500)


class StoryboardBlockDraft(StrictModel):
    block_id: str = Field(min_length=1, max_length=100)
    segments: list[SceneSegmentDraft] = Field(min_length=1)


class StoryboardResponse(StrictModel):
    segments: list[SceneSegmentDraft] = Field(min_length=1)


class ClaimDraft(StrictModel):
    text: str = Field(min_length=5, max_length=700)
    source_urls: list[str]

    @field_validator("source_urls")
    @classmethod
    def valid_urls(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value.startswith(("https://", "http://")):
                raise ValueError(f"Источник должен быть HTTP(S) URL: {value}")
        return values


class Claim(StrictModel):
    text: str = Field(min_length=5, max_length=700)
    source_urls: list[str]
    status: ClaimStatus
    source_availability: list[SourceAvailability]
    verified_by_human: bool = False

    @model_validator(mode="after")
    def human_verification_gate(self):
        if self.status == ClaimStatus.VERIFIED and not self.verified_by_human:
            raise ValueError("Статус verified возможен только после явного подтверждения человеком")
        if self.verified_by_human and self.status != ClaimStatus.VERIFIED:
            raise ValueError("verified_by_human=true требует status=verified")
        if self.status == ClaimStatus.NO_SOURCE and self.source_urls:
            raise ValueError("no_source не должен содержать source_urls")
        if self.status == ClaimStatus.VERIFY_BEFORE_PUBLISH and not self.source_urls:
            raise ValueError("verify_before_publish требует хотя бы один source_url")
        if len(self.source_availability) != len(self.source_urls):
            raise ValueError("source_availability должен соответствовать source_urls")
        return self


class AudioSFXDraft(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    purpose: str = Field(min_length=3, max_length=300)


class AudioSFX(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    purpose: str = Field(min_length=3, max_length=300)
    source_url: Optional[str]
    license_name: Optional[str]
    rights_status: str = "not_cleared"


class ChartData(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    labels: list[str] = Field(min_length=2, max_length=20)
    values: list[float] = Field(min_length=2, max_length=20)
    unit: str = Field(min_length=1, max_length=50)
    source_url: str = Field(min_length=10)
    methodology: str = Field(min_length=5, max_length=1000)

    @field_validator("source_url")
    @classmethod
    def chart_source_is_url(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("Для графика нужен HTTP(S) источник")
        return value

    @model_validator(mode="after")
    def equal_lengths_and_finite_values(self):
        if len(self.labels) != len(self.values):
            raise ValueError("labels и values должны иметь одинаковую длину")
        if any(not (float("-inf") < v < float("inf")) for v in self.values):
            raise ValueError("Значения графика должны быть конечными числами")
        return self


def _validate_provider_pair(strategy: AssetStrategy, provider_id: str) -> None:
    expected = {
        AssetStrategy.STOCK: "pixabay",
        AssetStrategy.RENDERED: "internal_renderer",
        AssetStrategy.MANUAL: "manual_input",
        AssetStrategy.SCREENCAST: "manual_input",
    }
    if strategy in expected and provider_id != expected[strategy]:
        raise ValueError(
            f"Для asset_strategy={strategy.value} в этой версии допустим "
            f"provider_id={expected[strategy]} (получено {provider_id})"
        )


def _normalize_visual_beat_ratios(payload: Any) -> Any:
    """Treat beat ratios as relative weights and normalize them to one whole scene.

    LLMs often return 1.0 for each beat to mean "equal duration". Requiring the
    model itself to add fractions to exactly 1.0 creates avoidable repair calls.
    Invalid, missing, non-finite, or non-positive values are left untouched so
    Pydantic can report the actual schema error.
    """
    if not isinstance(payload, dict):
        return payload
    beats = payload.get("visual_beats")
    if not isinstance(beats, list) or not beats:
        return payload

    weights: list[float] = []
    for beat in beats:
        if not isinstance(beat, dict) or "sub_duration_ratio" not in beat:
            return payload
        raw_weight = beat["sub_duration_ratio"]
        if isinstance(raw_weight, bool):
            return payload
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError):
            return payload
        if not isfinite(weight) or weight <= 0:
            return payload
        weights.append(weight)

    total = sum(weights)
    if not isfinite(total) or total <= 0:
        return payload
    if abs(total - 1.0) <= 1e-9:
        return payload

    normalized_beats: list[dict[str, Any]] = []
    assigned = 0.0
    for index, (beat, weight) in enumerate(zip(beats, weights)):
        normalized = (1.0 - assigned) if index == len(weights) - 1 else weight / total
        assigned += normalized
        updated_beat = dict(beat)
        updated_beat["sub_duration_ratio"] = normalized
        normalized_beats.append(updated_beat)

    normalized_payload = dict(payload)
    normalized_payload["visual_beats"] = normalized_beats
    return normalized_payload


class VisualBeatDraft(StrictModel):
    visual_type: VisualType
    visual_description: str = Field(min_length=10, max_length=1500)
    asset_strategy: AssetStrategy
    provider_id: str = Field(min_length=1, max_length=100)
    gen_prompt: Optional[str]
    search_queries: list[str]
    sub_duration_ratio: float = Field(gt=0.0, le=1.0)
    on_screen_text: list[str]

    @model_validator(mode="after")
    def provider_and_chart_rules(self):
        _validate_provider_pair(self.asset_strategy, self.provider_id)
        if self.asset_strategy == AssetStrategy.GENERATED:
            raise ValueError("generated provider пока не подключён")
        if self.visual_type == VisualType.CHART and self.asset_strategy != AssetStrategy.RENDERED:
            raise ValueError("chart beat должен использовать rendered/internal_renderer")
        return self


class VisualBeat(VisualBeatDraft):
    pass


class VisualPlanDraft(StrictModel):
    @model_validator(mode="before")
    @classmethod
    def normalize_beat_weights(cls, value: Any) -> Any:
        return _normalize_visual_beat_ratios(value)

    scene_id: str = Field(min_length=1, max_length=30)
    visual_type: VisualType
    visual_description: str = Field(min_length=10, max_length=1500)
    visual_beats: list[VisualBeatDraft]
    asset_strategy: AssetStrategy
    provider_id: str = Field(min_length=1, max_length=100)
    gen_prompt: Optional[str]
    search_queries: list[str]
    on_screen_text: list[str]
    speaker_emotion: Optional[str]
    pacing: Pacing
    camera_move: CameraMove
    music_cue: Optional[str]
    audio_sfx: list[AudioSFXDraft]
    claims: list[ClaimDraft]
    chart_data: Optional[ChartData]
    humor_note: str
    transition_in: Transition
    transition_out: Transition

    @field_validator("on_screen_text")
    @classmethod
    def readable_screen_text(cls, values: list[str]) -> list[str]:
        too_long = [x for x in values if len(x) > 40]
        if too_long:
            raise ValueError(f"Экранные надписи длиннее 40 символов: {too_long}")
        return values

    @model_validator(mode="after")
    def visual_requirements(self):
        _validate_provider_pair(self.asset_strategy, self.provider_id)
        if self.asset_strategy == AssetStrategy.GENERATED:
            raise ValueError("generated provider пока не подключён")
        chart_required = self.visual_type == VisualType.CHART or any(
            beat.visual_type == VisualType.CHART for beat in self.visual_beats
        )
        if chart_required and self.chart_data is None:
            raise ValueError("Для chart-сцены или chart beat обязателен chart_data с источником")
        if self.visual_type == VisualType.CHART and self.asset_strategy != AssetStrategy.RENDERED:
            raise ValueError("chart-сцена должна использовать rendered/internal_renderer")
        if self.visual_beats:
            total = sum(x.sub_duration_ratio for x in self.visual_beats)
            if abs(total - 1.0) > 0.01:
                raise ValueError(f"Сумма sub_duration_ratio должна быть 1.0, сейчас {total:.4f}")
        return self


class VisualPlanBatch(StrictModel):
    plans: list[VisualPlanDraft] = Field(min_length=1)


class MediaRef(StrictModel):
    path: str
    media_type: str
    sha256: str
    source: str
    source_url: Optional[str]
    creator: Optional[str]
    license_name: Optional[str]
    rights_status: str = "review_required"
    duration_ratio: float = Field(default=1.0, gt=0.0, le=1.0)


class MediaRefs(StrictModel):
    audio: Optional[MediaRef] = None
    visuals: list[MediaRef] = Field(default_factory=list)


class Scene(StrictModel):
    @model_validator(mode="before")
    @classmethod
    def normalize_beat_weights(cls, value: Any) -> Any:
        return _normalize_visual_beat_ratios(value)

    id: str = Field(pattern=r"^s_\d{3,}$")
    block_id: str = Field(min_length=1, max_length=100)
    voice_text: str = Field(min_length=1)
    meaning: str = Field(min_length=3, max_length=500)
    purpose: str = Field(min_length=3, max_length=500)
    visual_type: VisualType
    visual_description: str = Field(min_length=10, max_length=1500)
    visual_beats: list[VisualBeat] = Field(default_factory=list)
    asset_strategy: AssetStrategy
    provider_id: str
    gen_prompt: Optional[str]
    search_queries: list[str] = Field(default_factory=list)
    on_screen_text: list[str] = Field(default_factory=list)
    speaker_emotion: Optional[str] = None
    pacing: Pacing = Pacing.NORMAL
    camera_move: CameraMove = CameraMove.STATIC
    music_cue: Optional[str] = None
    audio_sfx: list[AudioSFX] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    chart_data: Optional[ChartData] = None
    humor_note: str = ""
    transition_in: Transition = Transition.CUT
    transition_out: Transition = Transition.CUT
    duration_sec: Optional[float] = Field(default=None, gt=0)
    start_sec: Optional[float] = Field(default=None, ge=0)
    end_sec: Optional[float] = Field(default=None, ge=0)
    media_refs: MediaRefs = Field(default_factory=MediaRefs)

    @field_validator("on_screen_text")
    @classmethod
    def readable_final_captions(cls, values: list[str]) -> list[str]:
        if any(len(value) > 40 for value in values):
            raise ValueError("Экранная надпись не должна превышать 40 символов")
        return values

    @model_validator(mode="after")
    def validate_chart_and_beats(self):
        _validate_provider_pair(self.asset_strategy, self.provider_id)
        if self.asset_strategy == AssetStrategy.GENERATED:
            raise ValueError("generated provider пока не подключён")
        chart_required = self.visual_type == VisualType.CHART or any(
            beat.visual_type == VisualType.CHART for beat in self.visual_beats
        )
        if chart_required and self.chart_data is None:
            raise ValueError("Для chart-сцены или chart beat обязателен chart_data")
        if self.visual_type == VisualType.CHART and self.asset_strategy != AssetStrategy.RENDERED:
            raise ValueError("chart-сцена должна использовать rendered/internal_renderer")
        if self.visual_beats:
            total = sum(x.sub_duration_ratio for x in self.visual_beats)
            if abs(total - 1.0) > 0.01:
                raise ValueError("Сумма долей visual_beats должна быть равна 1.0")
        timing = (self.duration_sec, self.start_sec, self.end_sec)
        if any(value is not None for value in timing):
            if not all(value is not None for value in timing):
                raise ValueError("duration_sec, start_sec и end_sec должны задаваться вместе")
            if self.end_sec < self.start_sec:
                raise ValueError("end_sec не может быть меньше start_sec")
            if abs((self.end_sec - self.start_sec) - self.duration_sec) > 0.08:
                raise ValueError("end_sec-start_sec не совпадает с duration_sec")
        return self


class StoryboardDocument(StrictModel):
    schema_version: str = "1.0"
    title: str
    source_script_sha256: str
    scenes: list[Scene] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("scenes")
    @classmethod
    def unique_scene_ids(cls, values: list[Scene]) -> list[Scene]:
        ids = [x.id for x in values]
        if len(ids) != len(set(ids)):
            raise ValueError("ID сцен должны быть уникальными")
        return values
