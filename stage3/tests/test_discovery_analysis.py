import json
from pathlib import Path

import pytest

from kadr_farm.discovery_analysis import analyze_discovery
from kadr_farm.discovery import purge_expired_discovery_output


def _report():
    return {
        "schema_version": "2.0",
        "generated_at_utc": "2026-11-02T10:00:00Z",
        "refresh_or_delete_by_utc": "2026-12-02T10:00:00Z",
        "unique_candidates": [
            {"video_id": "a", "url": "https://youtube.com/watch?v=a", "title": "A", "channel_title": "One", "published_at": "2026-11-02T09:00:00Z", "view_count": 900, "like_count": 10, "comment_count": 1, "duration": "PT45S", "duration_seconds": 45, "category_id": "20", "found_in": [{"market": "ru", "stream_id": "sports", "window_days": 1}]},
            {"video_id": "b", "url": "https://youtube.com/watch?v=b", "title": "B", "channel_title": "Two", "published_at": "2026-11-02T08:00:00Z", "view_count": 500, "like_count": 8, "comment_count": 2, "duration": "PT5M", "duration_seconds": 300, "category_id": "20", "found_in": [{"market": "global", "stream_id": "gaming", "window_days": 7}, {"market": "ru", "stream_id": "gaming", "window_days": 7}]},
            {"video_id": "c", "url": "https://youtube.com/watch?v=c", "title": "C", "channel_title": "Three", "published_at": "2026-11-01T08:00:00Z", "view_count": None, "like_count": None, "comment_count": None, "duration": None, "duration_seconds": None, "category_id": None, "found_in": []},
        ],
    }


def test_analyze_discovery_groups_by_provenance_duration_and_official_category(tmp_path):
    src = tmp_path / "discovery.json"
    src.write_text(json.dumps(_report()), encoding="utf-8")
    out = tmp_path / "analysis"
    result = analyze_discovery(src, out)

    assert result["candidate_count"] == 3
    assert result["method"]["topic_inference"] is False
    assert result["duration_groups"]["under_60_seconds"]["candidate_count"] == 1
    assert result["duration_groups"]["60_seconds_or_more"]["candidate_count"] == 1
    assert result["duration_groups"]["duration_unknown"]["candidate_count"] == 1
    assert result["youtube_category_groups"]["20"]["candidate_count"] == 2
    assert len([g for g in result["search_context_groups"] if g["search_stream_id"] == "gaming"]) == 2
    assert (out / "topics.json").is_file()
    assert "не обязательно являются Shorts" in (out / "topics.md").read_text(encoding="utf-8")


def test_rejects_wrong_schema_and_invalid_limit(tmp_path):
    src = tmp_path / "discovery.json"
    src.write_text(json.dumps({"schema_version": "1.0", "unique_candidates": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version 2.0"):
        analyze_discovery(src, tmp_path / "out")
    with pytest.raises(ValueError, match="1 до 100"):
        analyze_discovery(src, tmp_path / "out", limit=0)


def test_missing_file_is_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="Не найден"):
        analyze_discovery(tmp_path / "missing.json", tmp_path / "out")


def test_cleanup_also_removes_stage3_reports_after_source_deadline(tmp_path):
    source = tmp_path / "discovery.json"
    source.write_text(json.dumps({
        "refresh_or_delete_by_utc": "2026-11-01T00:00:00Z"
    }), encoding="utf-8")
    for name in ("discovery.md", "topics.json", "topics.md"):
        (tmp_path / name).write_text("raw API-derived report", encoding="utf-8")
    keep = tmp_path / "unrelated.txt"
    keep.write_text("keep", encoding="utf-8")

    import datetime as dt
    now = dt.datetime(2026, 11, 2, tzinfo=dt.timezone.utc)
    assert purge_expired_discovery_output(tmp_path, now=now) is True
    assert not source.exists()
    assert not (tmp_path / "discovery.md").exists()
    assert not (tmp_path / "topics.json").exists()
    assert not (tmp_path / "topics.md").exists()
    assert keep.read_text(encoding="utf-8") == "keep"
