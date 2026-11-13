from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from kadr_farm import kokoro_engine, tts
from kadr_farm.assemble import _validate_audio_manifest
from kadr_farm.common import write_json


class FakeG2P:
    def phonemize(self, text):
        return "abc", []


class FakeSession:
    def __init__(self):
        self.calls = []

    def run(self, _outputs, inputs):
        self.calls.append(inputs)
        return [np.full(2400, 0.05, dtype=np.float32)]


def _fake_runtime(tmp_path: Path):
    model = tmp_path / "model.onnx"
    voice = tmp_path / "dima.bin"
    model_cfg = tmp_path / "config.json"
    kokoro_cfg = tmp_path / "kokoro-config.json"
    g2p_file = tmp_path / "ru_g2p.py"
    stress = tmp_path / "stress.json"
    for path, payload in (
        (model, b"fake model"),
        (voice, b"fake voice"),
        (model_cfg, b'{"vocab":{"a":1,"b":2,"c":3}}'),
        (kokoro_cfg, b"{}"),
        (g2p_file, b"# fake"),
        (stress, b'{"schema_version":1,"ruaccent":{},"context_rules":[]}'),
    ):
        path.write_bytes(payload)
    from kadr_farm.kokoro_stress import load_stress_config
    return kokoro_engine.KokoroRuntime(
        session=FakeSession(),
        g2p=FakeG2P(),
        vocab={"a": 1, "b": 2, "c": 3},
        style=np.zeros((510, 256), dtype=np.float32),
        model_root=tmp_path,
        accent_model_root=tmp_path,
        model_path=model,
        voice_path=voice,
        stress_config_path=stress,
        stress_config=load_stress_config(stress),
        model_config_path=model_cfg,
        kokoro_config_path=kokoro_cfg,
        g2p_path=g2p_file,
    )


def _settings(tmp_path: Path, *, engine="kokoro", speaker="dima"):
    return SimpleNamespace(
        root=tmp_path,
        data_dir=tmp_path / "data",
        tts_speed=0.9,
        tts_engine=engine,
        tts_speaker=speaker,
    )


def _script(tmp_path: Path, blocks=None):
    path = tmp_path / "script.json"
    write_json(path, {"blocks": blocks or [
        {"id": "intro", "heading": "Вступление", "voice_text": "Проверочный текст."},
        {"id": "body", "heading": "Основная часть", "voice_text": "Второй проверочный текст."},
    ]})
    return path


def test_production_synthesize_uses_kokoro_and_writes_compatible_manifest(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    runtime = _fake_runtime(tmp_path)
    monkeypatch.setattr(kokoro_engine, "load_runtime", lambda _settings, _threads: runtime)
    monkeypatch.setenv("KOKORO_THREADS", "2")

    out = tmp_path / "audio"
    manifest = tts.synthesize(settings, _script(tmp_path), out, speed=0.9)

    assert manifest["status"] == "complete"
    assert manifest["engine"] == "Kokoro-ru v2 ONNX (CPU)"
    assert manifest["engine_package"] == "onnxruntime"
    assert manifest["revision"] == kokoro_engine.REPO_REVISION
    assert manifest["voice"] == "dima"
    assert manifest["blocks_expected"] == manifest["blocks_completed"] == 2
    assert [row["id"] for row in manifest["blocks"]] == ["intro", "body"]
    assert len(manifest["model_sha256"]) == 64
    assert len(manifest["voice_sha256"]) == 64
    assert len(manifest["stress_config_sha256"]) == 64
    assert runtime.session.calls[0]["speed"].tolist() == pytest.approx([0.9])
    for row in manifest["blocks"]:
        wav = out / row["file"]
        assert wav.is_file()
        assert row["sample_rate"] == 24000
        assert row["channels"] == 1
        assert row["sample_width_bytes"] == 2
        assert row["duration_seconds"] == 0.1
        assert row["source_voice_text_sha256"]
        assert row["normalized_text_sha256"]
    saved = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert saved["status"] == "complete"
    assert len(saved["blocks"]) == 2


def test_piper_is_rejected_without_fallback(tmp_path, monkeypatch):
    settings = _settings(tmp_path, engine="piper")
    monkeypatch.setattr(
        kokoro_engine, "load_runtime",
        lambda *_args: pytest.fail("Runtime must not be loaded for unsupported engine"),
    )
    with pytest.raises(RuntimeError, match="fallback на Piper нет"):
        tts.synthesize(settings, _script(tmp_path), tmp_path / "audio")


def test_non_dima_voice_is_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="проверенный голос Kokoro Dima"):
        tts.synthesize(_settings(tmp_path, speaker="sveta"), _script(tmp_path), tmp_path / "audio")


@pytest.mark.parametrize("speed", [0.0, 0.49, 2.01, float("inf")])
def test_invalid_speed_is_rejected_before_model_load(tmp_path, monkeypatch, speed):
    monkeypatch.setattr(
        kokoro_engine, "load_runtime",
        lambda *_args: pytest.fail("Model must not load for invalid speed"),
    )
    with pytest.raises(ValueError, match="Скорость TTS"):
        tts.synthesize(_settings(tmp_path), _script(tmp_path), tmp_path / "audio", speed=speed)


def test_empty_block_is_rejected_before_model_load(tmp_path, monkeypatch):
    monkeypatch.setattr(
        kokoro_engine, "load_runtime",
        lambda *_args: pytest.fail("Model must not load for empty text"),
    )
    script = _script(tmp_path, [{"id": "empty", "voice_text": "  "}])
    with pytest.raises(ValueError, match="Пустой voice_text"):
        tts.synthesize(_settings(tmp_path), script, tmp_path / "audio")


