"""Compile VisualStoryPlan into measured shot and event schedules."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .visual_story_v2 import (
    VisualEvent,
    VisualStoryPlan,
)


@dataclass(frozen=True)
class ScheduledEvent:
    event: VisualEvent
    offset_sec: float


@dataclass(frozen=True)
class ShotJob:
    shot_id: str

    start_sec: float
    end_sec: float
    duration_sec: float

    frame_start: int
    frame_end: int

    speech_unit_ids: tuple[str, ...]

    events: tuple[ScheduledEvent, ...]


def compile_plan(
    plan: VisualStoryPlan,
    measured_durations: dict[str, float],
) -> list[ShotJob]:
    """Build deterministic timeline from measured WAV durations."""

    expected_ids = [
        unit.unit_id
        for unit in plan.speech_units
    ]

    if set(measured_durations) != set(expected_ids):
        raise ValueError(
            "Measured WAV durations do not match the speech unit registry"
        )

    for unit_id, duration in measured_durations.items():
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or float(duration) <= 0
        ):
            raise ValueError(
                f"{unit_id}: measured duration must be a finite positive number"
            )

    # Absolute timeline for each separately synthesized speech unit.
    unit_timeline: dict[str, tuple[float, float]] = {}

    cursor = 0.0

    for unit in plan.speech_units:
        duration = float(measured_durations[unit.unit_id])

        start = cursor
        end = start + duration

        unit_timeline[unit.unit_id] = (start, end)

        cursor = end

    jobs: list[ShotJob] = []

    for shot in plan.shots:
        first_id = shot.speech_unit_ids[0]
        last_id = shot.speech_unit_ids[-1]

        start = unit_timeline[first_id][0]
        end = unit_timeline[last_id][1]

        # Video segments begin at globally rounded frame boundaries.
        frame_start = round(start * plan.fps)
        frame_end = round(end * plan.fps)

        if frame_end <= frame_start:
            raise ValueError(
                f"{shot.shot_id}: measured duration {end - start:.6f}s "
                f"produces zero video frames at {plan.fps} fps"
            )

        video_start_sec = frame_start / plan.fps

        scheduled_events: list[ScheduledEvent] = []

        for event in shot.events:
            unit_start, unit_end = unit_timeline[event.anchor.unit_id]

            if event.anchor.offset_sec > unit_end - unit_start:
                raise ValueError(
                    f"{shot.shot_id}/{event.event_id}: anchor exceeds "
                    f"measured WAV duration"
                )

            absolute_event_time = (
                unit_start + event.anchor.offset_sec
            )

            if not start <= absolute_event_time <= end:
                raise ValueError(
                    f"{shot.shot_id}/{event.event_id}: event outside shot"
                )

            scheduled_events.append(
                ScheduledEvent(
                    event=event,
                    offset_sec=max(0.0, absolute_event_time - video_start_sec),
                )
            )

        scheduled_events.sort(
            key=lambda item: item.offset_sec
        )

        jobs.append(
            ShotJob(
                shot_id=shot.shot_id,
                start_sec=start,
                end_sec=end,
                duration_sec=end - start,
                frame_start=frame_start,
                frame_end=frame_end,
                speech_unit_ids=tuple(shot.speech_unit_ids),
                events=tuple(scheduled_events),
            )
        )

    return jobs