from __future__ import annotations

"""Broad YouTube candidate discovery.

The report keeps API-returned values raw. It does not calculate view velocity,
trend scores, channel-relative ratios, sentiment, or inferred video tags.
Search-stream labels are user-configured query scopes, not automatic video
categorization. Additional derived metrics require YouTube's separate approval.
"""

import datetime as dt
import logging
import re
import uuid
from pathlib import Path
from typing import Any

import requests

from .common import read_json, write_json
from .config import Settings
from .discovery_quota import (
    DEFAULT_DAILY_CEILING, DailyQuotaExceeded, YouTubeSearchQuotaLedger,
)

log = logging.getLogger(__name__)
API = "https://www.googleapis.com/youtube/v3"

# Search scopes are configured query strings, not video-topic classifications.
# These are the Stage 5.1 initial refined candidates; measure actual search quality
# before treating them as final editorial categories.
TOPIC_STREAMS: tuple[dict[str, str], ...] = (
    {"id": "news", "title": "Новости и события", "query_ru": "новости|события|разбор события|объяснение новости", "query_global": "news|current events|news explained|event analysis"},
    {"id": "technology_science", "title": "Технологии и наука", "query_ru": "технологии|нейросети|программирование|обзор гаджетов|космос|научные открытия", "query_global": "technology|AI explained|software tutorial|gadget review|space exploration|science explained"},
    {"id": "business_economy", "title": "Экономика, бизнес и финансы", "query_ru": "экономика|бизнес|финансы|инвестиции|рынки|предпринимательство", "query_global": "economy|business|finance|investing|markets|entrepreneurship"},
    {"id": "sports", "title": "Спорт", "query_ru": "спорт|футбол|баскетбол|теннис|спортивная аналитика|разбор матча", "query_global": "sports analysis|football analysis|basketball analysis|tennis analysis|sports explained"},
    {"id": "gaming", "title": "Игры и гейминг", "query_ru": "игры|видеоигры|обзор игр|игровая индустрия|разбор сюжета игры", "query_global": "gaming|video game review|game industry|game story explained"},
    {"id": "entertainment", "title": "Кино, музыка и развлечения", "query_ru": "кино|сериалы|обзор фильма|разбор сюжета|музыкальная индустрия", "query_global": "movies|TV series|film review|story explained|music industry"},
    {"id": "autos_transport", "title": "Автомобили и транспорт", "query_ru": "автомобили|обзор автомобиля|сравнение автомобилей|электромобили|технологии авто", "query_global": "cars|car review|car comparison|electric vehicles|automotive technology"},
    {"id": "travel_food_lifestyle", "title": "Путешествия, еда и образ жизни", "query_ru": "путешествия|страны|города|достопримечательности|кулинария|история еды", "query_global": "travel|countries|cities|landmarks|cooking|food history"},
    {"id": "education_history", "title": "Образование и история", "query_ru": "история|документальный рассказ|научное объяснение|образование|исторические события", "query_global": "history|documentary explainer|science explained|education|historical events"},
    {"id": "diy_home", "title": "Дом, ремонт и DIY", "query_ru": "ремонт|инструкция|строительство|устройство техники|дом|сделай сам", "query_global": "home repair|how it works|construction|DIY|home improvement"},
)

WINDOWS_DAYS = (1, 7)
SEARCH_ORDERS = ("viewCount", "date")
DEFAULT_MAX_RESULTS = 50
DEFAULT_MAX_PAGES = 1
DEFAULT_SEARCH_CALL_BUDGET = DEFAULT_DAILY_CEILING
CHART_REGIONS = ("RU", "US")


def _get(path: str, params: dict[str, Any], api_key: str) -> dict[str, Any]:
    response = requests.get(
        f"{API}/{path}", params={**params, "key": api_key}, timeout=35
    )
    if response.status_code >= 400:
        # Do not include request URL: it contains the API key.
        raise RuntimeError(
            f"YouTube API {response.status_code} for {path}: {response.text[:700]}"
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"YouTube API returned an unexpected response for {path}.")
    return payload