def test_duplicate_ids_are_rejected_before_model_load(tmp_path, monkeypatch):
    monkeypatch.setattr(
        kokoro_engine, "load_runtime",
        lambda *_args: pytest.fail("Model must not load for duplicate IDs"),
    )
    script = _script(tmp_path, [
        {"id": "same", "voice_text": "Первый текст."},
        {"id": "same", "voice_text": "Второй текст."},
    ])
    with pytest.raises(ValueError, match="Повторяется ID"):
        tts.synthesize(_settings(tmp_path), script, tmp_path / "audio")


def test_failed_inference_persists_failed_manifest(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    runtime = _fake_runtime(tmp_path)

    def fail_run(*_args, **_kwargs):
        raise RuntimeError("fake inference failure")

    runtime.session.run = fail_run
    monkeypatch.setattr(kokoro_engine, "load_runtime", lambda *_args: runtime)
    out = tmp_path / "audio"
    with pytest.raises(RuntimeError, match="fake inference failure"):
        tts.synthesize(settings, _script(tmp_path, [
            {"id": "intro", "voice_text": "Проверочный текст."}
        ]), out)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert "fake inference failure" in manifest["error"]


def test_audio_manifest_must_cover_every_script_block(tmp_path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    import soundfile as sf
    sf.write(str(audio_dir / "intro.wav"), np.zeros(2400, dtype=np.float32), 24000, subtype="PCM_16")
    manifest = {"status": "complete", "blocks": [{"id": "intro", "file": "intro.wav", "duration_seconds": 0.1}]}
    with pytest.raises(RuntimeError, match="не соответствует сценарию"):
        _validate_audio_manifest([{"id": "intro"}, {"id": "body"}], manifest, audio_dir)


def test_audio_manifest_checks_declared_duration(tmp_path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    import soundfile as sf
    sf.write(str(audio_dir / "intro.wav"), np.zeros(2400, dtype=np.float32), 24000, subtype="PCM_16")
    manifest = {"status": "complete", "blocks": [{"id": "intro", "file": "intro.wav", "duration_seconds": 2.0}]}
    with pytest.raises(RuntimeError, match="не совпадает с манифестом"):
        _validate_audio_manifest([{"id": "intro"}], manifest, audio_dir)


def test_control_script_covers_required_speech_cases():
    root = Path(__file__).resolve().parents[1]
    script_path = root / "data" / "kokoro_stress_audio_test_script.json"
    script = json.loads(script_path.read_text(encoding="utf-8"))
    assert [block["id"] for block in script["blocks"]] == [
        "door_lock",
        "old_castle",
        "door_lock_genitive",
    ]
    assert all(str(block.get("voice_text", "")).strip() for block in script["blocks"])


def test_description_does_not_claim_stock_sources_when_no_external_media_used(tmp_path):
    from kadr_farm.assemble import create_description
    script_path = tmp_path / "script.json"
    write_json(script_path, {"title": "Тестовый ролик", "blocks": [{"id": "intro", "heading": "Вступление", "kind": "body"}]})
    payload = create_description(script_path, tmp_path / "publish.txt", used_media=[])
    assert "Pexels" not in payload["description"]
    assert "Pixabay" not in payload["description"]
    assert payload["media_attribution_status"] == "NO_EXTERNAL_MEDIA_USED"
    assert "не использованы" in payload["media_attribution"]


def test_description_lists_only_media_reported_as_actually_used(tmp_path):
    from kadr_farm.assemble import create_description
    script_path = tmp_path / "script.json"
    write_json(script_path, {"title": "Тестовый ролик", "blocks": []})
    used = [{
        "kind": "broll", "file": "clip.mp4", "source": "Pexels",
        "page_url": "https://example.invalid/clip", "creator": "Test Creator",
        "creator_url": "https://example.invalid/creator", "query": "science",
    }]
    payload = create_description(script_path, tmp_path / "publish.txt", used_media=used)
    assert "Pexels — Test Creator; https://example.invalid/clip" in payload["description"]
    assert payload["broll_sources"] == ["Pexels"]
    assert payload["media_attribution_status"] == "REVIEW_REQUIRED"
    assert payload["used_media"] == used


def test_description_created_without_render_evidence_marks_media_unknown(tmp_path):
    from kadr_farm.assemble import create_description
    script_path = tmp_path / "script.json"
    write_json(script_path, {"title": "Черновик описания", "blocks": []})
    payload = create_description(script_path, tmp_path / "publish.txt")
    assert payload["media_attribution_status"] == "UNKNOWN_REVIEW_REQUIRED"
    assert "не определена" in payload["media_attribution"]
    assert "Pexels" not in payload["description"]
    assert "Pixabay" not in payload["description"]


def test_runtime_initialization_failure_persists_failed_manifest(tmp_path, monkeypatch):
    def fail_load(*_args):
        raise FileNotFoundError("RUAccent assets missing")
    monkeypatch.setattr(kokoro_engine, "load_runtime", fail_load)
    out = tmp_path / "audio"
    with pytest.raises(FileNotFoundError, match="RUAccent assets missing"):
        tts.synthesize(_settings(tmp_path), _script(tmp_path, [
            {"id": "intro", "voice_text": "Проверочный текст."}
        ]), out)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert "RUAccent assets missing" in manifest["error"]
