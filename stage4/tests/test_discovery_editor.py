import datetime as dt
import json
from pathlib import Path

import pytest

from kadr_farm.discovery_editor import build_review_page


def _report(videos=None, *, deadline="2026-12-02T10:00:00Z"):
    return {
        "schema_version": "2.0",
        "generated_at_utc": "2026-11-02T10:00:00Z",
        "refresh_or_delete_by_utc": deadline,
        "candidate_count": len(videos or []),
        "unique_candidates": videos or [
            {
                "video_id": "video-a", "title": "Первое видео", "channel_title": "Канал A",
                "published_at": "2026-11-02T09:00:00Z", "view_count": 1200,
                "like_count": 12, "comment_count": 3, "duration": "PT45S",
                "duration_seconds": 45, "category_id": "20",
                "found_in": [{"market": "ru", "stream_id": "technology_science",
                              "stream_title": "Технологии и наука", "window_days": 1}],
            },
            {
                "video_id": "video-b", "title": "Второе видео", "channel_title": "Канал B",
                "published_at": "2026-11-01T09:00:00Z", "view_count": 800,
                "like_count": 8, "comment_count": 2, "duration": "PT8M",
                "duration_seconds": 480, "category_id": "28",
                "found_in": [{"market": "global", "stream_id": "education_history",
                              "stream_title": "Образование и история", "window_days": 7}],
            },
        ],
    }


def _now():
    return dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc)


def test_generates_offline_page_with_all_unique_candidates(tmp_path):
    src = tmp_path / "discovery.json"
    src.write_text(json.dumps(_report()), encoding="utf-8")
    out = tmp_path / "editor.html"
    result = build_review_page(src, out, now=_now())
    html = out.read_text(encoding="utf-8")

    assert result["candidate_count"] == 2
    assert "video-a" in html and "video-b" in html
    assert "AI Video Factory RU" in html
    assert "Импортировать отбор" in html
    assert "Экспортировать отбор JSON" in html
    assert "https://www.youtube.com/watch?v=" in html
    assert "cdn." not in html.lower()
    assert "fetch(" not in html


def test_rejects_expired_source(tmp_path):
    src = tmp_path / "discovery.json"
    src.write_text(json.dumps(_report(deadline="2026-11-01T00:00:00Z")), encoding="utf-8")
    with pytest.raises(ValueError, match="Срок обновления или удаления"):
        build_review_page(src, tmp_path / "editor.html", now=_now())
    assert not (tmp_path / "editor.html").exists()


def test_rejects_wrong_schema(tmp_path):
    src = tmp_path / "discovery.json"
    src.write_text(json.dumps({"schema_version": "1.0", "unique_candidates": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version 2.0"):
        build_review_page(src, tmp_path / "editor.html", now=_now())


def test_escapes_script_terminators_in_api_text(tmp_path):
    source = _report(videos=[{
        "video_id": "safe-id", "title": "</script><script>alert(1)</script>",
        "channel_title": "Channel", "found_in": [], "duration_seconds": 90,
    }])
    src = tmp_path / "discovery.json"
    src.write_text(json.dumps(source), encoding="utf-8")
    build_review_page(src, tmp_path / "editor.html", now=_now())
    html = (tmp_path / "editor.html").read_text(encoding="utf-8")
    assert "</script><script>alert(1)</script>" not in html
    assert "\\u003c/script\\u003e" in html


def test_deduplicates_video_ids(tmp_path):
    item = _report()["unique_candidates"][0]
    src = tmp_path / "discovery.json"
    src.write_text(json.dumps(_report(videos=[item, item])), encoding="utf-8")
    result = build_review_page(src, tmp_path / "editor.html", now=_now())
    assert result["candidate_count"] == 1


def test_cleanup_also_removes_stage4_outputs_after_source_deadline(tmp_path):
    import datetime as dt
    from kadr_farm.discovery import purge_expired_discovery_output

    source = tmp_path / "discovery.json"
    source.write_text(json.dumps({"refresh_or_delete_by_utc": "2026-11-01T00:00:00Z"}), encoding="utf-8")
    for name in ("discovery.md", "topics.json", "topics.md", "editor.html", "editorial.json"):
        (tmp_path / name).write_text("expired report data", encoding="utf-8")
    keep = tmp_path / "keep.txt"
    keep.write_text("not part of reports", encoding="utf-8")
    now = dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc)

    assert purge_expired_discovery_output(tmp_path, now=now) is True
    for name in ("discovery.json", "discovery.md", "topics.json", "topics.md", "editor.html", "editorial.json"):
        assert not (tmp_path / name).exists()
    assert keep.read_text(encoding="utf-8") == "not part of reports"
