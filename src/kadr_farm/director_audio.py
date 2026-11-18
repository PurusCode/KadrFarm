"""Narration concatenation + sound design.

Музыка и SFX берутся из локальных CC0-паков (KADR_MUSIC_DIR / KADR_SFX_DIR).
Музыка автоматически приглушается (ducking) под речь. Если паков нет —
процедурный fallback, как раньше. Контракт возврата сохранён.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any

import numpy as np

SAMPLE_RATE = 24_000
_AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".m4a"}
log = logging.getLogger("kadr_farm.director_audio")


def _contains_audio_files(directory: Path) -> bool:
    return directory.is_dir() and any(
        item.is_file() and item.suffix.lower() in _AUDIO_EXTENSIONS
        for item in directory.iterdir()
    )


def _resolve_audio_pack_dir(env_name: str, root: Path, default_relative: str) -> Path:
    """Resolve a configured pack; stale paths fall back to the project-local pack."""
    root = Path(root).resolve()
    default_dir = (root / default_relative).resolve()
    raw = os.getenv(env_name, "").strip()
    if raw:
        configured = Path(os.path.expanduser(os.path.expandvars(raw)))
        if not configured.is_absolute():
            configured = root / configured
        configured = configured.resolve()
        if _contains_audio_files(configured):
            return configured
        if configured != default_dir and _contains_audio_files(default_dir):
            log.warning(
                "%s points to a missing/empty directory (%s); using project audio pack (%s)",
                env_name, configured, default_dir,
            )
            return default_dir
        return configured
    return default_dir


def _resolve_audio_cache_dir() -> Path:
    root = Path(__file__).resolve().parents[2]
    raw = os.getenv("KADR_AUDIO_CACHE", "").strip()
    cache = Path(os.path.expanduser(os.path.expandvars(raw))) if raw else Path("data/audio/.cache")
    return (cache if cache.is_absolute() else root / cache).resolve()


def _read_wav(path: Path):
    with wave.open(str(path), "rb") as wf:
        ch, sr, sw = wf.getnchannels(), wf.getframerate(), wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if sw != 2 or ch != 1 or sr != SAMPLE_RATE:
        raise ValueError(f"{path}: expected mono PCM16 {SAMPLE_RATE} Hz, got ch={ch}, sr={sr}, width={sw}")
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _write_wav(path: Path, samples: np.ndarray):
    path.parent.mkdir(parents=True, exist_ok=True)
    values = np.clip(samples, -0.98, 0.98)
    pcm = (values * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


def _load_any_audio(path: Path) -> np.ndarray | None:
    """Читает wav/mp3/ogg/m4a, конвертируя в 24k mono через ffmpeg (с кэшем)."""
    try:
        return _read_wav(path)
    except Exception:
        pass
    cache_root = _resolve_audio_cache_dir()
    cache_root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{path.resolve()}:{path.stat().st_mtime_ns}".encode()).hexdigest()[:16]
    cached = cache_root / f"{key}.wav"
    if not cached.is_file():
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-i", str(path), "-ar", str(SAMPLE_RATE), "-ac", "1",
                 "-c:a", "pcm_s16le", str(cached)],
                check=True, capture_output=True,
            )
        except Exception:
            return None
    try:
        return _read_wav(cached)
    except Exception:
        return None


def _effect(name: str, duration: float = 0.45) -> np.ndarray:
    n = max(1, int(SAMPLE_RATE * duration))
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    key = name.lower()
    if any(x in key for x in ("ding", "success", "chime", "ping")):
        sig = np.sin(2 * np.pi * 880 * t) + 0.55 * np.sin(2 * np.pi * 1320 * t) * np.exp(-t * 2)
        env = np.exp(-t * 4)
    elif any(x in key for x in ("error", "buzzer", "wrong")):
        sig = np.sin(2 * np.pi * 145 * t) + 0.35 * np.sin(2 * np.pi * 190 * t)
        env = np.minimum(1, t * 45) * np.exp(-t * 2)
    elif any(x in key for x in ("whoosh", "shatter", "sweep")):
        seed = int.from_bytes(hashlib.sha256(name.encode()).digest()[:4], "big")
        rng = np.random.default_rng(seed)
        noise = rng.normal(0, 1, n).astype(np.float32)
        smooth = np.convolve(noise, np.ones(35, dtype=np.float32) / 35, mode="same")
        sig = noise * 0.35 + smooth * 1.8
        env = np.sin(np.clip(t / duration, 0, 1) * math.pi) ** 1.5
    elif any(x in key for x in ("typing", "key", "click", "tap", "pop")):
        sig = np.sin(2 * np.pi * (620 + 180 * np.sin(t * 35)) * t)
        env = np.exp(-t * 30)
    else:
        sig = np.sin(2 * np.pi * 520 * t) + 0.25 * np.sin(2 * np.pi * 780 * t)
        env = np.exp(-t * 5.5)
    return (sig * env * 0.20).astype(np.float32)


def _music_pad(duration: float, cue: str) -> np.ndarray:
    n = max(0, int(round(SAMPLE_RATE * duration)))
    if not n:
        return np.zeros(0, dtype=np.float32)
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    text = (cue or "").lower()
    if any(x in text for x in ("ominous", "tense", "mystery", "тревог", "напряж")):
        freqs, volume = (82.4, 123.5, 164.8), 0.025
    elif any(x in text for x in ("upbeat", "lo-fi", "light", "весел", "лёгк")):
        freqs, volume = (146.8, 220.0, 293.7), 0.018
    else:
        freqs, volume = (110.0, 164.8, 220.0), 0.018
    slow = 0.78 + 0.22 * np.sin(2 * np.pi * 0.12 * t)
    pad = sum(np.sin(2 * np.pi * f * t + i * 0.4) / (i + 1) for i, f in enumerate(freqs))
    fade = np.minimum(1.0, np.minimum(t / 0.35, (duration - t) / 0.35)).clip(0, 1)
    return (pad * slow * fade * volume).astype(np.float32)


_SFX_KEYS = {
    "whoosh": ("whoosh", "sweep", "swish"),
    "pop": ("pop", "click", "tap", "key"),
    "ding": ("ding", "chime", "success", "ping"),
    "error": ("error", "buzzer", "wrong"),
}


def _load_sfx_pack(sfx_dir: Path | None) -> dict[str, np.ndarray]:
    pack: dict[str, np.ndarray] = {}
    if not sfx_dir or not sfx_dir.is_dir():
        return pack
    for family, keys in _SFX_KEYS.items():
        for f in sorted(sfx_dir.iterdir()):
            if f.suffix.lower() not in (".wav", ".mp3", ".ogg", ".m4a"):
                continue
            if any(k in f.stem.lower() for k in keys):
                samples = _load_any_audio(f)
                if samples is not None and len(samples) > 240:
                    pack[family] = samples[: int(SAMPLE_RATE * 1.2)]
                break
    return pack


def _sfx_for(name: str, pack: dict[str, np.ndarray]) -> np.ndarray:
    key = str(name).lower()
    for family, keys in _SFX_KEYS.items():
        if any(k in key for k in keys) and family in pack:
            return pack[family]
    return _effect(name)


def _pick_music_track(music_dir: Path | None, cue: str) -> tuple[Path | None, np.ndarray | None]:
    if not music_dir or not music_dir.is_dir():
        return None, None
    tracks = [f for f in sorted(music_dir.iterdir())
              if f.suffix.lower() in (".wav", ".mp3", ".ogg", ".m4a")]
    if not tracks:
        return None, None
    idx = int.from_bytes(hashlib.sha256((cue or "default").encode()).digest()[:4], "big") % len(tracks)
    track = tracks[idx]
    return track, _load_any_audio(track)


def _ducked_music(music: np.ndarray, narration: np.ndarray, total_n: int,
                  level: float, depth: float) -> np.ndarray:
    if len(music) == 0:
        return np.zeros(total_n, dtype=np.float32)
    reps = total_n // len(music) + 1
    bed = np.tile(music, reps)[:total_n].astype(np.float32)
    # Огибающая речи -> приглушение музыки там, где говорит диктор.
    win = max(1, int(0.25 * SAMPLE_RATE))
    env = np.convolve(np.abs(narration), np.ones(win, dtype=np.float32) / win, mode="same")
    gate = np.clip(env * 6.0, 0.0, 1.0)
    gain = level * (1.0 - depth * gate)
    t = np.arange(total_n, dtype=np.float32) / SAMPLE_RATE
    fade = np.minimum(1.0, np.minimum(t / 0.6, (total_n / SAMPLE_RATE - t) / 0.8)).clip(0, 1)
    return (bed * gain * fade).astype(np.float32)


def build_master_audio(
    beats: list[dict[str, Any]],
    output_path: Path,
    *,
    pause_seconds: float = 0.22,
    add_procedural_bed: bool = False,
) -> dict[str, Any]:
    """Concatenate exact beat WAVs; beat boundaries are where visuals change."""
    if not beats:
        raise ValueError("Нет аудиобитов")

    root = Path(__file__).resolve().parents[2]
    music_dir = _resolve_audio_pack_dir("KADR_MUSIC_DIR", root, "data/audio/music")
    sfx_dir = _resolve_audio_pack_dir("KADR_SFX_DIR", root, "data/audio/sfx")
    level = float(os.getenv("KADR_MUSIC_LEVEL", "0.16"))
    depth = float(os.getenv("KADR_DUCK_DEPTH", "0.75"))

    narration_parts: list[np.ndarray] = []
    timeline: list[dict[str, Any]] = []
    cursor = 0.0
    for index, beat in enumerate(beats):
        samples = _read_wav(Path(beat["audio_path"]))
        duration = len(samples) / SAMPLE_RATE
        gap = pause_seconds if index < len(beats) - 1 else 0.0
        narration_parts.append(samples)
        if gap:
            narration_parts.append(np.zeros(int(SAMPLE_RATE * gap), dtype=np.float32))
        timeline.append({
            "scene_id": beat["scene_id"], "beat_id": beat["beat_id"],
            "start_sec": round(cursor, 4), "voice_end_sec": round(cursor + duration, 4),
            "end_sec": round(cursor + duration + gap, 4),
            "voice_duration_sec": round(duration, 4), "gap_after_sec": gap,
        })
        cursor += duration + gap

    narration = np.concatenate(narration_parts).astype(np.float32)
    mixed = narration.copy()
    total_n = len(mixed)

    track_path, music = _pick_music_track(music_dir, beats[0].get("music_cue", ""))
    used_procedural = False
    if music is not None:
        mixed += _ducked_music(music, narration, total_n, level, depth)
    elif add_procedural_bed:
        used_procedural = True
        for beat, slot in zip(beats, timeline):
            start = max(0, min(total_n, int(round(slot["start_sec"] * SAMPLE_RATE))))
            end = max(start, min(total_n, int(round(slot["end_sec"] * SAMPLE_RATE))))
            target = end - start
            if target <= 0:
                continue
            bed = _music_pad(target / SAMPLE_RATE, beat.get("music_cue", ""))
            if len(bed) < target:
                bed = np.pad(bed, (0, target - len(bed)))
            mixed[start:end] += bed[:target]

    sfx_pack = _load_sfx_pack(sfx_dir)
    effect_count = 0
    for beat, slot in zip(beats, timeline):
        base = int(slot["start_sec"] * SAMPLE_RATE)
        cues: list[tuple[str, int]] = []
        if str(beat.get("transition_in", "cut")).lower() not in ("cut", "", "none"):
            cues.append(("whoosh", base + int(0.02 * SAMPLE_RATE)))
        for sfx in beat.get("audio_sfx", [])[:2]:
            name = sfx.get("name", "accent") if isinstance(sfx, dict) else str(sfx)
            cues.append((name, base + int(0.08 * SAMPLE_RATE)))
        for name, start in cues:
            fx = _sfx_for(name, sfx_pack)
            start = min(total_n, start)
            end = min(total_n, start + len(fx))
            if end > start:
                mixed[start:end] += fx[: end - start]
                effect_count += 1

    _write_wav(output_path, mixed)
    return {
        "path": str(Path(output_path).resolve()),
        "sample_rate": SAMPLE_RATE,
        "duration_seconds": round(total_n / SAMPLE_RATE, 4),
        "beat_count": len(beats),
        "silence_between_beats_sec": pause_seconds,
        "procedural_bed": used_procedural,
        "effect_count": effect_count,
        "music_track": str(track_path) if track_path is not None and music is not None else None,
        "music_level": level if music is not None else 0.0,
        "sfx_pack_families": sorted(sfx_pack.keys()),
        "timeline": timeline,
        "note": "Music/SFX from local licensed packs; ducking applied under narration.",
    }