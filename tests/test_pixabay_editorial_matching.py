import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kadr_farm import pixabay_media
from kadr_farm import production_director as director
from kadr_farm import media_producer


def _photo_hit(identity, tags, *, width=1920, height=1080, likes=100, downloads=200):
    return {
        "id": identity,
        "pageURL": f"https://pixabay.com/photos/{identity}/",
        "largeImageURL": f"https://cdn.example/{identity}.jpg",
        "imageWidth": width, "imageHeight": height,
        "tags": tags, "likes": likes, "downloads": downloads,
    }


def test_candidate_score_rejects_semantically_unrelated_result():
    relevant = pixabay_media._candidate_score(
        "person typing laptop", _photo_hit("laptop", "person typing laptop"), "photo")
    unrelated = pixabay_media._candidate_score(
        "person typing laptop", _photo_hit("forest", "mountain forest lake"), "photo")
    assert relevant > 0
    assert unrelated == -1.0


def test_fetch_pixabay_ranks_across_queries_and_excludes_reused_sources(tmp_path, monkeypatch):
    first = _photo_hit("typing-laptop", "person typing laptop office")
    second = _photo_hit("computer-desk", "office desktop computer desk",
                        width=1280, height=720, likes=4, downloads=8)

    def fake_search(_settings, query, _kind, _destination, _api_key):
        return {"hits": [first] if "laptop" in query else [second]}, True

    def fake_download(_url, target, _api_key):
        Path(target).write_bytes(b"x" * 2048)

    monkeypatch.setattr(pixabay_media, "_search_pixabay", fake_search)
    monkeypatch.setattr(pixabay_media, "_download_candidate", fake_download)
    settings = SimpleNamespace(pixabay_api_key="test-key", cache_dir=tmp_path / "cache")

    selected = pixabay_media.fetch_pixabay(
        settings, ["person typing laptop", "office desktop computer"], "photo", tmp_path / "media")
    assert selected["source_url"] == first["pageURL"]
    assert selected["candidate_count"] == 2
    assert selected["selection_score"] > 0
    assert selected["search_queries_checked"] == 2

    selected_without_first = pixabay_media.fetch_pixabay(
        settings, ["person typing laptop", "office desktop computer"], "photo", tmp_path / "media",
        exclude_source_urls={first["pageURL"]})
    assert selected_without_first["source_url"] == second["pageURL"]


def test_stock_selection_does_not_silently_fall_back_to_html(tmp_path, monkeypatch):
    render_called = False

    def no_stock(_settings, _queries, _kind, _destination, **_kwargs):
        return None

    def unexpected_render(*_args, **_kwargs):
        nonlocal render_called
        render_called = True
        raise AssertionError("stock selection must not be replaced with generated graphics")

    monkeypatch.setattr(director, "fetch_pixabay", no_stock)
    monkeypatch.setattr(director, "render_html_video", unexpected_render)
    beat = {
        "asset_strategy": "stock", "visual_type": "photo",
        "search_queries": ["desktop computer on office desk"],
        "spoken_text": "На столе стоит компьютер.",
        "shot_id": "b01_s01", "source_beat_id": "b01",
    }
    with pytest.raises(RuntimeError, match="Автозамена графикой запрещена"):
        director._asset_for_beat(SimpleNamespace(), {"id": "s_001"}, beat,
                                 tmp_path / "visuals", [], 1, 3.0)
    assert (tmp_path / "visuals" / "s_001_01_asset_failure.json").is_file()
    assert not render_called


