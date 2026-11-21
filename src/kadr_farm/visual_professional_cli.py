"""CLI for the professional browser-composited Visual Story V2 renderer."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_settings
from .visual_professional_pipeline_v2 import render_professional_plan
from .visual_story_v2 import VisualStoryPlan


def _load_plan(path: Path) -> VisualStoryPlan:
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        return VisualStoryPlan.model_validate(document)
    except Exception as exc:
        raise RuntimeError(f"Cannot load Visual Story plan: {exc}") from exc


def command_validate(args: argparse.Namespace) -> int:
    plan = _load_plan(Path(args.plan).resolve())
    print("Visual Story V2 contract: OK")
    print(f"Title: {plan.title}")
    print(f"Speech units: {len(plan.speech_units)}")
    print(f"Shots: {len(plan.shots)}")
    for shot in plan.shots:
        print(f"  {shot.shot_id}: {len(shot.objects)} objects, {len(shot.events)} events")
    return 0


def command_render(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan).resolve()
    plan = _load_plan(plan_path)
    settings = load_settings()
    result = render_professional_plan(
        plan,
        plan_path=plan_path,
        output_path=Path(args.out),
        settings=settings,
        work_dir=args.work_dir,
        asset_cache=args.asset_cache,
        clean_work=args.clean_work,
        crf=args.crf,
    )
    print("\nProfessional Visual Story rendering complete")
    for key in ("video", "duration_sec", "expected_narration_sec", "shot_count", "object_count", "event_count", "contact_sheet", "audit", "work_dir"):
        print(f"{key}: {result.get(key)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kadr-farm-visual-pro",
        description="Professional layered HTML/SVG renderer for KadrFarm Visual Story V2",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="Validate a measured plan")
    validate.add_argument("plan")
    validate.set_defaults(func=command_validate)
    render = sub.add_parser("render", help="Render layered shots and assemble MP4")
    render.add_argument("plan")
    render.add_argument("--out", default="output/visual_v2_professional/video.mp4")
    render.add_argument("--work-dir", default=None)
    render.add_argument("--asset-cache", default="data/cache/visual_v2_professional")
    render.add_argument("--clean-work", action="store_true")
    render.add_argument("--crf", type=int, default=18)
    render.set_defaults(func=command_render)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        code = args.func(args)
    except KeyboardInterrupt:
        print("Stopped by user")
        code = 130
    except Exception as exc:
        print(f"VISUAL PRO ERROR: {exc}")
        raise
    raise SystemExit(code)


if __name__ == "__main__":
    main()
