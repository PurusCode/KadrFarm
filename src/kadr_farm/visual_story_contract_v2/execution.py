"""Typed, deterministic compiler output contracts.

These models contain no LLM-authored timestamps. Measured audio scheduling is
added only after a later compiler stage has inspected real WAV/alignment data.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, model_validator

from .common import ID_PATTERN, Diagnostic, StrictModel, ensure_unique
from .events import VisualEvent
from .objects import VisualObject


class RendererCapabilities(StrictModel):
    renderer_id: str = Field(pattern=ID_PATTERN)
    renderer_version: str = Field(min_length=1, max_length=80)
    supported_kinds: list[str] = Field(min_length=1, max_length=30)
    supported_actions: list[str] = Field(default_factory=list, max_length=100)
    supported_chart_types: list[str] = Field(default_factory=list, max_length=20)
    supported_formats: list[str] = Field(min_length=1, max_length=20)
    max_objects_per_shot: int | None = Field(default=None, ge=1, le=10000)
    max_nesting_depth: int = Field(default=0, ge=0, le=50)

    @model_validator(mode="after")
    def validate_uniqueness(self):
        for label, values in (
            ("supported kind", self.supported_kinds),
            ("supported action", self.supported_actions),
            ("supported chart type", self.supported_chart_types),
            ("supported format", self.supported_formats),
        ):
            ensure_unique(values, label)
        return self


class RendererTarget(StrictModel):
    renderer_id: str = Field(pattern=ID_PATTERN)
    renderer_version: str = Field(min_length=1, max_length=80)


class AssetRequest(StrictModel):
    request_id: str = Field(pattern=ID_PATTERN)
    scene_id: str = Field(pattern=ID_PATTERN)
    shot_id: str = Field(pattern=ID_PATTERN)
    object_id: str = Field(pattern=ID_PATTERN)
    media_kind: Literal["image", "video"]
    description: str = Field(min_length=5, max_length=1000)
    search_queries: list[str] = Field(min_length=1, max_length=8)
    orientation: Literal["landscape", "portrait", "square", "any"] = "landscape"
    crop_intent: str = Field(min_length=3, max_length=300)
    provider_allowlist: list[str] = Field(min_length=1, max_length=10)
    desired_duration: Literal["brief", "medium", "long", "flexible"] | None = None

    @model_validator(mode="after")
    def validate_duration_scope(self):
        if self.media_kind == "image" and self.desired_duration is not None:
            raise ValueError("image asset requests must not specify video duration intent")
        if self.media_kind == "video" and self.desired_duration is None:
            raise ValueError("video asset requests require qualitative desired_duration")
        ensure_unique(self.provider_allowlist, "asset provider")
        return self


class RenderTask(StrictModel):
    task_id: str = Field(pattern=ID_PATTERN)
    scene_id: str = Field(pattern=ID_PATTERN)
    shot_id: str = Field(pattern=ID_PATTERN)
    object_id: str = Field(pattern=ID_PATTERN)
    target: RendererTarget
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_media_type: Literal["image/png", "image/svg+xml", "video/mp4", "audio/wav", "application/json"]

    @model_validator(mode="after")
    def validate_dependencies(self):
        ensure_unique(self.depends_on, "render task dependency")
        if self.task_id in self.depends_on:
            raise ValueError("render task cannot depend on itself")
        return self


class ExecutionOutputSpec(StrictModel):
    width: int = Field(ge=320, le=7680)
    height: int = Field(ge=240, le=4320)
    fps: int = Field(ge=1, le=120)
    container: Literal["mp4"] = "mp4"
    video_codec: Literal["h264", "hevc", "vp9"] = "h264"
    pixel_format: Literal["yuv420p", "yuv444p"] = "yuv420p"


class ExecutionPlan(StrictModel):
    execution_schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(pattern=ID_PATTERN)
    storyboard_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    global_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scene_plan_sha256: dict[str, str] = Field(min_length=1)
    capability_registry_version: str = Field(min_length=1, max_length=80)
    asset_requests: list[AssetRequest] = Field(default_factory=list, max_length=1000)
    render_tasks: list[RenderTask] = Field(default_factory=list, max_length=5000)
    output_spec: ExecutionOutputSpec
    diagnostics: list[Diagnostic] = Field(default_factory=list, max_length=2000)
    status: Literal["ready", "blocked"] = "ready"

    @model_validator(mode="after")
    def validate_plan_integrity(self):
        ensure_unique([item.request_id for item in self.asset_requests], "asset request id")
        task_ids = [item.task_id for item in self.render_tasks]
        ensure_unique(task_ids, "render task id")
        task_set = set(task_ids)
        for task in self.render_tasks:
            missing = [dep for dep in task.depends_on if dep not in task_set]
            if missing:
                raise ValueError(f"{task.task_id}: dependency references unknown task(s): {missing}")
        dependency_map = {task.task_id: task.depends_on for task in self.render_tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError(f"render task dependency cycle includes {task_id}")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in dependency_map[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in dependency_map:
            visit(task_id)
        has_errors = any(item.severity == "error" for item in self.diagnostics)
        if self.status == "ready" and has_errors:
            raise ValueError("execution plan with error diagnostics cannot have status=ready")
        if self.status == "blocked" and not has_errors:
            raise ValueError("blocked execution plan must contain at least one error diagnostic")
        for scene_id, digest in self.scene_plan_sha256.items():
            if not scene_id or not re.fullmatch(r"^[0-9a-f]{64}$", digest):
                raise ValueError("scene_plan_sha256 must map scene ids to SHA-256 values")
        return self
