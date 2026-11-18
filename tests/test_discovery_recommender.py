import datetime as dt
import json
from pathlib import Path

import pytest

from kadr_farm.config import Settings
from kadr_farm.discovery import purge_expired_discovery_output
from kadr_farm.discovery_recommender import (
    _evidence_summary,
    _preselect_candidates,
    _review_topic_production_fit,
    recommend_discovery_topics,
)


def _settings(tmp_path):
    return Settings(
        root=tmp_path, data_dir=tmp_path / "data", output_dir=tmp_path / "output",
        cache_dir=tmp_path / "cache", broll_dir=tmp_path / "broll",
        images_dir=tmp_path / "images", audio_dir=tmp_path / "audio",
        ffmpeg="ffmpeg", ffprobe="ffprobe", width=1920, height=1080, fps=25,
        bitrate="4500k", gop=50, youtube_api_key="test", pexels_api_key="",
        pixabay_api_key="", tts_engine="kokoro", tts_language="ru",
        tts_speaker="dima", tts_speed=0.9, providers=[],
    )


def _write_source(tmp_path, *, candidate_count=6, generated="2026-11-18T10:00:00Z"):
    candidates = []
    for number in range(candidate_count):
        candidates.append({
            "video_id": f"video-{number}",
            "url": f"https://www.youtube.com/watch?v=video-{number}",
            "title": f"Заголовок о необычном сюжете номер {number}",
            "channel_id": f"channel-{number % 3}",
            "channel_title": f"Канал {number % 3}",
            "published_at": f"2026-11-{18 - (number % 7):02d}T08:00:00Z",
            "view_count": 1000 + number,
            "like_count": 30 + number,
            "comment_count": 5 + number,
            "duration": "PT5M",
            "duration_seconds": 300,
            "category_id": "28",
            "found_in": [{
                "market": "ru", "stream_id": "technology_science",
                "stream_title": "Технологии и наука", "window_days": 1 + (number % 2) * 6,
                "order": "viewCount" if number % 2 == 0 else "date",
            }],
        })
    report = {
        "schema_version": "2.0",
        "generated_at_utc": generated,
        "refresh_or_delete_by_utc": "2026-12-18T10:00:00Z",
        "candidate_count": candidate_count,
        "unique_candidates": candidates,
    }
    source_json = tmp_path / "discovery.json"
    source_md = tmp_path / "discovery.md"
    source_json.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    source_md.write_text(
        "# Discovery\n"
        f"- Сформировано (UTC): `{generated}`\n"
        f"- Уникальных видео в объединённом списке: **{candidate_count}**\n",
        encoding="utf-8",
    )
    return source_json, source_md


