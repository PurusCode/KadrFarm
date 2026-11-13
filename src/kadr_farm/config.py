from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv
from .common import ROOT, ensure_dir


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 120.0


@dataclass
class Settings:
    root: Path
    data_dir: Path
    output_dir: Path
    cache_dir: Path
    broll_dir: Path
    images_dir: Path
    audio_dir: Path
    ffmpeg: str
    ffprobe: str
    width: int
    height: int
    fps: int
    bitrate: str
    gop: int
    youtube_api_key: str
    pexels_api_key: str
    pixabay_api_key: str
    tts_engine: str
    tts_language: str
    tts_speaker: str
    tts_speed: float
    providers: list[ProviderConfig]


def load_settings() -> Settings:
    env_file = ROOT / ".env"
    load_dotenv(env_file, override=False)
    data = ROOT / "data"
    cache = data / "cache"
    # Must be configured before importing torch, otherwise Windows users may cache to Cyrillic profile paths.
    os.environ.setdefault("TORCH_HOME", str(cache / "torch"))
    os.environ.setdefault("HF_HOME", str(cache / "huggingface"))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache / "xdg"))
    for directory in (data, cache, data / "broll", data / "images", data / "audio", ROOT / "output"):
        ensure_dir(directory)
    providers: list[ProviderConfig] = []
    for label in ("PRIMARY", "SECONDARY"):
        base = os.getenv(f"LLM_{label}_BASE_URL", "").strip()
        model = os.getenv(f"LLM_{label}_MODEL", "").strip()
        key = os.getenv(f"LLM_{label}_API_KEY", "").strip()
        if base and model and key:
            default_timeout = 120.0 if label == "PRIMARY" else 900.0
            raw_timeout = os.getenv(
                f"LLM_{label}_TIMEOUT_SECONDS", str(default_timeout)
            ).strip()
            try:
                timeout_seconds = float(raw_timeout)
            except ValueError as exc:
                raise ValueError(
                    f"LLM_{label}_TIMEOUT_SECONDS должен быть числом секунд, "
                    f"получено: {raw_timeout!r}"
                ) from exc
            if not 10.0 <= timeout_seconds <= 3600.0:
                raise ValueError(
                    f"LLM_{label}_TIMEOUT_SECONDS должен быть в диапазоне 10..3600, "
                    f"получено: {timeout_seconds}"
                )
            providers.append(ProviderConfig(
                label.lower(), base, key, model, timeout_seconds=timeout_seconds
            ))
    return Settings(
        root=ROOT, data_dir=data, output_dir=ROOT / "output", cache_dir=cache,
        broll_dir=data / "broll", images_dir=data / "images", audio_dir=data / "audio",
        ffmpeg=os.getenv("FFMPEG_PATH", "ffmpeg"), ffprobe=os.getenv("FFPROBE_PATH", "ffprobe"),
        width=int(os.getenv("VIDEO_WIDTH", "1920")), height=int(os.getenv("VIDEO_HEIGHT", "1080")),
        fps=int(os.getenv("VIDEO_FPS", "25")), bitrate=os.getenv("VIDEO_BITRATE", "4500k"),
        gop=int(os.getenv("VIDEO_GOP", "50")), youtube_api_key=os.getenv("YOUTUBE_API_KEY", "").strip(),
        pexels_api_key=os.getenv("PEXELS_API_KEY", "").strip(),
        pixabay_api_key=os.getenv("PIXABAY_API_KEY", "").strip(),
        tts_engine=os.getenv("TTS_ENGINE", "kokoro").strip().lower(),
        tts_language=os.getenv("TTS_LANGUAGE", "ru").strip(),
        tts_speaker=os.getenv("TTS_SPEAKER", "dima").strip().lower(),
        tts_speed=float(os.getenv("TTS_SPEED", "0.9")),
        providers=providers,
    )