def _rfc3339(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso_duration_seconds(value: str | None) -> int | None:
    """Parse ISO-8601 duration only for display/filtering; retain original duration too."""
    if not value:
        return None
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not match:
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _video_record_from_detail(detail: dict[str, Any]) -> dict[str, Any] | None:
    video_id = detail.get("id")
    if not video_id:
        return None
    snippet = detail.get("snippet", {}) or {}
    statistics = detail.get("statistics", {}) or {}
    content = detail.get("contentDetails", {}) or {}
    duration = content.get("duration")
    return {
        "video_id": str(video_id),
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": snippet.get("title", ""),
        "channel_id": snippet.get("channelId", ""),
        "channel_title": snippet.get("channelTitle", ""),
        "published_at": snippet.get("publishedAt", ""),
        "view_count": int(statistics["viewCount"]) if "viewCount" in statistics else None,
        "like_count": int(statistics["likeCount"]) if "likeCount" in statistics else None,
        "comment_count": int(statistics["commentCount"]) if "commentCount" in statistics else None,
        "duration": duration,
        "duration_seconds": _iso_duration_seconds(duration),
        "category_id": snippet.get("categoryId"),
    }


def _search_record(search_item: dict[str, Any]) -> dict[str, Any] | None:
    resource_id = search_item.get("id", {})
    video_id = resource_id.get("videoId") if isinstance(resource_id, dict) else None
    if not video_id:
        return None
    snippet = search_item.get("snippet", {}) or {}
    return {
        "video_id": str(video_id),
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": snippet.get("title", ""),
        "channel_id": snippet.get("channelId", ""),
        "channel_title": snippet.get("channelTitle", ""),
        "published_at": snippet.get("publishedAt", ""),
        "view_count": None,
        "like_count": None,
        "comment_count": None,
        "duration": None,
        "duration_seconds": None,
        "category_id": None,
    }


def _fetch_video_details(video_ids: list[str], api_key: str) -> tuple[dict[str, dict[str, Any]], int]:
    details: dict[str, dict[str, Any]] = {}
    calls = 0
    # videos.list accepts up to 50 IDs in one request.
    for start in range(0, len(video_ids), 50):
        batch = video_ids[start:start + 50]
        if not batch:
            continue
        response = _get(
            "videos",
            {"part": "snippet,statistics,contentDetails", "id": ",".join(batch)},
            api_key,
        )
        calls += 1
        for item in response.get("items", []):
            if isinstance(item, dict) and item.get("id"):
                details[str(item["id"])] = item
    return details, calls


def _load_popular_charts(api_key: str) -> tuple[dict[str, Any], int]:
    """Fetch YouTube's own mostPopular chart for two explicit regions.

    The chart is a supplemental signal only. Official docs describe it as including
    trending music, movies and gaming videos; it is not an all-topic global chart.
    """
    charts: dict[str, Any] = {}
    calls = 0
    for region in CHART_REGIONS:
        payload = _get(
            "videos",
            {"part": "snippet,statistics,contentDetails", "chart": "mostPopular",
             "regionCode": region, "maxResults": 50},
            api_key,
        )
        calls += 1
        videos = []
        for item in payload.get("items", []):
            if isinstance(item, dict):
                record = _video_record_from_detail(item)
                if record:
                    videos.append(record)
        charts[region] = {"region_code": region, "videos": videos}
    return charts, calls


def _merge_candidates(
    sets: list[dict[str, Any]],
    charts: dict[str, Any],
    details: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for result_set in sets:
        for record in result_set["videos"]:
            video_id = record["video_id"]
            if video_id not in candidates:
                candidates[video_id] = dict(record)
                candidates[video_id]["found_in"] = []
            candidates[video_id]["found_in"].append({
                "market": result_set["market"],
                "stream_id": result_set["stream_id"],
                "stream_title": result_set["stream_title"],
                "window_days": result_set["window_days"],
                "order": result_set["order"],
                "query": result_set["query"],
            })
    for chart_region, chart in charts.items():
        for record in chart["videos"]:
            video_id = record["video_id"]
            if video_id not in candidates:
                candidates[video_id] = dict(record)
                candidates[video_id]["found_in"] = []
            candidates[video_id]["found_in"].append({
                "market": f"mostPopular:{chart_region}",
                "stream_id": "youtube_most_popular_chart",
                "stream_title": "YouTube mostPopular chart",
                "window_days": None,
                "order": "youtube_algorithm",
            })
    # Fill each unique candidate from the batched details lookup, retaining the
    # search snippet if a video has disappeared or is no longer accessible.
    for video_id, item in candidates.items():
        detail = details.get(video_id)
        if detail:
            refreshed = _video_record_from_detail(detail)
            if refreshed:
                refreshed["found_in"] = item["found_in"]
                candidates[video_id] = refreshed
    return candidates


def _fmt(value: int | None) -> str:
    return "н/д" if value is None else f"{value:,}".replace(",", " ")


def _make_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Ежедневный обзор популярных видео — YouTube",
        "",
        f"- Сформировано (UTC): `{report['generated_at_utc']}`",
        f"- Источник: {report['source']}",
        f"- Уникальных видео в объединённом списке: **{report['candidate_count']}**",
        f"- Поисковых запросов: **{report['quota_usage']['search_list_calls']}**",
        "",
        "> Поисковые направления ниже — заранее настроенные запросы, а не автоматическая классификация видео.",
        "> Поисковые наборы `viewCount` и `date` показываются отдельно. `viewCount` — общее число просмотров, а `date` — порядок по дате публикации.",
        "> Ни одна из этих сортировок не является скоростью роста или трендовым баллом.",
        "> Это выборка по запросам, а не полный рейтинг всех видео YouTube.",
        "",
    ]

    for result_set in report["search_sets"]:
        region_name = "Россия" if result_set["market"] == "ru" else "Зарубежная выдача (без ограничения региона; приоритет английского языка)"
        lines += [
            f"## {region_name} · {result_set['stream_title']} · {result_set['window_days']} дн. · order={result_set['order']}",
            "",
            f"Поисковый запрос: `{result_set['query']}` · Страниц: {result_set['pages_fetched']} · Видео в этой выборке: {len(result_set['videos'])}",
            "",
            "| Просмотры | Лайки | Комментарии | Дата публикации | Длительность | Канал | Видео |",
            "|---:|---:|---:|---|---|---|---|",
        ]
        shown = result_set["videos"][:10]
        for video in shown:
            title = str(video.get("title", "")).replace("|", "\\|").replace("\n", " ")
            channel = str(video.get("channel_title", "")).replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {_fmt(video.get('view_count'))} | {_fmt(video.get('like_count'))} "
                f"| {_fmt(video.get('comment_count'))} | {video.get('published_at', '')} "
                f"| {video.get('duration') or 'н/д'} | {channel} | [{title}]({video.get('url', '')}) |"
            )
        if not shown:
            lines.append("| — | — | — | — | — | — | Нет результатов |")
        if len(result_set["videos"]) > 10:
            lines.append(f"\nВ JSON-файле сохранены все {len(result_set['videos'])} результатов этого поискового набора; здесь показаны первые 10.")
        if result_set["has_more_pages"]:
            lines.append("\nУ API оставались следующие страницы; достигнут настроенный лимит страниц.")
        lines.append("")

    lines += ["## Отдельный чарт YouTube mostPopular", "",
              "Это собственная подборка YouTube. По документации она охватывает прежде всего музыкальные, кино- и игровые видео, поэтому не заменяет тематический поиск.", ""]
    for region, chart in report["most_popular_charts"].items():
        lines += [f"### Регион {region}", "", "| Просмотры | Дата публикации | Длительность | Канал | Видео |", "|---:|---|---|---|---|"]
        for video in chart["videos"][:25]:
            title = str(video.get("title", "")).replace("|", "\\|").replace("\n", " ")
            channel = str(video.get("channel_title", "")).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {_fmt(video.get('view_count'))} | {video.get('published_at', '')} | {video.get('duration') or 'н/д'} | {channel} | [{title}]({video.get('url', '')}) |")
        lines.append("")

    lines += [
        "## Как читать отчёт и ограничения",
        "",
        "- Российская выдача: `regionCode=RU`, языковой приоритет `ru`.",
        "- Зарубежная выдача: `relevanceLanguage=en`, без `regionCode`; это международный поиск с приоритетом английского, а не выборка всех стран или только США.",
        "- Периоды: последние 24 часа и 7 дней; для каждого набора отдельно выполняются `viewCount` и `date`.",
        "- `viewCount` — общее число просмотров; `date` — сортировка по дате публикации. Поиск не показывает скорость прироста просмотров.",
        "- Выдача YouTube по разным сортировкам и временным фильтрам может быть неполной; это выборки по запросам, не исчерпывающий рейтинг.",
        "- Повторения по направлениям и периодам объединяются по `video_id` в `unique_candidates`; в `found_in` сохраняется происхождение результата.",
        "- Скорость роста, трендовые баллы, отношения просмотров/лайков, автоматические тематические теги и анализ комментариев не рассчитываются.",
        "- В отчёт включены сырые счётчики и названия из API. Их требуется обновить или удалить в течение 30 календарных дней.",
        "- Дневной локальный потолок `search.list` — 90 попыток на Pacific-день для основного Google Cloud Project; 10 вызовов оставляются в резерве от стандартного лимита 100.",
        "- Журнал квоты учитывает попытку до HTTP-запроса, включая неуспешные вызовы. Запросы из Cloud Console, другого компьютера или приложения нужно вручную внести через `discover-quota --external-search-calls N`.",
        "- Стандартный поиск Stage 5.1: 10 направлений × 2 рынка × 2 окна × 2 порядка = 80 первых страниц; по умолчанию одна страница до 50 видео.",
        "",
    ]
    return "\n".join(lines)