def test_video_can_fall_back_to_relevant_photo_with_run_dedup(tmp_path, monkeypatch):
    from PIL import Image

    image = tmp_path / "relevant-photo.jpg"
    Image.new("RGB", (1280, 720)).save(image)
    calls = []

    def fake_fetch(
        _settings, _queries, kind, _destination,
        exclude_source_urls=None, download_budget=None,
    ):
        calls.append((
            kind,
            set(exclude_source_urls or set()),
            id(download_budget),
        ))
        if kind == "video":
            return None

        download_budget["used"] += 1
        return {
            "path": str(image), "media_type": "image", "source": "Pixabay",
            "source_url": "https://pixabay.com/photos/laptop/", "creator": "sample",
            "query": "laptop at desk", "candidate_count": 3, "selection_score": 102.5,
            "selected_tags": "laptop desk office", "selected_resolution": [1280, 720],
        }
    rendered = tmp_path / "visuals" / "s_001_01_photo.mp4"

    def fake_render(_beat, duration, output_path):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"rendered-video" * 100)

    monkeypatch.setattr(director, "fetch_pixabay", fake_fetch)
    monkeypatch.setattr(director, "_validate_pixabay_asset", lambda *_args: (True, "image_ok_1280x720"))
    monkeypatch.setattr(director, "render_html_video", fake_render)

    used = set()
    asset = director._asset_for_beat(
        SimpleNamespace(), {"id": "s_001"},
        {"asset_strategy": "stock", "visual_type": "video",
         "search_queries": ["person using laptop"], "spoken_text": "Человек работает за ноутбуком."},
        tmp_path / "visuals", [], 1, 3.4, used_source_urls=used)

    assert [row[0] for row in calls] == ["video", "photo"]
    assert calls[0][2] == calls[1][2]
    assert asset["beat_download_candidates_tried"] == 1
    assert asset["selection_fallback"] == "video_to_photo"
    assert asset["requested_media_type"] == "video"
    assert asset["template_id"] == "html_photo_kenburns"
    assert Path(asset["path"]) == rendered
    assert used == {"https://pixabay.com/photos/laptop/"}

def test_fetch_pixabay_respects_shared_candidate_budget(tmp_path, monkeypatch):
    first = _photo_hit("budget-first", "person typing laptop")
    second = _photo_hit("budget-second", "person typing laptop office")

    search_calls = []
    download_calls = []

    def fake_search(_settings, query, _kind, _destination, _api_key):
        search_calls.append(query)
        return {"hits": [first, second]}, True

    def fake_download(url, target, _api_key):
        download_calls.append(url)
        Path(target).write_bytes(b"x" * 2048)

    monkeypatch.setattr(pixabay_media, "_search_pixabay", fake_search)
    monkeypatch.setattr(pixabay_media, "_download_candidate", fake_download)

    settings = SimpleNamespace(
        pixabay_api_key="test-key",
        cache_dir=tmp_path / "cache",
    )
    budget = {"limit": 1, "used": 0}

    selected = pixabay_media.fetch_pixabay(
        settings,
        ["person typing laptop"],
        "photo",
        tmp_path / "media",
        download_budget=budget,
    )

    assert selected is not None
    assert selected["source_url"] == first["pageURL"]
    assert budget["used"] == 1

    next_candidate = pixabay_media.fetch_pixabay(
        settings,
        ["person typing laptop"],
        "photo",
        tmp_path / "media",
        exclude_source_urls={first["pageURL"]},
        download_budget=budget,
    )

    assert next_candidate is None
    assert budget["used"] == 1
    assert len(search_calls) == 1
    assert len(download_calls) == 1


