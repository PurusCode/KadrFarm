"""FFmpeg assembler driven by measured per-scene WAV durations and visual beats."""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Any

from .common import read_json, write_json


def _run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if result.returncode:
        raise RuntimeError(
            f"FFmpeg завершился с кодом {result.returncode}:\n{result.stderr[-5000:]}"
        )


def _quote_concat(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")


def _probe(ffprobe: str, path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "format=duration,size:stream=codec_type,codec_name,width,height,sample_rate,channels,duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    return json.loads(result.stdout)


def _render_visual_segment(settings: Any, asset: dict[str, Any], duration: float,
                           output: Path, *, transition_in: str = "cut",
                           transition_out: str = "cut", camera_move: str = "static") -> None:
    source = Path(asset["path"])
    if not source.is_file():
        raise FileNotFoundError(f"Визуальный материал отсутствует: {source}")
    width, height, fps = int(settings.width), int(settings.height), int(settings.fps)
    if asset.get("media_type") == "video":
        input_args = ["-stream_loop", "-1", "-i", str(source)]
    else:
        input_args = ["-loop", "1", "-framerate", str(fps), "-i", str(source)]
    filters = [f"scale={width}:{height}:force_original_aspect_ratio=increase",
               f"crop={width}:{height}"]
    if asset.get("media_type") != "video" and camera_move in ("slow_zoom", "zoom_in", "zoom_out", "ken_burns", "slow_pan"):
        frames = max(1, int(math.ceil(duration * fps)))
        if camera_move == "slow_pan":
            x_expr = f"(iw-iw/zoom)*min(on/{frames},1)"
            zoom_expr = "1.08"
        elif camera_move == "zoom_out":
            x_expr = "iw/2-(iw/zoom/2)"
            zoom_expr = "max(1.0,1.08-on*0.0005)"
        else:
            x_expr = "iw/2-(iw/zoom/2)"
            zoom_expr = "min(zoom+0.0005,1.08)"
        filters.append(
            f"zoompan=z='{zoom_expr}':x='{x_expr}':y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={fps}"
        )
    filters.extend([f"fps={fps}", "setsar=1", "format=yuv420p"])
    if transition_in == "fade":
        filters.append("fade=t=in:st=0:d=0.25")
    if transition_out == "fade":
        filters.append(f"fade=t=out:st={max(0.0, duration-0.25):.3f}:d=0.25")
    command = [settings.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               *input_args, "-vf", ",".join(filters), "-t", f"{duration:.3f}",
               "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-pix_fmt", "yuv420p", "-r", str(fps), str(output)]
    _run(command)


def assemble_v2(settings: Any, storyboard_path: Path, output_path: Path,
                work_dir: Path | None = None) -> dict[str, Any]:
    storyboard = read_json(storyboard_path)
    scenes = storyboard.get("scenes", [])
    if not scenes:
        raise ValueError("Нельзя собрать видео без сцен")
    work = work_dir or output_path.parent / "_assemble_v2"
    work.mkdir(parents=True, exist_ok=True)
    scene_files: list[Path] = []
    unsupported_effects: list[str] = []
    expected_start = 0.0

    for scene_index, scene in enumerate(scenes, 1):
        sid = scene["id"]
        duration = float(scene.get("duration_sec") or 0)
        if duration <= 0:
            raise ValueError(f"{sid}: отсутствует фактическая duration_sec после TTS")
        if abs(float(scene.get("start_sec", -999)) - expected_start) > 0.05:
            raise ValueError(f"{sid}: временная шкала содержит разрыв/перекрытие")
        expected_start += duration
        audio_ref = (scene.get("media_refs") or {}).get("audio") or {}
        audio_path = Path(audio_ref.get("path") or "")
        if not audio_path.is_file():
            raise FileNotFoundError(f"{sid}: отсутствует WAV сцены: {audio_path}")
        visuals = (scene.get("media_refs") or {}).get("visuals") or []
        for transition in (scene.get("transition_in", "cut"), scene.get("transition_out", "cut")):
            if transition not in ("cut", "fade"):
                unsupported_effects.append(f"{sid}: переход {transition} пока собран как hard cut")
        if not visuals:
            raise ValueError(f"{sid}: отсутствует визуальный материал")

        ratios = [float(v.get("duration_ratio", 1.0)) for v in visuals]
        total_ratio = sum(ratios)
        if abs(total_ratio - 1.0) > 0.02:
            raise ValueError(f"{sid}: доли визуальных планов должны суммироваться в 1.0")
        beat_files = []
        elapsed = 0.0
        for beat_index, (asset, ratio) in enumerate(zip(visuals, ratios), 1):
            beat_duration = duration - elapsed if beat_index == len(visuals) else duration * ratio
            elapsed += beat_duration
            segment = work / f"{scene_index:03d}_{beat_index:02d}_visual.mp4"
            _render_visual_segment(
                settings, asset, beat_duration, segment,
                transition_in=scene.get("transition_in", "cut") if beat_index == 1 else "cut",
                transition_out=scene.get("transition_out", "cut") if beat_index == len(visuals) else "cut",
                camera_move=scene.get("camera_move", "static"),
            )
            beat_files.append(segment)

        concat_file = work / f"{scene_index:03d}_visuals.ffconcat"
        concat_file.write_text(
            "ffconcat version 1.0\n" + "".join(f"file '{_quote_concat(p)}'\n" for p in beat_files),
            encoding="utf-8",
        )
        silent_scene = work / f"{scene_index:03d}_silent.mp4"
        _run([settings.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
              "-f", "concat", "-safe", "0", "-i", str(concat_file),
              "-an", "-c", "copy", str(silent_scene)])
        muxed_scene = work / f"{scene_index:03d}_scene.mp4"
        _run([settings.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
              "-i", str(silent_scene), "-i", str(audio_path),
              "-map", "0:v:0", "-map", "1:a:0", "-t", f"{duration:.3f}",
              "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
              "-ac", "2", "-movflags", "+faststart", str(muxed_scene)])
        scene_files.append(muxed_scene)

    concat_file = work / "scenes.ffconcat"
    concat_file.write_text(
        "ffconcat version 1.0\n" + "".join(f"file '{_quote_concat(p)}'\n" for p in scene_files),
        encoding="utf-8",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _run([settings.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
          "-f", "concat", "-safe", "0", "-i", str(concat_file),
          "-c", "copy", "-movflags", "+faststart", str(output_path)])
    probe = _probe(settings.ffprobe, output_path)
    actual_duration = float(probe.get("format", {}).get("duration") or 0)
    expected_duration = sum(float(s["duration_sec"]) for s in scenes)
    if actual_duration <= 0:
        raise RuntimeError("ffprobe не смог определить длительность итогового MP4")
    if abs(actual_duration - expected_duration) > max(1.5, len(scenes) * 0.08):
        raise RuntimeError(
            f"Длительность MP4 ({actual_duration:.2f}s) расходится с суммой WAV "
            f"({expected_duration:.2f}s)"
        )
    report = {
        "status": "complete", "video": str(output_path.resolve()),
        "duration_seconds": round(actual_duration, 3),
        "expected_audio_seconds": round(expected_duration, 3),
        "scene_count": len(scenes), "ffprobe": probe,
        "warnings": unsupported_effects,
    }
    write_json(output_path.with_suffix(".assemble.json"), report)
    return report
