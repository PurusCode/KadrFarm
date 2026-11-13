import datetime as dt
import json
import re
from pathlib import Path

import pytest

from kadr_farm.discovery_editor import build_review_page


PROFILE_PATH = Path(__file__).resolve().parents[1] / "config" / "production_profiles.json"


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
                "found_in": [
                    {"market": "ru", "stream_id": "technology_science", "stream_title": "Технологии и наука",
                     "window_days": 1, "order": "viewCount", "query": "локальные нейросети"},
                    {"market": "global", "stream_id": "technology_science", "stream_title": "Technology",
                     "window_days": 7, "order": "date", "query": "local AI"},
                ],
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


def _build(tmp_path, report=None, *, deadline=None):
    src = tmp_path / "discovery.json"
    payload = report or _report(deadline=deadline or "2026-12-02T10:00:00Z")
    src.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "editor.html"
    result = build_review_page(src, out, now=_now(), profiles_path=PROFILE_PATH)
    return result, out.read_text(encoding="utf-8")


def test_generates_offline_page_with_all_unique_candidates(tmp_path):
    result, html = _build(tmp_path)
    assert result["candidate_count"] == 2
    assert "video-a" in html and "video-b" in html
    assert "AI Video Factory RU" in html
    assert "Импортировать отбор" in html
    assert "Экспортировать отбор JSON" in html
    assert "https://www.youtube.com/watch?v=" in html
    assert "cdn." not in html.lower()
    assert "fetch(" not in html


def test_source_order_and_query_are_preserved_for_filtering(tmp_path):
    _, html = _build(tmp_path)
    match = re.search(r'<script type="application/json" id="source-data">(.*?)</script>', html, re.S)
    assert match
    embedded = json.loads(match.group(1))
    contexts = embedded["videos"][0]["found_in"]
    assert contexts[0]["order"] == "viewCount"
    assert contexts[0]["query"] == "локальные нейросети"
    assert contexts[1]["order"] == "date"
    assert "orderFilter" in html and "viewCount" in html and "production_fit" in html


def test_manual_production_fields_and_catalog_are_embedded(tmp_path):
    _, html = _build(tmp_path)
    static_fields = ("reference_format", "production_profile", "production_fit", "blockers", "asset_strategy",
                     "rights_status", "fact_check_status", "budget_status")
    for field in static_fields:
        assert f'data-field="{field}"' in html
    # The five safety flags are rendered from flagFields at runtime, so assert
    # both the data-driven template and every allowed key rather than a static
    # HTML attribute that does not exist until JavaScript renders the card.
    for field in ("human_presence_required", "human_voice_required", "field_filming_required",
                  "physical_experiment_required", "guest_required"):
        assert field in html
    assert 'data-field="${key}"' in html and "const flagFields = [" in html
    assert "Интервью" in html and "Объясняющий ролик" in html
    assert "Все редакторские метки ниже назначаются человеком вручную" in html


def test_legacy_stage4_import_schema_is_supported_without_losing_old_fields(tmp_path):
    _, html = _build(tmp_path)
    assert "['1.0','1.1'].includes(data.schema_version)" in html
    for key in ("decision", "editor_note", "language", "format_preference", "reviewed", "themes"):
        assert key in html
    assert "schema_version:'1.1'" in html
    assert "schema_version:'1.0'" not in html.split("function exportEditorial")[1].split("function importEditorial")[0]


def test_new_export_import_contract_validates_manual_enum_values(tmp_path):
    _, html = _build(tmp_path)
    for marker in ("safeEnum(a.reference_format", "safeEnum(a.production_profile", "safeArray(a.blockers", "safeArray(a.asset_strategy",
                   "safeEnum(a.rights_status", "safeEnum(a.fact_check_status", "safeEnum(a.budget_status"):
        assert marker in html
    assert "eligibleForSuitableNow" in html
    assert "productionBlockers = new Set" in html
    assert "a.blockers.some(key=>productionBlockers.has(key))" in html
    assert "multiField==='blockers'&&current.production_fit==='suitable_now'" in html
    assert "Не назначается автоматически" not in html



