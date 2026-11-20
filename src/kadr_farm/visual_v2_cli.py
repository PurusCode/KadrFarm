"""Standalone CLI for KadrFarm Visual Story V2."""

from __future__ import annotations

import argparse
import json
import math
import shutil

from pathlib import Path

from .config import load_settings
from .visual_story_v2 import VisualStoryPlan
from .visual_scene_compiler_v2 import compile_plan
from .visual_asset_resolver_v2 import VisualAssetResolver
from .visual_renderer_v2 import SceneRenderer
from .visual_animation_v2 import AnimationEngine
from .visual_execution_v2 import (
    measure_speech_units,
    concatenate_wavs,
    export_mp4,
    probe_video,
    sha256_file,
)
from .visual_audit_v2 import (
    create_contact_sheet,
    write_audit,
)


def _load_plan(path: Path) -> VisualStoryPlan:
    try:
        data = json.loads(
            path.read_text(encoding="utf-8")
        )

        return VisualStoryPlan.model_validate(data)

    except Exception as exc:
        raise RuntimeError(
            f"Unable to load Visual Story V2 plan: {exc}"
        ) from exc


def command_validate(args) -> int:
    plan_path = Path(args.plan).resolve()
    plan = _load_plan(plan_path)

    print("Visual Story V2 contract: OK")
    print(f"Title: {plan.title}")
    print(f"Speech units: {len(plan.speech_units)}")
    print(f"Shots: {len(plan.shots)}")

    for shot in plan.shots:
        print(
            f"  {shot.shot_id}: "
            f"{len(shot.objects)} objects, "
            f"{len(shot.events)} events"
        )

    return 0