class _FakeRouter:
    def __init__(self, settings, *, honor_provider_timeouts=False):
        self.calls = []

    def json(self, system_prompt, user_prompt, *, temperature=0.2, max_tokens=7000):
        self.calls.append(user_prompt)
        if "СПИСОК ТЕМ И ИСХОДНЫХ ВИДЕО (JSON):" in user_prompt:
            payload = json.loads(
                user_prompt.split("СПИСОК ТЕМ И ИСХОДНЫХ ВИДЕО (JSON):\n", 1)[1]
            )
            reviews = []
            for row in payload:
                reviews.append({
                    "topic_id": row["topic_id"],
                    "coherence": {
                        "decision": "approve",
                        "shared_specific_subject": "Один конкретный общий сюжет для теста",
                        "all_sources_support_same_subject": True,
                        "coherent_video_ids": row["evidence_video_ids"],
                        "reason": "Все источники тестовой темы согласованы.",
                    },
                    "farm_feasibility": {
                        "rating": "high",
                        "recommended_format": "Дикторское объяснение с авторской презентацией",
                        "narration_core": "Вся существенная информация раскрывается голосом диктора.",
                        "presentation_visuals": ["Схема и таймлайн"],
                        "stock_photo_needs": ["Иллюстративные фотографии по теме"],
                        "stock_video_needs": ["Общий тематический B-roll"],
                        "unavailable_visuals": ["Уникальные кадры не гарантируются"],
                        "production_risks": ["Проверить точность иллюстраций"],
                        "adaptation_idea": "Адаптация не требуется.",
                    },
                })
            return {"reviews": reviews}
        if "ДАННЫЕ ПАКЕТА (JSON):" in user_prompt:
            payload = json.loads(user_prompt.split("ДАННЫЕ ПАКЕТА (JSON):\n", 1)[1])
            ids = [item["id"] for item in payload]
            return {"clusters": [{
                "theme": "Повторяющийся сюжет",
                "connection": "Общие признаки заголовков пакета",
                "evidence_video_ids": ids[:2],
            }]} if len(ids) >= 2 else {"clusters": []}
        return {
            "recommendations": [
                {
                    "theme": "Тема первая",
                    "original_idea": "Оригинальный объясняющий ролик на основе источников",
                    "audience_hook": "Что стоит знать зрителю?",
                    "why_recommended": "Несколько отдельных видео раскрывают близкий вопрос.",
                    "evidence_video_ids": ["video-0", "video-1"],
                    "story_directions": ["Контекст", "Причины", "Практические выводы"],
                    "fact_check_risks": ["Проверить исходные утверждения"],
                },
                {
                    "theme": "Тема вторая",
                    "original_idea": "Второй оригинальный угол раскрытия",
                    "audience_hook": "Как это работает?",
                    "why_recommended": "Есть отдельная группа источников.",
                    "evidence_video_ids": ["video-2", "video-3", "fake-id"],
                    "story_directions": ["История", "Сравнение", "Итоги"],
                    "fact_check_risks": [],
                },
                {
                    "theme": "Тема третья",
                    "original_idea": "Третья самостоятельная идея",
                    "audience_hook": "Почему это важно?",
                    "why_recommended": "Повторяющийся интерес в исходной выборке.",
                    "evidence_video_ids": ["video-4", "video-5"],
                    "story_directions": ["Вопрос", "Разбор", "Вывод"],
                    "fact_check_risks": [],
                },
            ],
            "overall_notes": ["Проверить источники перед сценарием."],
        }


def test_recommendation_reads_all_candidates_in_batches_and_writes_two_reports(tmp_path, monkeypatch):
    source_json, source_md = _write_source(tmp_path)
    router = _FakeRouter(None)

    class RouterFactory:
        def __new__(cls, settings, *, honor_provider_timeouts=False):
            return router

    monkeypatch.setattr("kadr_farm.discovery_recommender.LLMRouter", RouterFactory)
    result = recommend_discovery_topics(
        _settings(tmp_path), source_json, source_md, tmp_path / "out",
        limit=10, batch_size=2,
        now=dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.timezone.utc),
    )

    assert result["method"]["candidate_count_total"] == 6
    assert result["method"]["candidate_count_selected"] == 6
    assert result["method"]["candidate_count_analyzed"] == 6
    assert result["method"]["preselection"]["selected_unique_count"] == 6
    assert result["method"]["batch_size"] == 2
    assert result["method"]["batch_count"] == 3
    assert result["method"]["channel_scope"] == "universal_all_categories_no_fixed_niche"
    assert len(router.calls) == 5  # three batches + final ranking + one KadrFarm audit
    assert "ПРОИЗВОДИМОСТЬЮ СРЕДСТВАМИ KadrFarm" in router.calls[-1]
    assert result["farm_review"]["performed"] is True
    assert result["farm_review"]["approved_count"] == 3
    assert result["farm_review"]["llm_call_count"] == 1
    assert all(item["production_status"] == "ready" for item in result["recommendations"])
    assert all(item["farm_feasibility"]["narration_core"] for item in result["recommendations"])
    assert "УНИВЕРСАЛЬНЫЙ" in router.calls[-2]
    assert "Не предпочитай ИИ/технологии" in router.calls[-2]
    assert "одного канала" in router.calls[0]
    assert "УНИВЕРСАЛЬНЫЙ" in router.calls[-2]
    assert "Не предпочитай ИИ/технологии" in router.calls[-2]
    assert "несколько роликов одного канала" in router.calls[0]
    assert result["recommendation_count"] == 3
    assert all(len(item["evidence"]["evidence_videos"]) >= 2 for item in result["recommendations"])
    assert all("fake-id" not in [v["video_id"] for v in item["evidence"]["evidence_videos"]]
               for item in result["recommendations"])
    assert (tmp_path / "out" / "recommendations.json").is_file()
    markdown = (tmp_path / "out" / "recommendations.md").read_text(encoding="utf-8")
    assert "Тема первая" in markdown
    assert "редакторская оценка LLM" in markdown
    assert "Производимость средствами KadrFarm" in markdown
    assert "Основа дикторского рассказа" in markdown
    assert "стокового B-roll" in markdown


