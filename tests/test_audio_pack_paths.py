from kadr_farm.director_audio import _resolve_audio_pack_dir


def test_stale_absolute_audio_paths_fall_back_to_project_packs(tmp_path, monkeypatch):
    music_dir = tmp_path / "data" / "audio" / "music"
    sfx_dir = tmp_path / "data" / "audio" / "sfx"
    music_dir.mkdir(parents=True)
    sfx_dir.mkdir(parents=True)
    (music_dir / "chill-ambient.mp3").write_bytes(b"music")
    (sfx_dir / "whoosh.wav").write_bytes(b"sfx")

    monkeypatch.setenv("KADR_MUSIC_DIR", r"D:\KadrFarm\data\audio\music")
    monkeypatch.setenv("KADR_SFX_DIR", r"D:\KadrFarm\data\audio\sfx")

    assert _resolve_audio_pack_dir("KADR_MUSIC_DIR", tmp_path, "data/audio/music") == music_dir.resolve()
    assert _resolve_audio_pack_dir("KADR_SFX_DIR", tmp_path, "data/audio/sfx") == sfx_dir.resolve()


def test_existing_custom_audio_pack_is_respected(tmp_path, monkeypatch):
    custom = tmp_path / "custom-music"
    custom.mkdir()
    (custom / "track.mp3").write_bytes(b"music")
    monkeypatch.setenv("KADR_MUSIC_DIR", str(custom))
    assert _resolve_audio_pack_dir("KADR_MUSIC_DIR", tmp_path, "data/audio/music") == custom.resolve()