def command_tts(args) -> int:
    """Synthesize each speech unit into a separate measured WAV."""

    from .kokoro_engine import synthesize_kokoro
    from .text_normalization import normalize_for_speech

    plan_path = Path(args.plan).resolve()
    plan = _load_plan(plan_path)

    settings = load_settings()

    if settings.tts_engine != "kokoro":
        raise RuntimeError(
            "Visual V2 currently requires the configured Kokoro engine"
        )

    if settings.tts_speaker != "dima":
        raise RuntimeError(
            "This project configuration is pinned to Kokoro speaker Dima"
        )

    output_dir = Path(args.out_dir).resolve()
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    prepared = []

    for unit in plan.speech_units:
        normalized = normalize_for_speech(unit.text)

        if not normalized:
            raise RuntimeError(
                f"{unit.unit_id}: empty text after speech normalization"
            )

        prepared.append(
            (
                unit.unit_id,
                {
                    "heading": unit.unit_id,
                },
                unit.text,
                normalized,
            )
        )

    manifest = synthesize_kokoro(
        settings,
        prepared,
        output_dir,
        speed=args.speed,
    )

    if manifest.get("status") != "complete":
        raise RuntimeError(
            "Kokoro did not complete all speech units"
        )

    blocks = {
        row["id"]: row
        for row in manifest.get("blocks", [])
    }

    updated_units = []

    for unit in plan.speech_units:
        row = blocks.get(unit.unit_id)

        if not row:
            raise RuntimeError(
                f"{unit.unit_id}: WAV missing from Kokoro manifest"
            )

        wav_path = (
            output_dir / row["file"]
        ).resolve()

        if not wav_path.is_file():
            raise RuntimeError(
                f"{unit.unit_id}: expected WAV not found: {wav_path}"
            )

        updated_units.append(
            unit.model_copy(
                update={
                    "wav_path": str(wav_path),
                    "sha256": sha256_file(wav_path),
                }
            )
        )

    measured_plan = plan.model_copy(
        update={
            "speech_units": updated_units,
        }
    )

    out_plan = Path(args.out_plan).resolve()
    out_plan.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    out_plan.write_text(
        json.dumps(
            measured_plan.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("Kokoro synthesis complete")
    print(f"WAV files: {len(updated_units)}")
    print(f"Measured plan: {out_plan}")

    return 0


def command_render(args) -> int:
    plan_path = Path(args.plan).resolve()
    plan = _load_plan(plan_path)

    settings = load_settings()

    output_path = Path(args.out).resolve()
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    work_dir = (
        Path(args.work_dir).resolve()
        if args.work_dir
        else output_path.parent / (
            output_path.stem + "_visual_v2_work"
        )
    )

    frames_dir = work_dir / "frames"
    audio_dir = work_dir / "audio"

    if args.clean_work and work_dir.exists():
        shutil.rmtree(work_dir)

    frames_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    audio_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # 1. Measure all real WAV files.
    durations, wav_paths, hashes = measure_speech_units(
        plan,
        plan_path.parent,
    )

    # 2. Compile the absolute voice timeline.
    jobs = compile_plan(
        plan,
        durations,
    )

    total_frames = max(
        job.frame_end
        for job in jobs
    )

    if total_frames <= 0:
        raise RuntimeError("Compiled timeline contains no video frames")

    # 3. Prepare resolver and rendering engines.
    resolver = VisualAssetResolver(
        settings,
        cache_dir=args.asset_cache,
        base_dir=plan_path.parent,
    )

    renderer = SceneRenderer(
        width=plan.width,
        height=plan.height,
        background=plan.background,
    )

    animation = AnimationEngine(
        renderer,
        resolver,
        fps=plan.fps,
    )

    shots_by_id = {
        shot.shot_id: shot
        for shot in plan.shots
    }

    # 4. Render all shots into one numbered frame sequence.
    for job in jobs:
        shot = shots_by_id[job.shot_id]

        print(
            f"[Visual V2] {shot.shot_id}: "
            f"{job.duration_sec:.2f}s, "
            f"{len(shot.objects)} objects, "
            f"{len(shot.events)} events"
        )

        animation.render_shot(
            plan,
            shot,
            job,
            frames_dir,
        )

    # 5. Verify that all frame numbers exist.
    missing = []

    for index in range(total_frames):
        frame = frames_dir / f"frame_{index:08d}.png"

        if not frame.is_file():
            missing.append(index)

            if len(missing) >= 10:
                break

    if missing:
        raise RuntimeError(
            "Rendered frame sequence has gaps: "
            + ", ".join(map(str, missing))
        )

    # 6. Concatenate the separate speech WAV units.
    ordered_wavs = [
        wav_paths[unit.unit_id]
        for unit in plan.speech_units
    ]

    master_audio = concatenate_wavs(
        ordered_wavs,
        audio_dir / "narration.wav",
    )

    # 7. Create MP4.
    export_mp4(
        frames_dir,
        total_frames,
        master_audio,
        output_path,
        fps=plan.fps,
        ffmpeg=settings.ffmpeg,
    )

    # 8. Probe actual result.
    probe = probe_video(
        output_path,
        ffprobe=settings.ffprobe,
    )

    # 9. Contact sheet: choose up to 24 evenly spaced frames.
    sample_count = min(
        24,
        total_frames,
    )

    sample_indices = sorted(
        {
            round(
                index * (total_frames - 1)
                / max(1, sample_count - 1)
            )
            for index in range(sample_count)
        }
    )

    sample_paths = [
        frames_dir / f"frame_{index:08d}.png"
        for index in sample_indices
    ]

    create_contact_sheet(
        sample_paths,
        output_path.with_suffix(".contact.jpg"),
    )

    # 10. Audit.
    write_audit(
        plan,
        jobs,
        durations,
        hashes,
        output_path.with_suffix(".audit.json"),
        video_probe=probe,
    )

    print()
    print("Visual Story V2 rendering complete")
    print(f"Video: {output_path}")
    print(f"Duration: {probe.get('format', {}).get('duration')} sec")
    print(f"Frames: {total_frames}")
    print(f"Contact sheet: {output_path.with_suffix('.contact.jpg')}")
    print(f"Audit: {output_path.with_suffix('.audit.json')}")

    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="kadrfarm-visual-v2",
        description="KadrFarm independent Visual Story V2 pipeline",
    )

    sub = parser.add_subparsers(
        dest="command",
        required=True,
    )

    validate = sub.add_parser(
        "validate",
        help="Validate a Visual Story V2 JSON plan",
    )

    validate.add_argument(
        "plan",
    )

    validate.set_defaults(
        func=command_validate,
    )

    tts = sub.add_parser(
        "tts",
        help="Create separate Kokoro WAV files for each speech unit",
    )

    tts.add_argument(
        "plan",
    )

    tts.add_argument(
        "--out-dir",
        default="output/visual_v2/audio_units",
    )

    tts.add_argument(
        "--out-plan",
        default="output/visual_v2/plan_measured.json",
    )

    tts.add_argument(
        "--speed",
        type=float,
        default=0.9,
    )

    tts.set_defaults(
        func=command_tts,
    )

    render = sub.add_parser(
        "render",
        help="Render animations and assemble final MP4",
    )

    render.add_argument(
        "plan",
    )

    render.add_argument(
        "--out",
        default="output/visual_v2/video.mp4",
    )

    render.add_argument(
        "--work-dir",
        default=None,
    )

    render.add_argument(
        "--asset-cache",
        default="data/cache/visual_v2",
    )

    render.add_argument(
        "--clean-work",
        action="store_true",
    )

    render.set_defaults(
        func=command_render,
    )

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        code = args.func(args)

    except KeyboardInterrupt:
        print("Stopped by user.")
        code = 130

    except Exception as exc:
        print(f"VISUAL V2 ERROR: {exc}")
        raise

    raise SystemExit(code)


if __name__ == "__main__":
    main()