def test_evidence_summary_counts_independent_channels_and_search_windows():
    by_id = {
        "a": {
            "video_id": "a", "title": "A", "channel_id": "ch-1", "channel_title": "Канал 1",
            "published_at": "2026-11-18T08:00:00Z", "view_count": 1000, "url": "https://youtube.com/a",
            "found_in": [
                {"market": "ru", "stream_id": "news", "window_days": 1, "order": "viewCount"},
                {"market": "ru", "stream_id": "news", "window_days": 7, "order": "date"},
            ],
        },
        "b": {
            "video_id": "b", "title": "B", "channel_id": "ch-2", "channel_title": "Канал 2",
            "published_at": "2026-11-17T08:00:00Z", "view_count": 900, "url": "https://youtube.com/b",
            "found_in": [{"market": "ru", "stream_id": "business", "window_days": 7, "order": "viewCount"}],
        },
        "c": {
            "video_id": "c", "title": "C", "channel_id": "ch-1", "channel_title": "Канал 1",
            "published_at": "2026-11-16T08:00:00Z", "view_count": 800, "url": "https://youtube.com/c",
            "found_in": [{"market": "ru", "stream_id": "news", "window_days": 7, "order": "date"}],
        },
        "d": {
            "video_id": "d", "title": "D", "channel_id": "ch-3", "channel_title": "Канал 3",
            "published_at": "2026-11-18T09:00:00Z", "view_count": 700, "url": "https://youtube.com/d",
            "found_in": [{"market": "ru", "stream_id": "business", "window_days": 1, "order": "date"}],
        },
    }
    evidence = _evidence_summary(["a", "b", "c", "d", "a"], by_id)
    assert evidence["supporting_video_count"] == 4
    assert evidence["independent_channel_count"] == 3
    assert evidence["largest_single_channel_video_count"] == 2
    assert evidence["source_independence"] == "multiple_channels"
    assert evidence["evidence_breadth"] == "moderate"
    assert evidence["videos_in_24h_search"] == 2
    assert evidence["videos_in_7d_search"] == 3
    assert evidence["videos_in_both_search_windows"] == 1
    assert evidence["videos_in_viewCount_results"] == 2
    assert evidence["videos_in_date_results"] == 3
    assert evidence["search_streams"] == ["business", "news"]


def test_preselection_takes_top_four_per_search_order_and_up_to_ten_ru_chart_videos():
    candidates = []
    # Ten search candidates appear in both viewCount and date results.
    # The viewCount leaders are v9..v6; the date leaders are v0..v3.
    for number in range(10):
        published_day = 18 - number if number < 4 else 5 - (number - 4)
        candidates.append({
            "video_id": f"v{number}",
            "title": f"Видео {number}",
            "channel_id": f"ch{number % 4}",
            "channel_title": f"Канал {number % 4}",
            "published_at": f"2026-11-{published_day:02d}T10:00:00Z",
            "view_count": number * 100,
            "url": f"https://youtube.com/watch?v=v{number}",
            "found_in": [
                {"market": "ru", "stream_id": "stream_a", "window_days": 1, "order": "viewCount"},
                {"market": "ru", "stream_id": "stream_a", "window_days": 1, "order": "date"},
            ],
        })

    # Ten separate videos come from the Russian mostPopular chart.
    for number in range(10, 20):
        candidates.append({
            "video_id": f"v{number}",
            "title": f"Чарт {number}",
            "channel_id": f"chart-ch{number % 5}",
            "channel_title": f"Чарт-канал {number % 5}",
            "published_at": "2026-11-18T09:00:00Z",
            "view_count": 5000 - number,
            "url": f"https://youtube.com/watch?v=v{number}",
            "found_in": [{
                "market": "ru", "stream_id": "youtube_most_popular_chart",
                "window_days": None, "order": "chart",
            }],
        })

    report = {
        "most_popular_charts": {
            "RU": {"videos": [{"video_id": f"v{number}"} for number in range(10, 20)]}
        }
    }
    selected, audit = _preselect_candidates(candidates, report)
    selected_ids = [item["video_id"] for item in selected]

    assert len(selected_ids) == 18
    assert len(selected_ids) == len(set(selected_ids))
    assert set(selected_ids[:8]) == {"v0", "v1", "v2", "v3", "v6", "v7", "v8", "v9"}
    assert selected_ids[8:] == [f"v{number}" for number in range(10, 20)]
    assert audit["search_set_count"] == 2
    assert audit["search_selected_unique_count"] == 8
    assert audit["chart_added_unique_count"] == 10
    assert audit["selected_unique_count"] == 18


