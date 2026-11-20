"""Audio measurement and FFmpeg export for Visual Story V2."""

from __future__ import annotations

import hashlib
import json
import subprocess
import wave

from pathlib import Path
from typing import Any

from .visual_story_v2 import VisualStoryPlan


class ExecutionError(RuntimeError):
    pass


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()

    with Path(path).open("rb") as stream:
        for chunk in iter(
            lambda: stream.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def resolve_wav_path(
    raw_path: str,
    base_dir: str | Path,
) -> Path:
    path = Path(raw_path)

    if not path.is_absolute():
        path = Path(base_dir) / path

    path = path.resolve()

    if not path.is_file():
        raise FileNotFoundError(
            f"Speech WAV not found: {path}"
        )

    return path


def measure_speech_units(
    plan: VisualStoryPlan,
    base_dir: str | Path,
) -> tuple[dict[str, float], dict[str, Path], dict[str, str]]:
    """Read actual WAV durations and verify optional hashes."""

    durations: dict[str, float] = {}
    paths: dict[str, Path] = {}
    hashes: dict[str, str] = {}

    for unit in plan.speech_units:
        if not unit.wav_path:
            raise ExecutionError(
                f"{unit.unit_id}: wav_path is missing. "
                "Run the Kokoro TTS command first."
            )

        path = resolve_wav_path(
            unit.wav_path,
            base_dir,
        )

        try:
            with wave.open(str(path), "rb") as wav:
                frames = wav.getnframes()
                rate = wav.getframerate()

                if frames <= 0 or rate <= 0:
                    raise ExecutionError(
                        f"{unit.unit_id}: empty or invalid WAV"
                    )

                if wav.getcomptype() != "NONE":
                    raise ExecutionError(
                        f"{unit.unit_id}: only uncompressed PCM WAV is supported"
                    )

                duration = frames / rate

        except (wave.Error, OSError) as exc:
            raise ExecutionError(
                f"{unit.unit_id}: invalid WAV: {path}"
            ) from exc

        digest = sha256_file(path)

        if unit.sha256 and digest.lower() != unit.sha256.lower():
            raise ExecutionError(
                f"{unit.unit_id}: SHA-256 mismatch"
            )

        durations[unit.unit_id] = duration
        paths[unit.unit_id] = path
        hashes[unit.unit_id] = digest

    return durations, paths, hashes


def concatenate_wavs(
    wav_paths: list[Path],
    output_path: str | Path,
) -> Path:
    """Concatenate PCM WAV units without re-encoding."""

    if not wav_paths:
        raise ExecutionError("No WAV files to concatenate")

    output_path = Path(output_path)
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    params = None

    with wave.open(str(wav_paths[0]), "rb") as first:
        params = first.getparams()

    for path in wav_paths:
        with wave.open(str(path), "rb") as source:
            current = source.getparams()

            if (
                current.nchannels != params.nchannels
                or current.sampwidth != params.sampwidth
                or current.framerate != params.framerate
                or current.comptype != params.comptype
            ):
                raise ExecutionError(
                    "All WAV units must have identical PCM format "
                    "(channels, sample width and sample rate)"
                )

    with wave.open(str(output_path), "wb") as target:
        target.setnchannels(params.nchannels)
        target.setsampwidth(params.sampwidth)
        target.setframerate(params.framerate)

        for path in wav_paths:
            with wave.open(str(path), "rb") as source:
                while True:
                    data = source.readframes(65536)

                    if not data:
                        break

                    target.writeframesraw(data)

    return output_path


def _run(command: list[str]):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise ExecutionError(
            "Command failed:\n"
            + " ".join(command[:5])
            + "\n"
            + result.stderr[-5000:]
        )


def export_mp4(
    frames_dir: str | Path,
    frame_count: int,
    audio_path: str | Path,
    output_path: str | Path,
    *,
    fps: int,
    ffmpeg: str = "ffmpeg",
    crf: int = 19,
):
    """Build H.264/AAC MP4 from numbered frames and master audio."""

    if frame_count <= 0:
        raise ExecutionError("frame_count must be positive")

    frames_dir = Path(frames_dir).resolve()
    audio_path = Path(audio_path).resolve()
    output_path = Path(output_path).resolve()

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    pattern = frames_dir / "frame_%08d.png"

    expected_first = frames_dir / "frame_00000000.png"

    if not expected_first.is_file():
        raise ExecutionError(
            f"First frame missing: {expected_first}"
        )

    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-framerate",
        str(fps),
        "-start_number",
        "0",
        "-i",
        str(pattern),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-frames:v",
        str(frame_count),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        str(crf),
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "44100",
        "-movflags",
        "+faststart",
        "-shortest",
        str(output_path),
    ]

    _run(command)

    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise ExecutionError("FFmpeg did not produce an MP4")

    return output_path


def probe_video(
    video_path: str | Path,
    ffprobe: str = "ffprobe",
) -> dict[str, Any]:
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration,size:stream=codec_type,codec_name,width,height",
        "-of",
        "json",
        str(video_path),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise ExecutionError(
            "ffprobe failed: " + result.stderr[-3000:]
        )

    return json.loads(result.stdout)