def test_multi_value_editor_fields_use_clickable_checkboxes_not_native_select(tmp_path):
    _, html = _build(tmp_path)
    assert '<select multiple' not in html
    assert 'class="multi-checks" data-field="blockers"' in html
    assert 'class="multi-checks" data-field="asset_strategy"' in html
    assert 'data-multi-field="${esc(field)}"' in html
    assert 'type="checkbox" data-multi-field=' in html
    assert 'input[data-multi-field="${multiField}"]:checked' in html
    assert 'setAnnotation(id,{[multiField]:value})' in html


def test_blocker_checkbox_guard_preserves_existing_selection_when_fit_is_suitable_now(tmp_path):
    _, html = _build(tmp_path)
    assert "multiField==='blockers'&&current.production_fit==='suitable_now'" in html
    assert "e.target.checked=false" in html
    assert "Нельзя добавить этот блокер, пока тема отмечена «Подходит сейчас»." in html

def test_filters_include_order_profile_and_fit(tmp_path):
    _, html = _build(tmp_path)
    for filter_id in ("orderFilter", "profileFilter", "fitFilter"):
        assert f'id="{filter_id}"' in html
        assert f"$(' {filter_id}')" not in html
        assert filter_id in html


def test_old_reports_without_order_are_mapped_to_unknown(tmp_path):
    report = _report(videos=[{"video_id": "legacy", "title": "Old", "found_in": [{"market": "ru"}]}])
    _, html = _build(tmp_path, report)
    match = re.search(r'<script type="application/json" id="source-data">(.*?)</script>', html, re.S)
    embedded = json.loads(match.group(1))
    assert embedded["videos"][0]["found_in"][0]["order"] == "unknown"
    assert embedded["videos"][0]["found_in"][0]["query"] == ""


def test_rejects_expired_source(tmp_path):
    src = tmp_path / "discovery.json"
    src.write_text(json.dumps(_report(deadline="2026-11-01T00:00:00Z")), encoding="utf-8")
    with pytest.raises(ValueError, match="Срок обновления или удаления"):
        build_review_page(src, tmp_path / "editor.html", now=_now(), profiles_path=PROFILE_PATH)
    assert not (tmp_path / "editor.html").exists()


def test_rejects_wrong_schema(tmp_path):
    src = tmp_path / "discovery.json"
    src.write_text(json.dumps({"schema_version": "1.0", "unique_candidates": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version 2.0"):
        build_review_page(src, tmp_path / "editor.html", now=_now(), profiles_path=PROFILE_PATH)


def test_escapes_script_terminators_in_api_text(tmp_path):
    source = _report(videos=[{
        "video_id": "safe-id", "title": "</script><script>alert(1)</script>",
        "channel_title": "Channel", "found_in": [], "duration_seconds": 90,
    }])
    _, html = _build(tmp_path, source)
    assert "</script><script>alert(1)</script>" not in html
    assert "\u003c/script\u003e" in html


def test_deduplicates_video_ids(tmp_path):
    item = _report()["unique_candidates"][0]
    result, _ = _build(tmp_path, _report(videos=[item, item]))
    assert result["candidate_count"] == 1


def test_cleanup_also_removes_stage_outputs_after_source_deadline(tmp_path):
    from kadr_farm.discovery import purge_expired_discovery_output
    source = tmp_path / "discovery.json"
    source.write_text(json.dumps({"refresh_or_delete_by_utc": "2026-11-01T00:00:00Z"}), encoding="utf-8")
    for name in ("discovery.md", "topics.json", "topics.md", "editor.html", "editorial.json"):
        (tmp_path / name).write_text("expired report data", encoding="utf-8")
    keep = tmp_path / "keep.txt"
    keep.write_text("not part of reports", encoding="utf-8")
    assert purge_expired_discovery_output(tmp_path, now=_now()) is True
    for name in ("discovery.json", "discovery.md", "topics.json", "topics.md", "editor.html", "editorial.json"):
        assert not (tmp_path / name).exists()
    assert keep.read_text(encoding="utf-8") == "not part of reports"


def test_requires_stage51_production_profile_catalog(tmp_path):
    src = tmp_path / "discovery.json"
    src.write_text(json.dumps(_report()), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="production_profiles.json"):
        build_review_page(src, tmp_path / "editor.html", now=_now(), profiles_path=tmp_path / "missing.json")