def test_russian_40_search_sets_produce_at_most_170_unique_llm_candidates():
    candidates = []
    for stream_number in range(10):
        for window in (1, 7):
            for order in ("viewCount", "date"):
                for rank in range(5):
                    video_id = f"s{stream_number:02d}-w{window}-{order}-{rank}"
                    candidates.append({
                        "video_id": video_id,
                        "title": video_id,
                        "channel_id": f"channel-{rank}",
                        "channel_title": f"Канал {rank}",
                        "published_at": f"2026-11-{18-rank:02d}T10:00:00Z",
                        "view_count": 10000 - rank,
                        "url": f"https://youtube.com/watch?v={video_id}",
                        "found_in": [{
                            "market": "ru", "stream_id": f"stream_{stream_number}",
                            "window_days": window, "order": order,
                        }],
                    })
    for number in range(10):
        video_id = f"chart-{number}"
        candidates.append({
            "video_id": video_id, "title": video_id, "channel_id": f"chart-{number}",
            "channel_title": f"Чарт {number}", "published_at": "2026-11-18T09:00:00Z",
            "view_count": 50000-number, "url": f"https://youtube.com/watch?v={video_id}",
            "found_in": [{
                "market": "ru", "stream_id": "youtube_most_popular_chart",
                "window_days": None, "order": "chart",
            }],
        })
    report = {
        "most_popular_charts": {
            "RU": {"videos": [{"video_id": f"chart-{number}"} for number in range(10)]}
        }
    }

    selected, audit = _preselect_candidates(candidates, report)

    assert audit["search_set_count"] == 40
    assert audit["search_selected_unique_count"] == 160
    assert audit["chart_added_unique_count"] == 10
    assert audit["selected_unique_count"] == 170
    assert len(selected) == 170
    assert all(len(item["selected_video_ids"]) == 4 for item in audit["search_sets"])


def test_preselection_deduplicates_same_video_across_search_sets_and_chart():
    shared = {
        "video_id": "shared", "title": "Общее видео", "channel_id": "channel-1",
        "channel_title": "Канал 1", "published_at": "2026-11-18T10:00:00Z",
        "view_count": 500, "url": "https://youtube.com/watch?v=shared",
        "found_in": [
            {"market": "ru", "stream_id": "stream_a", "window_days": 1, "order": "viewCount"},
            {"market": "ru", "stream_id": "stream_b", "window_days": 7, "order": "date"},
            {"market": "ru", "stream_id": "youtube_most_popular_chart", "window_days": None, "order": "chart"},
        ],
    }
    other = {
        "video_id": "other", "title": "Другое видео", "channel_id": "channel-2",
        "channel_title": "Канал 2", "published_at": "2026-11-17T10:00:00Z",
        "view_count": 400, "url": "https://youtube.com/watch?v=other",
        "found_in": [{"market": "ru", "stream_id": "stream_a", "window_days": 1, "order": "viewCount"}],
    }
    report = {"most_popular_charts": {"RU": {"videos": [{"video_id": "shared"}]}}}
    selected, audit = _preselect_candidates([shared, other], report)
    ids = [item["video_id"] for item in selected]
    assert ids == ["shared", "other"]
    assert len(ids) == len(set(ids))
    assert audit["chart_added_unique_count"] == 0