def purge_expired_discovery_output(out_dir: Path, *, now: dt.datetime | None = None) -> bool:
    """Delete this module's raw API report after its 30-day refresh deadline."""
    json_path = out_dir / "discovery.json"
    if not json_path.is_file():
        return False
    try:
        report = read_json(json_path)
        deadline = report.get("refresh_or_delete_by_utc")
        if not deadline:
            return False
        deadline_dt = dt.datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
        current = now or dt.datetime.now(dt.timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        expired = current.astimezone(dt.timezone.utc) >= deadline_dt.astimezone(dt.timezone.utc)
    except (OSError, ValueError, TypeError):
        return False
    if not expired:
        return False
    json_path.unlink(missing_ok=True)
    (out_dir / "discovery.md").unlink(missing_ok=True)
    (out_dir / "topics.json").unlink(missing_ok=True)
    (out_dir / "topics.md").unlink(missing_ok=True)
    (out_dir / "editor.html").unlink(missing_ok=True)
    (out_dir / "editorial.json").unlink(missing_ok=True)
    return True


def discover_youtube(
    settings: Settings,
    out_dir: Path,
    *,
    query: str | None = None,
    region: str = "RU",
    language: str = "ru",
    max_results: int = DEFAULT_MAX_RESULTS,
    max_pages: int = DEFAULT_MAX_PAGES,
    search_call_budget: int = DEFAULT_SEARCH_CALL_BUDGET,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Search both API orders and enforce a persistent project-wide local ceiling.

    Standard run: 10 streams × 2 markets × 2 windows × 2 orders = 80 first
    pages. The default returns up to 50 results per page and does not paginate.
    Extra pages are optional and use only the remaining daily allowance.
    """
    if not settings.youtube_api_key:
        raise RuntimeError("Не задан YOUTUBE_API_KEY. Поиск использует официальный YouTube Data API v3.")
    if not 1 <= int(max_results) <= 50:
        raise ValueError("max_results должен быть в диапазоне от 1 до 50")
    if not 1 <= int(max_pages) <= 3:
        raise ValueError("max_pages должен быть в диапазоне от 1 до 3")
    if not 1 <= int(search_call_budget) <= 100:
        raise ValueError("search_call_budget должен быть в диапазоне от 1 до 100")
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    generated_at = _rfc3339(now)
    expires_at = _rfc3339(now + dt.timedelta(days=30))

    if query is not None and not str(query).strip():
        raise ValueError("Пользовательский поисковый запрос не должен быть пустым")
    streams = ([{"id": "custom", "title": "Пользовательский запрос",
                 "query_ru": query.strip(), "query_global": query.strip()}]
               if query and query.strip() else list(TOPIC_STREAMS))
    markets = [
        {"id": "ru", "region_code": region.upper().strip(),
         "language": language.strip(), "query_key": "query_ru"},
        {"id": "global", "region_code": None,
         "language": "en", "query_key": "query_global"},
    ]
    planned_first_pages = len(WINDOWS_DAYS) * len(markets) * len(streams) * len(SEARCH_ORDERS)
    if planned_first_pages > int(search_call_budget):
        raise ValueError(
            f"Лимит запуска {search_call_budget} меньше числа обязательных первых страниц "
            f"({planned_first_pages}). Уменьшите набор поиска или увеличьте --search-call-budget."
        )

    ledger = YouTubeSearchQuotaLedger(settings.data_dir / "usage" / "youtube_search_quota.json")
    quota_before = ledger.status(now=now)
    if quota_before["remaining"] < planned_first_pages:
        raise DailyQuotaExceeded(
            f"Для выбранного поиска нужно {planned_first_pages} первых страниц, но в локальном "
            f"журнале осталось только {quota_before['remaining']} вызовов из {quota_before['daily_ceiling']} "
            f"за Pacific-день {quota_before['pacific_date']}. Новые запросы не отправлялись. "
            "Проверьте Google Cloud Quotas и/или выполните discover-quota --external-search-calls N, "
            "если были внешние обращения."
        )

    run_id = uuid.uuid4().hex
    search_calls = 0
    search_sets: list[dict[str, Any]] = []
    all_search_records: dict[str, dict[str, Any]] = {}
    budget_exhausted = False

    def perform_search(result_set: dict[str, Any], page_token: str | None = None) -> str | None:
        nonlocal search_calls
        context = {
            "market": result_set["market"],
            "stream_id": result_set["stream_id"],
            "window_days": result_set["window_days"],
            "order": result_set["order"],
            "page_number": result_set["pages_fetched"] + 1,
        }
        call_id = ledger.reserve_attempt(run_id=run_id, context=context, now=now)
        search_calls += 1
        params: dict[str, Any] = {
            "part": "snippet", "type": "video", "q": result_set["query"],
            "maxResults": int(max_results), "order": result_set["order"],
            "publishedAfter": result_set["published_after"],
            "relevanceLanguage": result_set["language_priority"],
            "safeSearch": "moderate",
        }
        if result_set["region_code"]:
            params["regionCode"] = result_set["region_code"]
        if page_token:
            params["pageToken"] = page_token
        try:
            response = _get("search", params, settings.youtube_api_key)
        except Exception as exc:
            error_text = str(exc)
            reason = next((name for name in (
                "quotaExceeded", "dailyLimitExceeded", "keyInvalid",
                "accessNotConfigured", "forbidden", "backendError",
            ) if name in error_text), None)
            ledger.finish_attempt(call_id, "failed", error_reason=reason, now=now)
            raise
        else:
            ledger.finish_attempt(call_id, "succeeded", now=now)
        result_set["pages_fetched"] += 1
        if result_set["api_total_results"] is None:
            result_set["api_total_results"] = (response.get("pageInfo") or {}).get("totalResults")
        seen_ids = result_set.setdefault("_seen_ids", set())
        for item in response.get("items", []):
            if not isinstance(item, dict):
                continue
            record = _search_record(item)
            if not record:
                continue
            video_id = record["video_id"]
            if video_id in seen_ids:
                continue
            seen_ids.add(video_id)
            result_set["videos"].append(record)
            all_search_records.setdefault(video_id, record)
        return response.get("nextPageToken")

    # Phase 1: complete all 80 (or 8 custom-query) first pages before spending
    # anything on pagination. This prevents page 2 of early sets crowding out
    # first pages of later streams/orders.
    for days in WINDOWS_DAYS:
        published_after = _rfc3339(now - dt.timedelta(days=days))
        for market in markets:
            for stream in streams:
                q = stream[market["query_key"]]
                for order in SEARCH_ORDERS:
                    result_set: dict[str, Any] = {
                        "market": market["id"], "region_code": market["region_code"],
                        "language_priority": market["language"],
                        "stream_id": stream["id"], "stream_title": stream["title"],
                        "query": q, "window_days": days, "order": order,
                        "published_after": published_after, "api_total_results": None,
                        "pages_fetched": 0, "has_more_pages": False, "videos": [],
                        "_seen_ids": set(), "_next_page_token": None,
                    }
                    token = perform_search(result_set)
                    result_set["_next_page_token"] = token
                    result_set["has_more_pages"] = bool(token)
                    search_sets.append(result_set)

    # Phase 2: optional pagination. Only spend calls remaining after all first
    # pages, up to max_pages and both the per-run and persistent daily ceilings.
    for _page_no in range(2, int(max_pages) + 1):
        made_progress = False
        for result_set in search_sets:
            token = result_set.get("_next_page_token")
            if not token:
                continue
            quota_now = ledger.status(now=now)
            if search_calls >= int(search_call_budget) or quota_now["remaining"] <= 0:
                budget_exhausted = True
                break
            next_token = perform_search(result_set, token)
            result_set["_next_page_token"] = next_token
            result_set["has_more_pages"] = bool(next_token)
            made_progress = True
        if budget_exhausted or not made_progress:
            break

    for result_set in search_sets:
        result_set.pop("_seen_ids", None)
        result_set.pop("_next_page_token", None)

    details, detail_calls = _fetch_video_details(list(all_search_records), settings.youtube_api_key)
    for result_set in search_sets:
        for index, record in enumerate(result_set["videos"]):
            detail = details.get(record["video_id"])
            if detail:
                rich = _video_record_from_detail(detail)
                if rich:
                    result_set["videos"][index] = rich

    charts, chart_calls = _load_popular_charts(settings.youtube_api_key)
    unique_candidates = _merge_candidates(search_sets, charts, details)
    quota_after = ledger.status(now=now)

    report: dict[str, Any] = {
        "schema_version": "2.0",
        "source": "YouTube Data API v3",
        "generated_at_utc": generated_at,
        "refresh_or_delete_by_utc": expires_at,
        "search_design": {
            "markets": [
                {"id": "ru", "region_code": region.upper().strip(), "relevance_language": language.strip()},
                {"id": "global", "region_code": None, "relevance_language": "en"},
            ],
            "topic_streams": streams,
            "window_days": list(WINDOWS_DAYS),
            # Backward-compatible legacy field; `orders` is authoritative for Stage 5.1.
            "order": "viewCount",
            "orders": list(SEARCH_ORDERS),
            "max_results_per_page": int(max_results),
            "max_pages_per_search": int(max_pages),
            "search_call_budget": int(search_call_budget),
            "search_call_budget_per_run": int(search_call_budget),
            "daily_project_ceiling": DEFAULT_DAILY_CEILING,
            "quota_ledger_timezone": "America/Los_Angeles",
        },
        "search_sets": search_sets,
        "most_popular_charts": charts,
        "unique_candidates": list(unique_candidates.values()),
        "candidate_count": len(unique_candidates),
        "search_budget_exhausted": budget_exhausted,
        "quota_usage": {
            "search_list_calls": search_calls,
            "search_list_quota_units": search_calls,
            "search_list_calls_remaining_today": quota_after["remaining"],
            "search_list_daily_accounting": quota_after,
            "videos_list_calls": detail_calls + chart_calls,
            "videos_list_detail_calls": detail_calls,
            "videos_list_chart_calls": chart_calls,
            "videos_list_quota_units": detail_calls + chart_calls,
            "note": "search.list uses its separate daily Search Queries bucket; the local application ceiling is 90 calls per America/Los_Angeles day, including externally reconciled usage.",
        },
        "notes": [
            "Search-stream names identify preconfigured search requests; they are not automatically inferred video tags.",
            "Each query is separately searched with API order=viewCount and order=date; the two selections are not merged into a custom score.",
            "viewCount is total views, not view velocity; date is publication-date ordering, not a popularity measure.",
            "International search has no regionCode and uses English as relevanceLanguage priority, not as a language filter.",
            "The mostPopular chart is supplemental and not a full all-topic trending list.",
            "Non-Authorized Data must be refreshed or deleted within 30 calendar days.",
            "Quota ledger accounts local search.list attempts before HTTP and does not see other apps/computers unless reconciled manually.",
        ],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "discovery.json", report)
    (out_dir / "discovery.md").write_text(_make_markdown(report), encoding="utf-8")
    return report