def test_video_photo_fallback_shares_one_candidate_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(director, "MAX_DOWNLOAD_CANDIDATES", 1)

    calls = []

    def fake_fetch(
        _settings, _queries, kind, _destination,
        exclude_source_urls=None, download_budget=None,
    ):
        calls.append(kind)
        download_budget["used"] += 1

        return {
            "path": str(tmp_path / "unused.jpg"),
            "media_type": "image",
            "source": "Pixabay",
            "source_url": f"https://pixabay.com/{kind}/rejected/",
        }

    monkeypatch.setattr(director, "fetch_pixabay", fake_fetch)
    monkeypatch.setattr(
        director,
        "_validate_pixabay_asset",
        lambda *_args: (False, "test_rejected"),
    )

    beat = {
        "asset_strategy": "stock",
        "visual_type": "video",
        "search_queries": ["person using laptop"],
        "spoken_text": "Person using a laptop.",
    }

    with pytest.raises(RuntimeError):
        director._asset_for_beat(
            SimpleNamespace(),
            {"id": "s_budget"},
            beat,
            tmp_path / "visuals",
            [],
            1,
            3.0,
        )

    assert calls == ["video"]

    report_path = (
        tmp_path / "visuals" / "s_budget_01_asset_failure.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["download_candidates_tried"] == 1
    assert report["download_candidates_limit"] == 1
    assert report["status"] == "manual_review_required"


def test_photo_rate_limit_is_recorded_after_video_fallback(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(
        _settings, _queries, kind, _destination,
        exclude_source_urls=None, download_budget=None,
    ):
        calls.append(kind)

        if kind == "video":
            return None

        raise pixabay_media.PixabayError(
            "CDN rate limited",
            status_code=429,
        )

    monkeypatch.setattr(director, "fetch_pixabay", fake_fetch)

    beat = {
        "asset_strategy": "stock",
        "visual_type": "video",
        "search_queries": ["person using laptop"],
        "spoken_text": "Person using a laptop.",
    }

    with pytest.raises(RuntimeError):
        director._asset_for_beat(
            SimpleNamespace(),
            {"id": "s_429"},
            beat,
            tmp_path / "visuals",
            [],
            1,
            3.0,
        )

    assert calls == ["video", "photo"]

    report_path = tmp_path / "visuals" / "s_429_01_asset_failure.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["rate_limited"] is True


def test_semantic_stock_failure_requires_manual_review(tmp_path, monkeypatch):
    render_called = False

    def no_stock(_settings, _queries, _kind, _destination):
        return None

    def unexpected_render(*_args, **_kwargs):
        nonlocal render_called
        render_called = True
        raise AssertionError("Stock must not fall back to generated graphics.")

    monkeypatch.setattr(media_producer, "fetch_pixabay", no_stock)
    monkeypatch.setattr(
        media_producer, "render_scene_visual", unexpected_render
    )

    scene = {
        "id": "s_semantic",
        "voice_text": "A person works at a computer.",
    }
    spec = {
        "asset_strategy": "stock",
        "visual_type": "photo",
        "search_queries": ["person using computer", "office workstation"],
        "sub_duration_ratio": 1.0,
    }

    with pytest.raises(RuntimeError, match="manual review required"):
        media_producer._visual_job(
            SimpleNamespace(pixabay_api_key="test-key"),
            scene,
            spec,
            tmp_path,
            [],
            1,
        )

    report_path = (
        tmp_path / "visuals" / "s_semantic_01_asset_failure.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["status"] == "manual_review_required"
    assert report["search_queries"] == spec["search_queries"]
    assert report["rate_limited"] is False
    assert not render_called


def test_semantic_stock_passes_all_search_queries(tmp_path, monkeypatch):
    source = tmp_path / "sample.jpg"
    source.write_bytes(b"sample-stock-image")

    captured = []

    def fake_fetch(_settings, queries, kind, destination):
        captured.append((list(queries), kind, Path(destination)))
        return {
            "path": str(source),
            "source": "Pixabay",
            "source_url": "https://pixabay.com/photos/sample/",
        }

    monkeypatch.setattr(media_producer, "fetch_pixabay", fake_fetch)

    queries = [
        "person using laptop",
        "office workstation",
        "computer desk closeup",
    ]

    result = media_producer._visual_job(
        SimpleNamespace(pixabay_api_key="test-key"),
        {"id": "s_queries", "voice_text": "A person works."},
        {
            "asset_strategy": "stock",
            "visual_type": "photo",
            "search_queries": queries,
            "sub_duration_ratio": 1.0,
        },
        tmp_path,
        [],
        1,
    )

    assert captured[0][0] == queries
    assert captured[0][1] == "photo"
    assert result["source"] == "Pixabay"
    assert result["duration_ratio"] == 1.0


def test_semantic_stock_failure_records_rate_limit(tmp_path, monkeypatch):
    def rate_limited(_settings, _queries, _kind, _destination):
        raise pixabay_media.PixabayError(
            "Pixabay CDN rate limited",
            status_code=429,
        )

    monkeypatch.setattr(media_producer, "fetch_pixabay", rate_limited)

    with pytest.raises(RuntimeError, match="manual review required"):
        media_producer._visual_job(
            SimpleNamespace(pixabay_api_key="test-key"),
            {"id": "s_semantic_429", "voice_text": "A person works."},
            {
                "asset_strategy": "stock",
                "visual_type": "photo",
                "search_queries": ["person at computer"],
                "sub_duration_ratio": 1.0,
            },
            tmp_path,
            [],
            1,
        )

    report_path = (
        tmp_path / "visuals" / "s_semantic_429_01_asset_failure.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["rate_limited"] is True

def test_semantic_stock_failure_report_preserves_russian_utf8(tmp_path, monkeypatch):
    spoken_text = (
        "\u0427\u0435\u043b\u043e\u0432\u0435\u043a "
        "\u043f\u0440\u043e\u0432\u0435\u0440\u044f\u0435\u0442 "
        "\u043e\u0442\u0432\u0435\u0442 "
        "\u043d\u0435\u0439\u0440\u043e\u0441\u0435\u0442\u0438."
    )

    monkeypatch.setattr(
        media_producer,
        "fetch_pixabay",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="manual review required"):
        media_producer._visual_job(
            SimpleNamespace(pixabay_api_key="test-key"),
            {
                "id": "s_utf8",
                "voice_text": spoken_text,
            },
            {
                "asset_strategy": "stock",
                "visual_type": "photo",
                "search_queries": ["person checking information"],
                "sub_duration_ratio": 1.0,
            },
            tmp_path,
            [],
            1,
        )

    report_path = (
        tmp_path / "visuals" / "s_utf8_01_asset_failure.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["spoken_text"] == spoken_text
    assert report["status"] == "manual_review_required"


def test_semantic_internal_renderer_requires_rendered_strategy(
    tmp_path, monkeypatch
):
    rendered_paths = []

    def fake_renderer(scene, output_path):
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"test renderer output")
        rendered_paths.append(scene["visual_type"])
        return output_path

    monkeypatch.setattr(
        media_producer,
        "render_scene_visual",
        fake_renderer,
    )

    asset = media_producer._visual_job(
        SimpleNamespace(),
        {"id": "s_rendered", "voice_text": "Test narration."},
        {
            "asset_strategy": "rendered",
            "visual_type": "diagram",
            "visual_description": "A simple process diagram",
            "on_screen_text": [],
            "gen_prompt": None,
            "sub_duration_ratio": 1.0,
        },
        tmp_path,
        [],
        1,
    )

    assert rendered_paths == ["diagram"]
    assert asset["source"] == "Internal renderer"
    assert asset["rights_status"] == "project_generated"
    assert Path(asset["path"]).is_file()


def test_semantic_unknown_asset_strategy_does_not_call_renderer(
    tmp_path, monkeypatch
):
    render_called = False

    def unexpected_renderer(*_args, **_kwargs):
        nonlocal render_called
        render_called = True
        raise AssertionError("Unknown strategy must not invoke renderer.")

    monkeypatch.setattr(
        media_producer,
        "render_scene_visual",
        unexpected_renderer,
    )

    with pytest.raises(RuntimeError, match="unsupported asset_strategy"):
        media_producer._visual_job(
            SimpleNamespace(),
            {"id": "s_unknown", "voice_text": "Test narration."},
            {
                "asset_strategy": "renderedd",
                "visual_type": "diagram",
                "visual_description": "Unknown strategy",
                "on_screen_text": [],
                "sub_duration_ratio": 1.0,
            },
            tmp_path,
            [],
            1,
        )

    assert not render_called
