"""Speech-synchronized animation engine for Visual Story V2."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .visual_renderer_v2 import SceneRenderer
from .visual_scene_compiler_v2 import ShotJob
from .visual_story_v2 import Shot, VisualStoryPlan
from .visual_asset_resolver_v2 import VisualAssetResolver


def _clamp(value: float) -> float:
    return max(
        0.0,
        min(1.0, value),
    )


class AnimationEngine:
    def __init__(
        self,
        renderer: SceneRenderer,
        asset_resolver: VisualAssetResolver,
        fps: int = 25,
    ):
        self.renderer = renderer
        self.asset_resolver = asset_resolver
        self.fps = fps

    @staticmethod
    def _initial_states(shot: Shot) -> dict[str, dict[str, Any]]:
        states = {}

        for obj in shot.objects:
            states[obj.object_id] = {
                "visible": obj.initial_state == "visible",
                "opacity": 1.0,
                "dx": 0.0,
                "dy": 0.0,
                "scale": 1.0,
                "highlighted": False,
                "draw_progress": 0.0,
                "revealed_rows": 0,
            }

        return states

    def _apply_events(
        self,
        shot: Shot,
        job: ShotJob,
        elapsed_sec: float,
    ) -> dict[str, dict[str, Any]]:
        states = self._initial_states(shot)

        for scheduled in job.events:
            event = scheduled.event

            event_start = scheduled.offset_sec
            relative = elapsed_sec - event_start

            if relative < 0:
                continue

            state = states[event.target_id]

            duration = max(
                0.001,
                event.duration_sec,
            )

            progress = _clamp(
                relative / duration
            )

            params = event.params

            if event.action == "appear":
                state["visible"] = True
                state["opacity"] = 1.0

            elif event.action == "fade_in":
                state["visible"] = True
                state["opacity"] = progress

            elif event.action == "fade_out":
                state["visible"] = True
                state["opacity"] = 1.0 - progress

                if progress >= 1.0:
                    state["visible"] = False

            elif event.action == "highlight":
                state["highlighted"] = relative <= event.duration_sec

                state["highlight_color"] = params.get(
                    "color",
                    "#F4B860",
                )

            elif event.action == "move":
                state["visible"] = True

                target_x = float(
                    params.get("x", 0.5)
                )

                target_y = float(
                    params.get("y", 0.5)
                )

                obj = next(
                    item
                    for item in shot.objects
                    if item.object_id == event.target_id
                )

                state["dx"] = (
                    target_x - obj.layer.x
                ) * progress

                state["dy"] = (
                    target_y - obj.layer.y
                ) * progress

            elif event.action == "scale":
                state["visible"] = True

                target_scale = float(
                    params.get("factor", 1.0)
                )

                state["scale"] = (
                    1.0
                    + (target_scale - 1.0) * progress
                )

            elif event.action == "draw":
                state["visible"] = True
                state["draw_progress"] = progress

            elif event.action == "reveal_row":
                state["visible"] = True

                state["revealed_rows"] = max(
                    int(state["revealed_rows"]),
                    int(params["row_index"]) + 1,
                )

            elif event.action == "count_up":
                state["visible"] = True

                start_value = float(params["from"])
                end_value = float(params["to"])

                value = (
                    start_value
                    + (end_value - start_value) * progress
                )

                state["value"] = round(
                    value,
                    int(params.get("decimals", 0)),
                )

            elif event.action == "update_text":
                state["visible"] = True
                state["text"] = params["text"]

        return states

    def render_shot(
        self,
        plan: VisualStoryPlan,
        shot: Shot,
        job: ShotJob,
        output_dir: str | Path,
    ) -> list[Path]:
        """Render one shot to a numbered sequence of PNG frames."""

        output_dir = Path(output_dir)
        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        assets = self.asset_resolver.prepare_shot(
            shot,
            width=plan.width,
            height=plan.height,
            fps=plan.fps,
            duration_sec=job.duration_sec,
        )

        frame_count = max(
            1,
            job.frame_end - job.frame_start,
        )

        output_paths = []

        for local_index in range(frame_count):
            elapsed = local_index / plan.fps

            states = self._apply_events(
                shot,
                job,
                elapsed,
            )

            frame = self.renderer.render(
                shot.objects,
                assets=assets,
                states=states,
                frame_index=local_index,
            )

            frame_number = (
                job.frame_start + local_index
            )

            frame_path = (
                output_dir
                / f"frame_{frame_number:08d}.png"
            )

            frame.save(
                frame_path,
                format="PNG",
                optimize=False,
            )

            output_paths.append(frame_path)

        return output_paths