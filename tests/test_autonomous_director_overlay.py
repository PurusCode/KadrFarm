import pytest
from kadr_farm.production_director import _check_exact_coverage, _norm
from kadr_farm.director_models import DirectorBeat, DirectorScenePlan
from kadr_farm.director_sources import _safe_public_url

def test_exact_spoken_text_coverage_accepts_whitespace_only_changes():
    scene={"id":"s_001","voice_text":"Привет,   мир!"}
    plan={"scene_id":"s_001","beats":[{"spoken_text":"Привет, мир!"}]}
    _check_exact_coverage(plan,scene)

def test_exact_spoken_text_coverage_rejects_paraphrase():
    scene={"id":"s_001","voice_text":"Привет, мир!"}
    plan={"scene_id":"s_001","beats":[{"spoken_text":"Здравствуйте, мир!"}]}
    with pytest.raises(ValueError): _check_exact_coverage(plan,scene)

def test_provider_strategy_contract():
    with pytest.raises(Exception):
        DirectorBeat(spoken_text="Речь",visual_type="broll",visual_description="Подходящее видео смартфона",
            asset_strategy="stock",provider_id="internal_renderer",search_queries=["phone typing"])

def test_manual_strategy_is_not_in_schema():
    with pytest.raises(Exception):
        DirectorBeat(spoken_text="Речь",visual_type="screencast",visual_description="Симуляция интерфейса чата",
            asset_strategy="manual",provider_id="manual_input")

def test_local_urls_are_blocked():
    assert _safe_public_url("http://127.0.0.1:8000")[0] is False
    assert _safe_public_url("http://localhost/admin")[0] is False

def test_public_https_url_passes_syntax_gate():
    assert _safe_public_url("https://example.org/page")== (True,"ok")

def test_rendered_templates_produce_full_hd_png(tmp_path):
    from PIL import Image
    from kadr_farm.director_visuals import render_beat_visual

    path = render_beat_visual(
        {"id": "s_001", "meaning": "Объяснение токенизации"},
        {"visual_type": "text", "visual_description": "Слово разбивается на фрагменты",
         "on_screen_text": ["ТОКЕН", "кусочек слова"]},
        tmp_path / "tokens.png",
    )
    with Image.open(path) as image:
        assert image.size == (1920, 1080)
        assert image.format == "PNG"


def test_music_pad_preserves_exact_sample_count_after_float_roundtrip():
    from kadr_farm.director_audio import _music_pad, SAMPLE_RATE

    # This sample count is a known float edge case: int(215279 / 24000 *
    # 24000) evaluates to 215278 on CPython due to binary rounding.
    sample_count = 215279
    pad = _music_pad(sample_count / SAMPLE_RATE, "calm")
    assert len(pad) == sample_count


