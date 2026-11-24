"""Additive two-pass Visual Story 2.0 authoring/execution contracts.

This package is intentionally separate from the existing
``kadr_farm.visual_story_v2`` execution schema and its renderer integrations.
It does not replace or import the legacy runtime contract.
"""
from .common import Diagnostic, SourceRef, StrictModel
from .events import (
    ContentEvent,
    EmphasisEvent,
    EventTarget,
    EventTrigger,
    GraphicEvent,
    MediaEvent,
    SpeechAnchor,
    TimingIntent,
    TransformEvent,
    VisibilityEvent,
    VisualEvent,
)
from .execution import (
    AssetRequest,
    ExecutionOutputSpec,
    ExecutionPlan,
    RenderTask,
    RendererCapabilities,
    RendererTarget,
)
from .global_plan import (
    GlobalSceneDirection,
    PlannedShotRange,
    SceneRelation,
    VisualArc,
    VisualMotif,
    VisualStoryPlan,
    VisualStyle,
)
from .objects import (
    AppearanceSpec,
    CalloutObject,
    ChartObject,
    CompositionPlan,
    CounterObject,
    FormulaObject,
    GroupObject,
    IconObject,
    ImageObject,
    LayerSpec,
    NodeGraphObject,
    ShapeObject,
    SpatialRelationship,
    TableObject,
    TextObject,
    VideoObject,
    VisualObject,
)
from .scene_plan import (
    ContinuityLink,
    FallbackPolicy,
    SceneCompositionPlan,
    ShotContinuity,
    ShotPlan,
)
from .validation import (
    validate_continuity_references,
    validate_global_plan_against_storyboard,
    validate_global_plan_coverage,
    validate_scene_composition_plan,
    validate_scene_plan_coverage,
    validate_visual_story_bundle,
)

__all__ = [name for name in globals() if not name.startswith("_")]
