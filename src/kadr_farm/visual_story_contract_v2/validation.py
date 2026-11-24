"""Cross-document validation for storyboard, plans, speech anchors and continuity."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .common import validate_exact_text_span
from .global_plan import VisualStoryPlan
from .scene_plan import SceneCompositionPlan


def _scene_id(scene: dict[str, Any]) -> str:
    value = scene.get("id", scene.get("scene_id"))
    if not isinstance(value, str) or not value:
        raise ValueError("source storyboard scene is missing a non-empty id/scene_id")
    return value


def _voice_text(scene: dict[str, Any]) -> str:
    value = scene.get("voice_text")
    if not isinstance(value, str) or not value:
        raise ValueError(f"{_scene_id(scene)}: source storyboard voice_text is missing or empty")
    return value


def validate_global_plan_coverage(
    plan: VisualStoryPlan,
    source_scenes: list[dict[str, Any]],
) -> None:
    """Require exact one-to-one scene coverage in authoritative storyboard order."""
    source_ids = [_scene_id(scene) for scene in source_scenes]
    plan_ids = [scene.scene_id for scene in plan.scenes]
    if plan_ids != source_ids:
        raise ValueError(
            "global plan scene coverage/order differs from authoritative storyboard: "
            f"expected={source_ids}, actual={plan_ids}"
        )



def validate_global_plan_against_storyboard(
    plan: VisualStoryPlan,
    storyboard: dict[str, Any],
) -> None:
    """Verify both canonical storyboard fingerprint and complete scene order."""
    canonical = json.dumps(storyboard, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    actual_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if plan.storyboard_sha256 != actual_hash:
        raise ValueError("global plan storyboard_sha256 does not match authoritative storyboard")
    scenes = storyboard.get("scenes")
    if not isinstance(scenes, list):
        raise ValueError("authoritative storyboard must contain a scenes list")
    validate_global_plan_coverage(plan, scenes)

def validate_scene_composition_plan(
    plan: SceneCompositionPlan,
    source_scene: dict[str, Any],
) -> None:
    """Bind every authored speech anchor to the immutable source scene text."""
    scene_id = _scene_id(source_scene)
    text = _voice_text(source_scene)
    if plan.scene_id != scene_id:
        raise ValueError(f"scene plan id {plan.scene_id!r} does not match source scene {scene_id!r}")
    expected_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if plan.source_text_sha256 != expected_hash:
        raise ValueError(f"{scene_id}: source_text_sha256 does not match authoritative voice_text")
    for shot in plan.shots:
        for anchor in shot.speech_anchors:
            if anchor.source_scene_id != scene_id:
                raise ValueError(f"{shot.shot_id}/{anchor.anchor_id}: wrong source scene")
            validate_exact_text_span(
                source_text=text,
                char_start=anchor.char_start,
                char_end=anchor.char_end,
                text_excerpt=anchor.text_excerpt,
                label=f"{shot.shot_id}/{anchor.anchor_id}",
            )


def validate_scene_plan_coverage(
    plans: list[SceneCompositionPlan],
    source_scenes: list[dict[str, Any]],
) -> None:
    """Require exactly one composition plan per source scene, in source order."""
    source_ids = [_scene_id(scene) for scene in source_scenes]
    plan_ids = [plan.scene_id for plan in plans]
    if plan_ids != source_ids:
        raise ValueError(
            "scene composition plan coverage/order differs from storyboard: "
            f"expected={source_ids}, actual={plan_ids}"
        )
    source_by_id = {_scene_id(scene): scene for scene in source_scenes}
    for plan in plans:
        validate_scene_composition_plan(plan, source_by_id[plan.scene_id])


def validate_continuity_references(
    plans: list[SceneCompositionPlan],
) -> None:
    """Resolve cross-shot references and verify direction/order of continuity."""
    scene_order = {plan.scene_id: index for index, plan in enumerate(plans)}
    shot_order: dict[tuple[str, str], int] = {}
    objects: dict[tuple[str, str, str], Any] = {}
    for plan in plans:
        for shot_index, shot in enumerate(plan.shots):
            shot_order[(plan.scene_id, shot.shot_id)] = shot_index
            for obj in shot.composition.objects:
                objects[(plan.scene_id, shot.shot_id, obj.object_id)] = obj

    for plan in plans:
        current_scene_index = scene_order[plan.scene_id]
        for shot_index, shot in enumerate(plan.shots):
            for link in shot.continuity.incoming:
                _validate_continuity_ref(
                    link, plan.scene_id, shot.shot_id, current_scene_index, shot_index,
                    scene_order, shot_order, objects, expected_direction="incoming",
                )
            for link in shot.continuity.outgoing:
                _validate_continuity_ref(
                    link, plan.scene_id, shot.shot_id, current_scene_index, shot_index,
                    scene_order, shot_order, objects, expected_direction="outgoing",
                )


def _validate_continuity_ref(
    link,
    current_scene_id: str,
    current_shot_id: str,
    current_scene_index: int,
    current_shot_index: int,
    scene_order: dict[str, int],
    shot_order: dict[tuple[str, str], int],
    objects: dict[tuple[str, str, str], Any],
    *,
    expected_direction: str,
) -> None:
    ref = link.reference
    if ref.scene_id not in scene_order:
        raise ValueError(f"continuity reference points to unknown scene: {ref.scene_id}")
    ref_scene_index = scene_order[ref.scene_id]
    if ref.shot_id is not None:
        if (ref.scene_id, ref.shot_id) not in shot_order:
            raise ValueError(f"continuity reference points to unknown shot: {ref.scene_id}/{ref.shot_id}")
        ref_shot_index = shot_order[(ref.scene_id, ref.shot_id)]
    else:
        ref_shot_index = None

    earlier = ref_scene_index < current_scene_index or (
        ref_scene_index == current_scene_index
        and ref_shot_index is not None
        and ref_shot_index < current_shot_index
    )
    later = ref_scene_index > current_scene_index or (
        ref_scene_index == current_scene_index
        and ref_shot_index is not None
        and ref_shot_index > current_shot_index
    )
    if expected_direction == "incoming" and not earlier:
        raise ValueError("incoming continuity reference must point to an earlier scene/shot")
    if expected_direction == "outgoing" and not later:
        raise ValueError("outgoing continuity reference must point to a later scene/shot")

    source_obj = None
    if ref.object_id is not None:
        if ref.shot_id is None or (ref.scene_id, ref.shot_id, ref.object_id) not in objects:
            raise ValueError(f"continuity reference points to unknown object: {ref.scene_id}/{ref.shot_id}/{ref.object_id}")
        source_obj = objects[(ref.scene_id, ref.shot_id, ref.object_id)]
    elif link.relation == "object":
        raise ValueError("object continuity reference must identify an object")

    if link.continuity_id is not None:
        if source_obj is None or source_obj.continuity_id != link.continuity_id:
            raise ValueError("continuity_id does not match the referenced source object's continuity_id")
        current_objects = [
            obj for (scene_id, shot_id, _object_id), obj in objects.items()
            if scene_id == current_scene_id and shot_id == current_shot_id
        ]
        if not any(obj.continuity_id == link.continuity_id for obj in current_objects):
            raise ValueError("continuity_id is not declared by any object in the current shot")


def validate_visual_story_bundle(
    global_plan: VisualStoryPlan,
    scene_plans: list[SceneCompositionPlan],
    storyboard: dict[str, Any],
) -> None:
    """Validate a complete pair of director passes against the source storyboard."""
    scenes = storyboard.get("scenes")
    if not isinstance(scenes, list):
        raise ValueError("authoritative storyboard must contain a scenes list")
    validate_global_plan_against_storyboard(global_plan, storyboard)
    validate_scene_plan_coverage(scene_plans, scenes)
    validate_continuity_references(scene_plans)
