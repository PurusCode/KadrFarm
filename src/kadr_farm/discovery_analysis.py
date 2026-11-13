from __future__ import annotations

"""Editorial views over a saved Discovery v2 report.

This module does not infer topics, language, sentiment, virality, or trends.
It only reorganizes API-returned fields and the search provenance already
recorded by Discovery v2. Official YouTube category IDs are retained as-is.
"""

import json
from pathlib import Path
from typing import Any

from .common import read_json, write_json

SCHEMA_VERSION = "1.0"


def _format_bucket(seconds: int | None) -> str:
    if seconds is None:
        return "duration_unknown"
    return "under_60_seconds" if seconds < 60 else "60_seconds_or_more"


def _market_label(value: str) -> str:
    if value == "ru":
        return "RU search context (not a language guarantee)"
    if value == "global":
        return "Global search context (English relevance priority, not a language filter)"
    if value.startswith("mostPopular:"):
        return value
    return value or "unknown"


def analyze_discovery(source: Path, out_dir: Path, *, limit: int = 20) -> dict[str, Any]:
    """Create review lists from Discovery v2 without inferred classifications."""
    if not 1 <= int(limit) <= 100:
        raise ValueError("limit должен быть в диапазоне от 1 до 100")
    if not source.is_file():
        raise FileNotFoundError(f"Не найден файл Discovery: {source}")
    report = read_json(source)
    if not isinstance(report, dict) or not isinstance(report.get("unique_candidates"), list):
        raise ValueError("Ожидался discovery.json со списком unique_candidates")
    if str(report.get("schema_version", "")) != "2.0":
        raise ValueError("Поддерживается discovery.json schema_version 2.0")

    candidates = report["unique_candidates"]
    by_id: dict[str, dict[str, Any]] = {}
    for item in candidates:
        if isinstance(item, dict) and item.get("video_id"):
            by_id[str(item["video_id"])] = item

    # Search provenance is used as-is. No title/content-based topic inference.
    context_groups: dict[tuple[str, str, int | None], list[dict[str, Any]]] = {}
    official_categories: dict[str, list[dict[str, Any]]] = {}
    formats: dict[str, list[dict[str, Any]]] = {
        "under_60_seconds": [], "60_seconds_or_more": [], "duration_unknown": []
    }
    for video in by_id.values():
        bucket = _format_bucket(video.get("duration_seconds"))
        formats[bucket].append(video)
        category = video.get("category_id")
        if category not in (None, ""):
            official_categories.setdefault(str(category), []).append(video)
        contexts = video.get("found_in") or []
        for context in contexts:
            if not isinstance(context, dict):
                continue
            key = (str(context.get("market") or "unknown"),
                   str(context.get("stream_id") or "unknown"),
                   context.get("window_days"))
            context_groups.setdefault(key, []).append(video)

    # YouTube API search sets are already returned in viewCount order. Keep their
    # first-seen order; do not calculate a new score or ratio here.
    def ordered_unique(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result, seen = [], set()
        for item in items:
            vid = str(item.get("video_id", ""))
            if vid and vid not in seen:
                seen.add(vid)
                result.append(item)
        return result

    def compact(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "video_id": item.get("video_id"), "url": item.get("url"),
            "title": item.get("title"), "channel_title": item.get("channel_title"),
            "published_at": item.get("published_at"), "view_count": item.get("view_count"),
            "like_count": item.get("like_count"), "comment_count": item.get("comment_count"),
            "duration": item.get("duration"), "duration_seconds": item.get("duration_seconds"),
            "category_id": item.get("category_id"), "found_in": item.get("found_in", []),
        }

    groups = []
    for (market, stream_id, window_days), items in sorted(
        context_groups.items(), key=lambda x: (x[0][0], x[0][1], x[0][2] or 0)
    ):
        unique = ordered_unique(items)
        groups.append({
            "market_context": _market_label(market), "market": market,
            "search_stream_id": stream_id, "window_days": window_days,
            "candidate_count": len(unique),
            "videos": [compact(v) for v in unique[:limit]],
        })

    format_groups = {
        key: {"candidate_count": len(items), "videos": [compact(v) for v in items[:limit]]}
        for key, items in formats.items()
    }
    category_groups = {
        key: {"youtube_category_id": key, "candidate_count": len(items),
              "videos": [compact(v) for v in items[:limit]]}
        for key, items in sorted(official_categories.items())
    }

    result = {
        "schema_version": SCHEMA_VERSION,
        "source_file": source.name,
        "source_schema_version": report.get("schema_version"),
        "source_generated_at_utc": report.get("generated_at_utc"),
        "source_refresh_or_delete_by_utc": report.get("refresh_or_delete_by_utc"),
        "candidate_count": len(by_id),
        "method": {
            "description": "Editorial review lists built only from API-returned fields and recorded search provenance.",
            "topic_inference": False,
            "language_inference": False,
            "trend_or_velocity_metrics": False,
            "ranking": "Preserves first-seen order from YouTube search sets, which use order=viewCount.",
            "short_video_threshold_seconds": 60,
            "category_source": "YouTube API snippet.categoryId; IDs are not replaced with inferred labels.",
        },
        "search_context_groups": groups,
        "duration_groups": format_groups,
        "youtube_category_groups": category_groups,
        "notes": [
            "RU and global identify search contexts, not verified video languages or creator locations.",
            "Under 60 seconds is a duration bucket, not a claim that the video is a YouTube Short.",
            "Search stream labels are configured query provenance, not inferred subject classifications.",
            "No custom topic clusters, trend scores, view velocity, ratios, or sentiment are calculated.",
            "Raw YouTube API data retains the source report's refresh/delete deadline.",
        ],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "topics.json", result)
    (out_dir / "topics.md").write_text(render_markdown(result), encoding="utf-8")
    return result


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Discovery Stage 3 — редакторские подборки",
        "",
        f"- Видео в исходной выборке: **{result['candidate_count']}**",
        f"- Исходный отчёт сформирован (UTC): `{result.get('source_generated_at_utc')}`",
        f"- Обновить или удалить исходные API-данные до: `{result.get('source_refresh_or_delete_by_utc')}`",
        "",
        "> Это не автоматическое определение трендов или семантических тем. Ниже сгруппированы исходные видео по контексту поискового запроса, официальной категории YouTube и длительности.",
        "> RU/global — контексты поисковых запросов, а не подтверждённый язык ролика. Ролики короче 60 секунд не обязательно являются Shorts.",
        "",
        "## Подборки по контексту поиска",
        "",
    ]
    for group in result["search_context_groups"]:
        lines += [f"### {group['market_context']} · {group['search_stream_id']} · {group['window_days']} дн.",
                  f"Видео в группе: {group['candidate_count']}", "",
                  "| Просмотры | Дата | Длительность | Канал | Видео |",
                  "|---:|---|---|---|---|"]
        for v in group["videos"]:
            title = str(v.get("title") or "").replace("|", "\\|").replace("\n", " ")
            channel = str(v.get("channel_title") or "").replace("|", "\\|").replace("\n", " ")
            views = "н/д" if v.get("view_count") is None else f"{v['view_count']:,}".replace(",", " ")
            lines.append(f"| {views} | {v.get('published_at') or 'н/д'} | {v.get('duration') or 'н/д'} | {channel} | [{title}]({v.get('url') or ''}) |")
        lines.append("")

    lines += ["## Разделение по длительности", ""]
    for key, group in result["duration_groups"].items():
        lines += [f"### {key} — {group['candidate_count']} видео", "", "| Просмотры | Длительность | Канал | Видео |", "|---:|---|---|---|"]
        for v in group["videos"]:
            title = str(v.get("title") or "").replace("|", "\\|").replace("\n", " ")
            channel = str(v.get("channel_title") or "").replace("|", "\\|").replace("\n", " ")
            views = "н/д" if v.get("view_count") is None else f"{v['view_count']:,}".replace(",", " ")
            lines.append(f"| {views} | {v.get('duration') or 'н/д'} | {channel} | [{title}]({v.get('url') or ''}) |")
        lines.append("")

    lines += ["## Официальные категории YouTube", "",
              "Категории показаны по исходному `category_id`; названия и дополнительные темы не выводятся без отдельного справочника.", ""]
    for key, group in result["youtube_category_groups"].items():
        lines.append(f"- `category_id={key}`: {group['candidate_count']} видео")
    lines += ["", "## Методика и ограничения", ""]
    lines.extend(f"- {note}" for note in result["notes"])
    return "\n".join(lines) + "\n"