def test_audio_master_clamps_rounded_final_endpoint_to_narration_length(tmp_path):
    import wave
    from kadr_farm.director_audio import build_master_audio, SAMPLE_RATE

    source = tmp_path / "fractional.wav"
    sample_count = 215279
    with wave.open(str(source), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(b"\x00\x00" * sample_count)

    output = tmp_path / "master.wav"
    report = build_master_audio([
        {"scene_id": "s_001", "beat_id": "b01", "audio_path": str(source), "music_cue": "calm"}
    ], output, add_procedural_bed=True)
    with wave.open(str(output), "rb") as wav:
        assert wav.getnframes() == sample_count
    assert report["duration_seconds"] == round(sample_count / SAMPLE_RATE, 4)


def test_audio_master_uses_measured_wav_lengths_and_interbeat_gap(tmp_path):
    import wave
    from kadr_farm.director_audio import build_master_audio, SAMPLE_RATE

    paths = []
    for name, frames in (("one.wav", 4800), ("two.wav", 7200)):
        path = tmp_path / name
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(b"\x00\x00" * frames)
        paths.append(path)
    report = build_master_audio([
        {"scene_id": "s_001", "beat_id": "b01", "audio_path": str(paths[0])},
        {"scene_id": "s_001", "beat_id": "b02", "audio_path": str(paths[1])},
    ], tmp_path / "master.wav", pause_seconds=0.1, add_procedural_bed=False)
    assert report["timeline"][0]["voice_duration_sec"] == 0.2
    assert report["timeline"][1]["start_sec"] == 0.3
    assert abs(report["duration_seconds"] - 0.6) < 0.001


def test_media_production_synthesizes_each_exact_phrase_and_renders_fallback(tmp_path, monkeypatch):
    import wave
    from pathlib import Path
    from types import SimpleNamespace
    from kadr_farm import production_director as director

    def fake_normalize(text):
        return text

    def fake_synthesize(_settings, prepared, out_dir, _speed):
        out_dir.mkdir(parents=True, exist_ok=True)
        blocks = []
        for index, (block_id, _meta, _raw, _normalized) in enumerate(prepared, 1):
            filename = f"{index:02d}_{block_id}.wav"
            path = out_dir / filename
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(b"\x00\x00" * 24000)
            blocks.append({"id": block_id, "file": filename, "duration_seconds": 1.0})
        return {"status": "complete", "engine": "Kokoro test double", "blocks": blocks}

    monkeypatch.setattr(director, "normalize_for_speech", fake_normalize)
    monkeypatch.setattr(director, "synthesize_kokoro", fake_synthesize)
    settings = SimpleNamespace(tts_engine="kokoro", tts_speaker="dima", pixabay_api_key="")
    scenes = [{"id": "s_001", "meaning": "Объяснение", "purpose": "Пояснить мысль"}]
    plans = [{"scene_id": "s_001", "editorial_intent": "Сначала запрос, затем ответ",
              "music_cue": "calm", "claims": [], "beats": [
        {"spoken_text": "Первое точное предложение.", "visual_type": "comparison",
         "visual_description": "Сравнение старой базы данных и языковой модели",
         "asset_strategy": "rendered", "provider_id": "internal_renderer",
         "gen_prompt": None, "search_queries": [], "on_screen_text": ["АРХИВ", "МОДЕЛЬ"],
         "motion": "slow_zoom", "transition_in": "cut", "audio_sfx": []}
    ]}]
    beats, report = director._produce(settings, scenes, plans, tmp_path / "production", 0.9)
    assert report["status"] == "complete"
    assert len(beats) == 1
    assert beats[0]["spoken_text"] == "Первое точное предложение."
    assert Path(beats[0]["audio_path"]).is_file()
    assert Path(beats[0]["visual_path"]).is_file()
    assert beats[0]["source"] == "AI Video Factory local renderer"


def test_source_audit_never_marks_claim_verified(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from kadr_farm import director_sources

    monkeypatch.setattr(director_sources, "fetch_source_excerpt",
                        lambda _url: {"url": _url, "status": "http_200", "text": "Evidence text"})

    class FakeRouter:
        def __init__(self, _settings):
            pass
        def complete(self, _schema, _system, prompt, **_kwargs):
            import json
            from kadr_farm.director_models import SourceEvidenceBatch, SourceEvidence
            # Read source IDs from the exact payload line after the system prompt.
            data = json.loads(prompt.split("\n", 1)[1])
            return SourceEvidenceBatch(assessments=[SourceEvidence(
                source_id=item["source_id"], verdict="supports",
                evidence_quote="Evidence text", reasoning="The visible excerpt matches the claim.")
                for item in data])

    monkeypatch.setattr(director_sources, "StructuredLLMRouter", FakeRouter)
    report = director_sources.audit_claim_sources(SimpleNamespace(), [{
        "scene_id": "s_001", "claims": [{"text": "A factual claim", "source_urls": ["https://example.org"]}]
    }], tmp_path / "sources.json")
    assert report["facts_verified"] is False
    assert report["entries"][0]["ai_verdict"] == "supports"
    assert report["entries"][0]["verified_by_human"] is False
    assert report["entries"][0]["status"] == "human_review_required"


def test_critic_can_use_a_separate_configured_provider(monkeypatch):
    from types import SimpleNamespace
    from kadr_farm.production_director import _critic_settings

    primary = SimpleNamespace(name="primary", model="mws-gemma")
    secondary = SimpleNamespace(name="secondary", model="qwen-local")
    settings = SimpleNamespace(providers=[primary, secondary])
    monkeypatch.setenv("DIRECTOR_CRITIC_PROVIDER", "secondary")
    critic_settings = _critic_settings(settings)
    assert critic_settings is not settings
    assert critic_settings.providers == [secondary]
    assert settings.providers == [primary, secondary]


def test_pixabay_photo_is_decoded_and_resolution_checked(tmp_path):
    from types import SimpleNamespace
    from PIL import Image
    from kadr_farm.production_director import _validate_pixabay_asset

    good = tmp_path / "good.jpg"
    Image.new("RGB", (800, 450), (20, 60, 90)).save(good)
    ok, detail = _validate_pixabay_asset(SimpleNamespace(), {"path": str(good)}, "photo")
    assert ok is True
    assert detail == "image_ok_800x450"

    small = tmp_path / "small.jpg"
    Image.new("RGB", (320, 180), (20, 60, 90)).save(small)
    ok, detail = _validate_pixabay_asset(SimpleNamespace(), {"path": str(small)}, "photo")
    assert ok is False
    assert "resolution_too_small" in detail


def test_render_never_exposes_visual_description_as_visible_copy(tmp_path):
    import json
    from kadr_farm.director_visuals import render_beat_visual

    description = "Статичный макет чата с ИИ. Блок текста появляется мгновенно."
    image = render_beat_visual(
        {"id": "s_001", "meaning": "Постановка главного вопроса и введение термина галлюцинации"},
        {"visual_type": "screencast", "visual_description": description,
         "spoken_text": "Нейросеть иногда уверенно отвечает даже при недостатке информации.",
         "on_screen_text": ["Статичный макет чата с ИИ", "ОТВЕТ МОДЕЛИ"]},
        tmp_path / "chat.png",
    )
    metadata = json.loads(image.with_suffix(".png.meta.json").read_text(encoding="utf-8"))
    assert metadata["description_rendered"] is False
    assert description not in metadata["visible_text"]
    assert all("макет" not in text.lower() and "появляется" not in text.lower()
               for text in metadata["visible_text"])
    assert metadata["template_id"] == "simulated_interface"


def test_instruction_like_display_labels_are_filtered():
    from kadr_farm.director_visuals import _safe_labels

    labels = _safe_labels({"on_screen_text": [
        "Центральный белый текст на темном фоне",
        "Статичный макет чата с ИИ",
        "ПРОВЕРКА ИСТОЧНИКА",
    ]})
    assert labels == ["ПРОВЕРКА ИСТОЧНИКА"]


def test_visual_motif_does_not_confuse_context_with_tokenization():
    from kadr_farm.director_visuals import _motif

    motif = _motif(
        {"meaning": "Проверка достоверности источника"},
        {"spoken_text": "Перед публикацией нужно проверить исходный источник и контекст.",
         "visual_description": "Стилизованная иконка лупы"},
    )
    assert motif == "search"


def test_visual_audit_detects_duplicate_images_and_records_templates(tmp_path):
    import json
    from PIL import Image
    from kadr_farm.director_visuals import render_beat_visual, audit_visual_assets

    path1 = render_beat_visual(
        {"id": "s_001", "meaning": "Проверка источника"},
        {"visual_type": "illustration", "spoken_text": "Проверим исходный источник.",
         "visual_description": "Нарисовать поисковую лупу.", "on_screen_text": ["ИСТОЧНИК"]},
        tmp_path / "one.png",
    )
    # A byte-identical asset is an intentional test fixture for the duplicate gate.
    path2 = tmp_path / "two.png"
    path2.write_bytes(path1.read_bytes())
    audit = audit_visual_assets([
        {"scene_id": "s_001", "beat_id": "b01", "visual_path": str(path1),
         "media_type": "image", "visual_type": "illustration", "source": "renderer"},
        {"scene_id": "s_002", "beat_id": "b01", "visual_path": str(path2),
         "media_type": "image", "visual_type": "illustration", "source": "renderer"},
    ], tmp_path / "visual_audit.json")
    assert audit["status"] == "passed_with_warnings"
    assert any("near-duplicate" in warning for warning in audit["warnings"])
    assert audit["entries"][0]["template_id"] == "illustration_search"
    saved = json.loads((tmp_path / "visual_audit.json").read_text(encoding="utf-8"))
    assert saved["semantic_frame_to_speech"] == "human_review_required"


def test_local_renderer_strategy_rejects_stock_visual_types():
    with pytest.raises(Exception):
        DirectorBeat(
            spoken_text="В кадре реальное видео.", visual_type="video",
            visual_description="Реальный видеоматериал с человеком.",
            asset_strategy="rendered", provider_id="internal_renderer",
        )


def test_unverified_percent_labels_are_not_rendered():
    from kadr_farm.director_visuals import _safe_labels

    assert _safe_labels({"on_screen_text": ["Париж 99%", "ПРОВЕРИТЬ ИСТОЧНИК"]}) == ["ПРОВЕРИТЬ ИСТОЧНИК"]


def test_final_video_audit_extracts_real_encoded_frames_and_contact_sheet(tmp_path):
    from pathlib import Path
    import shutil
    import subprocess
    import pytest
    from kadr_farm.director_visuals import render_beat_visual, audit_final_video_frames

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg is not installed in this test environment")
    image = render_beat_visual(
        {"id": "s_001", "meaning": "Проверка источника"},
        {"visual_type": "illustration", "spoken_text": "Нужно проверить источник.",
         "visual_description": "Нарисовать поисковую лупу.", "on_screen_text": ["ИСТОЧНИК"]},
        tmp_path / "source.png",
    )
    video = tmp_path / "final.mp4"
    subprocess.run([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-i", str(image),
        "-t", "1.2", "-r", "25", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
    ], capture_output=True, check=True)
    report = audit_final_video_frames(
        video,
        [{"scene_id": "s_001", "beat_id": "b01", "start_sec": 0.0, "end_sec": 1.2,
          "spoken_text": "Нужно проверить источник."}],
        tmp_path / "final_audit.json", tmp_path / "contact_sheet.jpg", ffmpeg=ffmpeg,
    )
    assert report["status"] == "passed"
    assert report["decoded_frame_count"] == 1
    assert Path(report["entries"][0]["frame_path"]).is_file()
    assert Path(report["contact_sheet"]).is_file()
