from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from kadr_farm.common import write_json
from kadr_farm.kokoro_engine import (
    _audio_cache_runtime_fingerprint,
    sha256_file,
)
from kadr_farm.production_director import (
    _file_sha256,
    _load_cached_visual_asset,
)
from kadr_farm.storyboard import SYSTEM_PROMPT


def _write(path: Path, content: bytes = b"test") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _fake_kokoro_tree(tmp_path: Path):
    data_dir = tmp_path / "data"
    root = data_dir / "models" / "kokoro-ru-v2-onnx"
    accent = data_dir / "models" / "ruaccent-accentuator"

    files = [
        root / "onnx" / "model_dima_quantized.onnx",
        root / "voices" / "dima.bin",
        root / "config.json",
        root / "kokoro-config.json",
        root / "ru_g2p.py",
        root / "espeak-data" / "ru_dict",
        accent / "dictionary" / "words.txt",
        accent / "nn" / "nn_accent" / "weights.bin",
        accent / "nn" / "nn_stress_usage_predictor" / "weights.bin",
        accent / "nn" / "nn_yo_homograph_resolver" / "weights.bin",
    ]

    for index, path in enumerate(files):
        _write(path, f"file-{index}".encode())

    stress = tmp_path / "project" / "data" / "kokoro_stress_config.json"
    _write(stress, b'{"schema_version": 1}')

    settings = SimpleNamespace(data_dir=data_dir)

    return settings, stress, root, accent


def test_kokoro_fingerprint_changes_when_model_or_voice_changes(tmp_path, monkeypatch):
    import kadr_farm.kokoro_engine as engine

    settings, stress, root, _accent = _fake_kokoro_tree(tmp_path)

    monkeypatch.setattr(
        engine,
        "package_version",
        lambda _name: "1.2.3",
    )

    stress_digest = sha256_file(stress)

    before = _audio_cache_runtime_fingerprint(
        settings,
        stress,
        stress_digest,
        "normalizer-v1",
        "backend-a",
    )

    assert before is not None
    assert before["audio_cache_fingerprint_version"] == 2

    model_file = root / "onnx" / "model_dima_quantized.onnx"
    _write(model_file, b"changed-model")

    after_model_change = _audio_cache_runtime_fingerprint(
        settings,
        stress,
        stress_digest,
        "normalizer-v1",
        "backend-a",
    )

    assert after_model_change is not None
    assert after_model_change["model_sha256"] != before["model_sha256"]

    voice_file = root / "voices" / "dima.bin"
    _write(voice_file, b"changed-voice")

    after_voice_change = _audio_cache_runtime_fingerprint(
        settings,
        stress,
        stress_digest,
        "normalizer-v1",
        "backend-a",
    )

    assert after_voice_change is not None
    assert after_voice_change["voice_sha256"] != after_model_change["voice_sha256"]


def test_kokoro_fingerprint_changes_when_accent_assets_change(tmp_path, monkeypatch):
    import kadr_farm.kokoro_engine as engine

    settings, stress, _root, accent = _fake_kokoro_tree(tmp_path)

    monkeypatch.setattr(
        engine,
        "package_version",
        lambda _name: "1.2.3",
    )

    before = _audio_cache_runtime_fingerprint(
        settings,
        stress,
        sha256_file(stress),
        "normalizer-v1",
        "backend-a",
    )

    assert before is not None

    _write(
        accent / "nn" / "nn_accent" / "weights.bin",
        b"changed-accent",
    )

    after = _audio_cache_runtime_fingerprint(
        settings,
        stress,
        sha256_file(stress),
        "normalizer-v1",
        "backend-a",
    )

    assert after is not None
    assert after["accent_assets_sha256"] != before["accent_assets_sha256"]


def test_kokoro_fingerprint_changes_when_normalizer_changes(tmp_path, monkeypatch):
    import kadr_farm.kokoro_engine as engine

    settings, stress, _root, _accent = _fake_kokoro_tree(tmp_path)

    monkeypatch.setattr(
        engine,
        "package_version",
        lambda _name: "1.2.3",
    )

    first = _audio_cache_runtime_fingerprint(
        settings,
        stress,
        sha256_file(stress),
        "normalizer-v1",
        "backend-a",
    )

    second = _audio_cache_runtime_fingerprint(
        settings,
        stress,
        sha256_file(stress),
        "normalizer-v2",
        "backend-a",
    )

    assert first is not None
    assert second is not None
    assert first["text_normalizer_version"] != second["text_normalizer_version"]


def test_kokoro_fingerprint_is_unavailable_when_runtime_files_are_missing(tmp_path, monkeypatch):
    import kadr_farm.kokoro_engine as engine

    settings = SimpleNamespace(data_dir=tmp_path / "data")
    stress = tmp_path / "stress.json"
    _write(stress)

    monkeypatch.setattr(
        engine,
        "package_version",
        lambda _name: "1.2.3",
    )

    assert _audio_cache_runtime_fingerprint(
        settings,
        stress,
        sha256_file(stress),
        "normalizer-v1",
        "backend-a",
    ) is None


def test_visual_checkpoint_rejects_changed_overlay(tmp_path):
    visual = tmp_path / "visual.mp4"
    overlay = tmp_path / "overlay.png"
    checkpoint = tmp_path / "asset.json"

    _write(visual, b"visual")
    _write(overlay, b"overlay")

    asset = {
        "path": str(visual),
        "sha256": _file_sha256(visual),
        "overlay_path": str(overlay),
        "overlay_sha256": _file_sha256(overlay),
    }

    write_json(
        checkpoint,
        {
            "input_fingerprint": "expected",
            "asset": asset,
        },
    )

    assert _load_cached_visual_asset(checkpoint, "expected") is not None

    _write(overlay, b"modified-overlay")

    assert _load_cached_visual_asset(checkpoint, "expected") is None


def test_visual_checkpoint_rejects_overlay_without_checksum(tmp_path):
    visual = tmp_path / "visual.mp4"
    overlay = tmp_path / "overlay.png"
    checkpoint = tmp_path / "asset.json"

    _write(visual, b"visual")
    _write(overlay, b"overlay")

    write_json(
        checkpoint,
        {
            "input_fingerprint": "expected",
            "asset": {
                "path": str(visual),
                "sha256": _file_sha256(visual),
                "overlay_path": str(overlay),
            },
        },
    )

    assert _load_cached_visual_asset(checkpoint, "expected") is None


def test_storyboard_prompt_is_not_restricted_to_technology():
    assert "широкой тематикой" in SYSTEM_PROMPT
    assert "Не своди тематику канала к ИИ или технологиям" in SYSTEM_PROMPT
