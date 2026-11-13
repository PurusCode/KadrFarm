from __future__ import annotations

import wave
from pathlib import Path

import pytest

from kadr_farm.assemble import _validate_audio_manifest


def _write_wav(
    path: Path,
    *,
    sample_rate: int = 24_000,
    channels: int = 1,
    sample_width: int = 2,
    duration_seconds: float = 0.25,
) -> float:
    frames = int(sample_rate * duration_seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00" * frames * channels * sample_width)
    return frames / sample_rate


def _manifest(path: Path, *, status: str = "complete") -> tuple[list[dict], dict]:
    duration = _write_wav(path)
    blocks = [{"id": "block_1", "voice_text": "Тест."}]
    manifest = {
        "status": status,
        "blocks": [
            {
                "id": "block_1",
                "file": path.name,
                "duration_seconds": duration,
            }
        ],
    }
    return blocks, manifest


def test_accepts_complete_manifest_with_kokoro_wav(tmp_path: Path):
    wav_path = tmp_path / "block_1.wav"
    script_blocks, manifest = _manifest(wav_path)

    result = _validate_audio_manifest(script_blocks, manifest, tmp_path)

    assert list(result) == ["block_1"]
    assert result["block_1"]["file"] == "block_1.wav"


def test_rejects_failed_manifest_even_when_ids_and_wav_match(tmp_path: Path):
    wav_path = tmp_path / "block_1.wav"
    script_blocks, manifest = _manifest(wav_path, status="failed")

    with pytest.raises(RuntimeError, match="complete"):
        _validate_audio_manifest(script_blocks, manifest, tmp_path)


def test_rejects_manifest_without_complete_status(tmp_path: Path):
    wav_path = tmp_path / "block_1.wav"
    script_blocks, manifest = _manifest(wav_path)
    manifest.pop("status")

    with pytest.raises(RuntimeError, match="complete"):
        _validate_audio_manifest(script_blocks, manifest, tmp_path)


@pytest.mark.parametrize(
    ("sample_rate", "channels", "sample_width"),
    [
        (22_050, 1, 2),
        (24_000, 2, 2),
        (24_000, 1, 1),
    ],
)
def test_rejects_wav_outside_kokoro_pcm_contract(
    tmp_path: Path,
    sample_rate: int,
    channels: int,
    sample_width: int,
):
    wav_path = tmp_path / "block_1.wav"
    duration = _write_wav(
        wav_path,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
    )
    script_blocks = [{"id": "block_1", "voice_text": "Тест."}]
    manifest = {
        "status": "complete",
        "blocks": [
            {
                "id": "block_1",
                "file": wav_path.name,
                "duration_seconds": duration,
            }
        ],
    }

    with pytest.raises(RuntimeError, match="Неподдерживаемый WAV для Kokoro"):
        _validate_audio_manifest(script_blocks, manifest, tmp_path)