def test_farm_review_separates_ready_conditional_low_and_incoherent_topics():
    class ReviewRouter:
        def json(self, system_prompt, user_prompt, *, temperature=0.2, max_tokens=7000):
            rows = json.loads(
                user_prompt.split("СПИСОК ТЕМ И ИСХОДНЫХ ВИДЕО (JSON):\n", 1)[1]
            )
            ratings = {"T1": "high", "T2": "conditional", "T3": "low", "T4": "high"}
            output = []
            for row in rows:
                topic_id = row["topic_id"]
                reject = topic_id == "T4"
                output.append({
                    "topic_id": topic_id,
                    "coherence": {
                        "decision": "reject" if reject else "approve",
                        "shared_specific_subject": "" if reject else f"Узкий сюжет {topic_id}",
                        "all_sources_support_same_subject": not reject,
                        "coherent_video_ids": [] if reject else row["evidence_video_ids"],
                        "reason": "Источники о разных сюжетах." if reject else "Сюжет подтверждён.",
                    },
                    "farm_feasibility": {
                        "rating": ratings[topic_id],
                        "recommended_format": "Объясняющий видеоролик",
                        "narration_core": "Полное объяснение закадровым рассказом.",
                        "presentation_visuals": ["Схема"],
                        "stock_photo_needs": ["Фотографии"],
                        "stock_video_needs": ["B-roll"],
                        "unavailable_visuals": ["Эксклюзивные кадры"],
                        "production_risks": ["Проверить доступность стоков"],
                        "adaptation_idea": "Перестроить подачу в объясняющий формат.",
                    },
                })
            return {"reviews": output}

    topics = []
    by_id = {}
    for index, rating in enumerate(("high", "conditional", "low", "high"), 1):
        video_a, video_b = f"v{index}a", f"v{index}b"
        topic = {
            "rank": index,
            "theme": f"Тема {index}",
            "original_idea": f"Идея {index}",
            "evidence": {"video_ids": [video_a, video_b]},
        }
        topics.append(topic)
        by_id[video_a] = {"video_id": video_a, "title": "Общий сюжет A", "channel_id": f"ch{index}a", "channel_title": f"Канал {index}A"}
        by_id[video_b] = {"video_id": video_b, "title": "Общий сюжет B", "channel_id": f"ch{index}b", "channel_title": f"Канал {index}B"}

    result = _review_topic_production_fit(ReviewRouter(), topics, by_id)
    assert [item["theme"] for item in result["approved"]] == ["Тема 1", "Тема 2"]
    assert result["approved"][0]["production_status"] == "ready"
    assert result["approved"][1]["production_status"] == "adaptation_required"
    assert [item["theme"] for item in result["not_suitable"]] == ["Тема 3"]
    assert [item["theme"] for item in result["coherence_rejections"]] == ["Тема 4"]
    assert result["reviewed_count"] == 4
    assert result["llm_call_count"] == 1


def test_rejects_mismatched_markdown_pair_before_calling_llm(tmp_path, monkeypatch):
    source_json, source_md = _write_source(tmp_path)
    source_md.write_text(
        "# Discovery\n- Сформировано (UTC): `2026-11-17T10:00:00Z`\n"
        "- Уникальных видео в объединённом списке: **6**\n",
        encoding="utf-8",
    )

    def unexpected_router(*args, **kwargs):
        raise AssertionError("LLM не должна вызываться для несогласованной пары файлов")

    monkeypatch.setattr("kadr_farm.discovery_recommender.LLMRouter", unexpected_router)
    with pytest.raises(ValueError, match="относятся к разным запускам"):
        recommend_discovery_topics(
            _settings(tmp_path), source_json, source_md, tmp_path / "out",
            now=dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.timezone.utc),
        )


def test_rejects_expired_source_data(tmp_path, monkeypatch):
    source_json, source_md = _write_source(tmp_path)
    monkeypatch.setattr(
        "kadr_farm.discovery_recommender.LLMRouter",
        lambda *args, **kwargs: pytest.fail("LLM не должна запускаться для просроченных данных"),
    )
    with pytest.raises(ValueError, match="Срок хранения"):
        recommend_discovery_topics(
            _settings(tmp_path), source_json, source_md, tmp_path / "out",
            now=dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc),
        )


def test_cleanup_removes_recommendation_reports_after_source_deadline(tmp_path):
    (tmp_path / "discovery.json").write_text(
        json.dumps({"refresh_or_delete_by_utc": "2026-11-01T00:00:00Z"}),
        encoding="utf-8",
    )
    for name in ("discovery.md", "recommendations.json", "recommendations.md"):
        (tmp_path / name).write_text("YouTube-derived content", encoding="utf-8")
    now = dt.datetime(2026, 11, 18, tzinfo=dt.timezone.utc)
    assert purge_expired_discovery_output(tmp_path, now=now) is True
    assert not (tmp_path / "recommendations.json").exists()
    assert not (tmp_path / "recommendations.md").exists()
