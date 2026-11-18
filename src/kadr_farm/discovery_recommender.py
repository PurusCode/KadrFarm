from __future__ import annotations

"""LLM-backed editorial recommendations from a saved YouTube Discovery report.

All unique candidate titles are reviewed in bounded batches. The model proposes
semantic clusters, then consolidates and ranks original editorial ideas. The
report deliberately does not calculate view velocity, trend scores, or ratios.
"""

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from .common import read_json, write_json
from .config import Settings
from .llm import LLMRouter


CLUSTER_SYSTEM = """Ты редактор-аналитик универсального русскоязычного YouTube-канала. У канала НЕТ постоянной ниши и НЕТ заранее предпочтительных категорий. Рассматривай на равных все темы, которые подтверждаются данными: новости и события, общество, экономика, политика, работа и деньги, технологии, наука, история, спорт, игры, кино, музыка, автомобили, путешествия, еда, животные, дом, ремонт, природа, происшествия и любые другие сюжеты. Не отдавай автоматическое предпочтение ИИ/технологиям и не исключай никакую категорию по тематике. Анализируй только переданные записи и заголовки, не делай вид, что смотрел видео. Выделяй конкретные повторяющиеся сюжеты, а не широкие категории. Не копируй названия. Не придумывай факты, события, цифры или содержание роликов. Не выводи скорость роста, трендовые баллы, рейтинги популярности или отношения метрик. Верни только корректный JSON."""

FINAL_SYSTEM = """Ты главный редактор УНИВЕРСАЛЬНОГО русскоязычного YouTube-канала. Канал не ограничен одной нишей: темы будущих роликов выбираются по интересу аудитории в текущий момент, а не по постоянной категории канала. Все категории рассматривай на равных; не продвигай ИИ/технологии только потому, что они часто встречаются в обучающих примерах. На основе подтверждённых кластеров реальных видео выбери самые интересные и актуальные сюжеты текущего среза YouTube Discovery. Не копируй заголовки. Не выдавай тему заголовка за проверенное содержание видео. Не придумывай события, факты, цифры и цитаты. Приоритет — редакторская оценка с прозрачными основаниями, а не математический трендовый балл. Не рассчитывай скорость роста просмотров, viral score или отношения метрик. Верни только корректный JSON."""


def _parse_utc(value: Any, field: str) -> dt.datetime:
    if not value:
        raise ValueError(f"В discovery.json отсутствует поле {field}")
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Некорректная дата {field}: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Дата {field} должна содержать часовой пояс")
    return parsed.astimezone(dt.timezone.utc)


def _validate_source_pair(source_json: Path, source_md: Path, now: dt.datetime) -> dict[str, Any]:
    if not source_json.is_file():
        raise FileNotFoundError(f"Не найден файл Discovery JSON: {source_json}")
    if not source_md.is_file():
        raise FileNotFoundError(f"Не найден файл Discovery Markdown: {source_md}")

    report = read_json(source_json)
    if not isinstance(report, dict) or str(report.get("schema_version")) != "2.0":
        raise ValueError("Поддерживается только discovery.json со schema_version 2.0")

    generated = _parse_utc(report.get("generated_at_utc"), "generated_at_utc")
    deadline = _parse_utc(report.get("refresh_or_delete_by_utc"), "refresh_or_delete_by_utc")
    if now.tzinfo is None:
        raise ValueError("now должен содержать часовой пояс")
    if now.astimezone(dt.timezone.utc) >= deadline:
        raise ValueError(
            "Срок хранения исходных YouTube API Data истёк. "
            "Сначала обновите Discovery командой discover."
        )

    markdown = source_md.read_text(encoding="utf-8")
    date_match = re.search(r"^- Сформировано \(UTC\): `([^`]+)`\s*$", markdown, re.MULTILINE)
    count_match = re.search(
        r"^- Уникальных видео в объединённом списке: \*\*(\d+)\*\*\s*$",
        markdown,
        re.MULTILINE,
    )
    if not date_match or date_match.group(1) != str(report.get("generated_at_utc")):
        raise ValueError(
            "discovery.md и discovery.json относятся к разным запускам "
            "или Markdown не содержит ожидаемые метаданные. Пересоздайте Discovery."
        )

    candidates = report.get("unique_candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("В discovery.json отсутствует непустой список unique_candidates")
    actual_count = sum(
        1 for item in candidates
        if isinstance(item, dict) and str(item.get("video_id") or "").strip()
    )
    if count_match and int(count_match.group(1)) != actual_count:
        raise ValueError(
            "Количество видео в discovery.md не совпадает с discovery.json. "
            "Пересоздайте пару отчётов командой discover."
        )
    declared_count = report.get("candidate_count")
    if declared_count is not None and int(declared_count) != actual_count:
        raise ValueError("Поле candidate_count не совпадает с unique_candidates в discovery.json")
    report["_validated_generated_at"] = generated.isoformat().replace("+00:00", "Z")
    report["_validated_deadline"] = deadline.isoformat().replace("+00:00", "Z")
    return report


def _compact_context(context: Any) -> dict[str, Any] | None:
    if not isinstance(context, dict):
        return None
    return {
        "market": str(context.get("market") or "unknown"),
        "stream_id": str(context.get("stream_id") or "unknown"),
        "stream_title": str(context.get("stream_title") or context.get("stream_id") or ""),
        "window_days": context.get("window_days"),
        "order": str(context.get("order") or "unknown"),
    }


def _prepare_candidates(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Prepare every unique candidate; do not sample away the long tail."""
    prepared: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in report["unique_candidates"]:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("video_id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not video_id or not title or video_id in seen:
            continue
        seen.add(video_id)
        contexts: list[dict[str, Any]] = []
        context_keys: set[tuple[Any, ...]] = set()
        for raw_context in item.get("found_in") or []:
            context = _compact_context(raw_context)
            if context is None:
                continue
            key = (
                context["market"], context["stream_id"], context["window_days"],
                context["order"],
            )
            if key not in context_keys:
                context_keys.add(key)
                contexts.append(context)
        prepared.append({
            "video_id": video_id,
            "title": title,
            "channel_id": str(item.get("channel_id") or ""),
            "channel_title": str(item.get("channel_title") or ""),
            "published_at": item.get("published_at"),
            "view_count": item.get("view_count"),
            "like_count": item.get("like_count"),
            "comment_count": item.get("comment_count"),
            "duration": item.get("duration"),
            "duration_seconds": item.get("duration_seconds"),
            "url": str(item.get("url") or f"https://www.youtube.com/watch?v={video_id}"),
            "found_in": contexts,
        })
    if not prepared:
        raise ValueError("В discovery.json нет видео с video_id и title")
    return prepared


def _numeric_view_count(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _published_timestamp(value: Any) -> float | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _preselect_candidates(
    candidates: list[dict[str, Any]],
    report: dict[str, Any],
    *,
    per_search_set: int = 4,
    chart_limit: int = 10,
    max_search_candidates: int = 160,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select a balanced, bounded sample from every search set plus RU chart.

    For each (market, stream, window, order) set, take up to four records:
    highest view_count for viewCount order, newest published_at for date order.
    De-duplicate by video_id across all sets, then add up to ten RU chart videos.
    A round-robin across sets keeps the hard cap fair if both markets are enabled.
    """
    if per_search_set < 1 or chart_limit < 0 or max_search_candidates < 1:
        raise ValueError("Некорректные параметры предварительного отбора")

    by_id = {item["video_id"]: item for item in candidates}
    eligible_by_context: dict[tuple[str, str, int, str], list[dict[str, Any]]] = {}
    for item in candidates:
        for context in item.get("found_in") or []:
            if not isinstance(context, dict):
                continue
            stream_id = str(context.get("stream_id") or "unknown")
            order = str(context.get("order") or "unknown")
            market = str(context.get("market") or "unknown")
            window = context.get("window_days")
            if stream_id == "youtube_most_popular_chart":
                continue
            if order not in {"viewCount", "date"} or not isinstance(window, int):
                continue
            key = (market, stream_id, window, order)
            eligible_by_context.setdefault(key, []).append(item)

    ranked_sets: list[tuple[tuple[str, str, int, str], list[dict[str, Any]]]] = []
    for key in sorted(eligible_by_context, key=lambda value: (value[1], value[2], value[3], value[0])):
        order = key[3]
        rows = eligible_by_context[key]
        if order == "viewCount":
            rows = sorted(
                rows,
                key=lambda item: (
                    _numeric_view_count(item.get("view_count")) is not None,
                    _numeric_view_count(item.get("view_count")) or -1,
                    _published_timestamp(item.get("published_at")) or -1,
                    item["video_id"],
                ),
                reverse=True,
            )
        else:
            rows = sorted(
                rows,
                key=lambda item: (
                    _published_timestamp(item.get("published_at")) is not None,
                    _published_timestamp(item.get("published_at")) or -1,
                    _numeric_view_count(item.get("view_count")) or -1,
                    item["video_id"],
                ),
                reverse=True,
            )
        ranked_sets.append((key, rows[:per_search_set]))

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    search_set_audit: list[dict[str, Any]] = []
    search_selected_ids: list[str] = []

    # Round-robin by rank: for RU this yields up to 4 per each of 40 sets;
    # for a larger two-market run it avoids filling the cap with early sets.
    for rank in range(per_search_set):
        for key, rows in ranked_sets:
            if len(search_selected_ids) >= max_search_candidates:
                break
            if rank >= len(rows):
                continue
            item = rows[rank]
            video_id = item["video_id"]
            if video_id in selected_ids:
                continue
            selected_ids.add(video_id)
            selected.append(item)
            search_selected_ids.append(video_id)
        if len(search_selected_ids) >= max_search_candidates:
            break

    for key, rows in ranked_sets:
        selected_for_set = [item["video_id"] for item in rows if item["video_id"] in selected_ids]
        search_set_audit.append({
            "market": key[0],
            "stream_id": key[1],
            "window_days": key[2],
            "order": key[3],
            "eligible_video_count": len(eligible_by_context[key]),
            "top_candidate_count": len(rows),
            "selected_video_ids": selected_for_set,
        })

    ru_chart = (report.get("most_popular_charts") or {}).get("RU", {})
    chart_rows = ru_chart.get("videos", []) if isinstance(ru_chart, dict) else []
    chart_ids: list[str] = []
    for row in chart_rows if isinstance(chart_rows, list) else []:
        if not isinstance(row, dict):
            continue
        video_id = str(row.get("video_id") or row.get("id") or "").strip()
        if video_id and video_id in by_id and video_id not in chart_ids:
            chart_ids.append(video_id)
        if len(chart_ids) >= chart_limit:
            break

    # Backward-safe fallback: use explicit chart provenance if a report does not
    # include a separate most_popular_charts.RU.videos array.
    if not chart_ids and chart_limit:
        for item in candidates:
            if any(
                isinstance(context, dict)
                and str(context.get("stream_id") or "") == "youtube_most_popular_chart"
                and str(context.get("market") or "").upper() == "RU"
                for context in item.get("found_in") or []
            ):
                chart_ids.append(item["video_id"])
                if len(chart_ids) >= chart_limit:
                    break

    chart_selected_ids: list[str] = []
    for video_id in chart_ids:
        if video_id in selected_ids:
            continue
        selected_ids.add(video_id)
        selected.append(by_id[video_id])
        chart_selected_ids.append(video_id)

    if not selected:
        raise ValueError(
            "Предварительный отбор не нашёл видео с контекстом поискового набора "
            "или российского mostPopular chart. Проверьте структуру discovery.json."
        )

    selection = {
        "strategy": "top_per_search_set_then_ru_most_popular",
        "per_search_set_limit": per_search_set,
        "search_set_count": len(ranked_sets),
        "search_candidate_cap": max_search_candidates,
        "chart_region": "RU",
        "chart_limit": chart_limit,
        "search_selected_unique_count": len(search_selected_ids),
        "chart_available_count": len(chart_ids),
        "chart_added_unique_count": len(chart_selected_ids),
        "selected_unique_count": len(selected),
        "search_sets": search_set_audit,
    }
    return selected, selection


def _json_response(router: LLMRouter, system: str, prompt: str, *, max_tokens: int) -> dict[str, Any]:
    payload = router.json(system, prompt, temperature=0.2, max_tokens=max_tokens)
    if not isinstance(payload, dict):
        raise ValueError("LLM вернула ответ не в формате JSON-объекта")
    return payload


def _cluster_batches(
    router: LLMRouter,
    candidates: list[dict[str, Any]],
    *,
    batch_size: int,
) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    chunks = [candidates[i:i + batch_size] for i in range(0, len(candidates), batch_size)]
    for batch_number, chunk in enumerate(chunks, 1):
        compact_rows = []
        batch_ids = {item["video_id"] for item in chunk}
        for item in chunk:
            compact_rows.append({
                "id": item["video_id"],
                "title": item["title"],
                "channel_id": item["channel_id"],
                "channel": item["channel_title"],
                "published_at": item["published_at"],
                "view_count_raw": item["view_count"],
                "search_contexts": [
                    {
                        "stream": context["stream_id"],
                        "window_days": context["window_days"],
                        "order": context["order"],
                        "market": context["market"],
                    }
                    for context in item["found_in"][:5]
                ],
            })
        data = json.dumps(compact_rows, ensure_ascii=False, separators=(",", ":"))
        prompt = f"""Пакет {batch_number} из {len(chunks)}. В этом пакете {len(chunk)} РАЗНЫХ видео.

Задача — обнаружить конкретные сюжеты, интересные аудитории СЕЙЧАС, в любом тематическом направлении. Канал универсальный, постоянной ниши нет. Рассматривай все категории на равных: общество, новости, экономика, политика, наука, технологии, история, спорт, игры, кино, музыка, автомобили, путешествия, еда, животные, дом, ремонт, природа, происшествия и прочее. Не отдавай предпочтение ИИ или технологиям и не отбрасывай тему из-за её категории.

Объединяй только действительно близкие сюжеты. Не склеивай разные события лишь из-за широкой категории. Учитывай, что несколько роликов одного канала могут быть частями одной серии и не равны независимому подтверждению от разных авторов. Используй даты публикации, исходные просмотры и контекст попадания в выборки как наблюдаемые признаки, но НЕ делай вывод о скорости роста: view_count — общий счётчик на момент сбора. window_days и order показывают контекст выдачи, а не динамику просмотров.

Верни JSON строго такой структуры:
{{"clusters":[{{"theme":"конкретный повторяющийся сюжет","connection":"что конкретно объединяет эти видео","evidence_video_ids":["точные ID из пакета"]}}]}}

Допускается от 0 до 15 кластеров. Если для сюжета есть только один источник, не выдавай его за подтверждённую повторяющуюся тему: можно вернуть предварительную гипотезу с одним ID. Все evidence_video_ids должны принадлежать этому пакету. Не выдумывай ID и не копируй заголовки как название темы.

ДАННЫЕ ПАКЕТА (JSON):
{data}"""
        result = _json_response(router, CLUSTER_SYSTEM, prompt, max_tokens=2800)
        raw_clusters = result.get("clusters", [])
        if not isinstance(raw_clusters, list):
            continue
        for cluster_number, raw in enumerate(raw_clusters[:10], 1):
            if not isinstance(raw, dict):
                continue
            theme = str(raw.get("theme") or "").strip()
            connection = str(raw.get("connection") or "").strip()
            ids = raw.get("evidence_video_ids")
            if not theme or not isinstance(ids, list):
                continue
            valid_ids = list(dict.fromkeys(
                str(value).strip() for value in ids
                if str(value).strip() in batch_ids
            ))
            if not valid_ids:
                continue
            clusters.append({
                "cluster_id": f"b{batch_number}_c{cluster_number}",
                "theme": theme[:180],
                "connection": connection[:700],
                "evidence_video_ids": valid_ids[:20],
            })
    return clusters


def _build_cluster_catalogue(
    clusters: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    catalogue = []
    for cluster in clusters:
        examples = []
        for video_id in cluster["evidence_video_ids"][:5]:
            video = by_id[video_id]
            examples.append({
                "id": video_id,
                "title": video["title"],
                "channel_id": video["channel_id"],
                "channel": video["channel_title"],
                "published_at": video["published_at"],
                "view_count_raw": video["view_count"],
                "search_streams": sorted({c["stream_id"] for c in video["found_in"]}),
            })
        catalogue.append({
            "cluster_id": cluster["cluster_id"],
            "theme": cluster["theme"],
            "connection": cluster["connection"],
            "evidence_video_ids": cluster["evidence_video_ids"],
            "evidence_signals": {key: value for key, value in _evidence_summary(cluster["evidence_video_ids"], by_id).items()
                                 if key not in {"video_ids", "leading_channels", "evidence_videos"}},
            "examples": examples,
        })
    return catalogue


def _evidence_summary(video_ids: list[str], by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Summarize source independence and search-window provenance without trend metrics."""
    selected = [by_id[video_id] for video_id in dict.fromkeys(video_ids) if video_id in by_id]
    channel_counts: dict[str, dict[str, Any]] = {}
    streams: set[str] = set()
    markets: set[str] = set()
    window_counts = {1: 0, 7: 0}
    order_counts = {"viewCount": 0, "date": 0}
    videos_in_both_windows = 0

    for video in selected:
        channel_id = str(video.get("channel_id") or "").strip()
        channel_title = str(video.get("channel_title") or "").strip()
        channel_key = channel_id or channel_title.casefold() or "unknown-channel"
        row = channel_counts.setdefault(
            channel_key,
            {"channel_id": channel_id, "channel_title": channel_title or "Канал не указан", "video_count": 0},
        )
        row["video_count"] += 1

        video_windows: set[int] = set()
        video_orders: set[str] = set()
        for context in video.get("found_in") or []:
            if not isinstance(context, dict):
                continue
            stream = str(context.get("stream_id") or "unknown")
            market = str(context.get("market") or "unknown")
            if stream != "unknown":
                streams.add(stream)
            markets.add(market)
            if context.get("window_days") in (1, 7):
                video_windows.add(int(context["window_days"]))
            order = str(context.get("order") or "unknown")
            if order in order_counts:
                video_orders.add(order)
        for window in video_windows:
            window_counts[window] += 1
        if video_windows == {1, 7}:
            videos_in_both_windows += 1
        for order in video_orders:
            order_counts[order] += 1

    channel_rows = sorted(
        channel_counts.values(),
        key=lambda item: (-item["video_count"], item["channel_title"].casefold()),
    )
    channel_count = len(channel_counts)
    largest_channel_count = max((item["video_count"] for item in channel_rows), default=0)
    if len(selected) >= 5 and channel_count >= 4:
        breadth = "strong"
    elif len(selected) >= 3 and channel_count >= 2:
        breadth = "moderate"
    else:
        breadth = "limited"

    return {
        "video_ids": [video["video_id"] for video in selected],
        "supporting_video_count": len(selected),
        "independent_channel_count": channel_count,
        "largest_single_channel_video_count": largest_channel_count,
        "source_independence": "multiple_channels" if channel_count >= 2 else "single_or_unknown_channel",
        "evidence_breadth": breadth,
        "videos_in_24h_search": window_counts[1],
        "videos_in_7d_search": window_counts[7],
        "videos_in_both_search_windows": videos_in_both_windows,
        "videos_in_viewCount_results": order_counts["viewCount"],
        "videos_in_date_results": order_counts["date"],
        "search_streams": sorted(streams),
        "window_days": [window for window in (1, 7) if window_counts[window]],
        "source_orders": [order for order in ("viewCount", "date") if order_counts[order]],
        "market_contexts": sorted(markets),
        "leading_channels": channel_rows[:5],
        "evidence_videos": [
            {
                "video_id": video["video_id"],
                "title": video["title"],
                "channel_id": video.get("channel_id"),
                "channel_title": video["channel_title"],
                "published_at": video["published_at"],
                "view_count": video["view_count"],
                "url": video["url"],
            }
            for video in selected[:8]
        ],
    }


def _render_markdown(result: dict[str, Any]) -> str:
    method = result["method"]
    selection = method["preselection"]
    review = result["farm_review"]
    lines = [
        "# Автоматические рекомендации тем по YouTube Discovery",
        "",
        f"- Исходный отчёт: `{result['source_json_file']}` + `{result['source_markdown_file']}`",
        f"- Сформирован (UTC): `{result['generated_at_utc']}`",
        f"- Кандидатов в исходном Discovery: **{method['candidate_count_total']}**",
        f"- Отобрано и передано LLM: **{method['candidate_count_analyzed']}**",
        f"- Сокращение входной выборки: **{method['candidate_count_total']} → {method['candidate_count_analyzed']}**",
        f"- Пакетов анализа: **{method['batch_count']} × до {method['batch_size']} видео**",
        f"- Предложено первичным редактором: **{result['proposed_recommendation_count']}**",
        f"- Финальный аудит: **{review['approved_count']} допущено; {review['watchlist_count']} в наблюдение; {review['not_suitable_count']} не подходят ферме; {review['coherence_rejected_count']} отклонено по смысловой целостности**",
        f"- Исходные данные обновить или удалить до: `{result['source_refresh_or_delete_by_utc']}`",
        "",
        "> Канал универсальный: все тематические категории рассматриваются на равных, постоянной ниши нет.",
        "> Приоритеты — редакторская оценка LLM, но каждая тема дополнительно проходит проверку конкретной связи между источниками и производимости средствами KadrFarm.",
        "> KadrFarm: дикторская дорожка, живая презентация, авторские слайды, схемы/диаграммы/карты, фотографии и видеовставки стокового B-roll. Pixabay настроен; Pexels сейчас не настроен.",
        "> Дикторский рассказ полностью передаёт важную информацию. Визуалы усиливают рассказ, но не заменяют объяснение.",
        "> Оценка наличия стоков предварительная: конкретные фото и видео нужно проверить поиском по фотостокам. Никакие материалы не считаются доступными до фактического поиска.",
        "> Это не официальный рейтинг YouTube и не математический трендовый балл. Скорость роста просмотров не рассчитывается.",
        "> Анализ основан на заголовках и метаданных, а не на просмотре исходных видео. Перед сценарием необходимо проверить источники и факты.",
        "",
    ]

    for item in result["recommendations"]:
        evidence = item["evidence"]
        farm = item["farm_feasibility"]
        lines += [
            f"## {item['rank']}. {item['theme']}",
            "",
            f"**Статус:** {item['production_status_label']}",
            f"**Общий конкретный сюжет:** {item['coherence_review']['shared_specific_subject']}",
            f"**Оригинальная идея ролика:** {item['original_idea']}",
            "",
            f"**Обещание зрителю:** {item['audience_hook']}",
            "",
            f"**Почему актуально рекомендовано:** {item['why_recommended']}",
            "",
            (
                f"**Сигналы выборки:** {evidence['supporting_video_count']} уникальных видео; "
                f"{evidence['independent_channel_count']} независимых каналов; "
                f"поиск за 24 часа — {evidence['videos_in_24h_search']}; "
                f"за 7 дней — {evidence['videos_in_7d_search']}; "
                f"сортировка viewCount — {evidence['videos_in_viewCount_results']}; "
                f"сортировка date — {evidence['videos_in_date_results']}; "
                f"ширина подтверждения — `{evidence['evidence_breadth']}`."
            ),
            "",
            "### Производимость средствами KadrFarm",
            f"**Оценка:** `{farm['rating']}` — {item['production_status_label']}.",
            f"**Рекомендуемый формат:** {farm['recommended_format']}",
            f"**Основа дикторского рассказа:** {farm['narration_core']}",
            "",
            "**Что создаём в живой презентации:**",
        ]
        lines.extend(f"- {value}" for value in farm["presentation_visuals"])
        lines += ["", "**Какие фотографии/иллюстрации искать:**"]
        lines.extend(f"- {value}" for value in farm["stock_photo_needs"])
        lines += ["", "**Какие видеовставки искать в фотостоках:**"]
        lines.extend(f"- {value}" for value in farm["stock_video_needs"])
        lines += ["", "**Какие кадры ферма не может гарантировать:**"]
        lines.extend(f"- {value}" for value in farm["unavailable_visuals"])
        lines += ["", "**Производственные риски:**"]
        lines.extend(f"- {value}" for value in farm["production_risks"])
        lines += ["", f"**Адаптация под ферму:** {farm['adaptation_idea']}", ""]

        if item["story_directions"]:
            lines.append("**Возможная структура:**")
            lines.extend(f"- {part}" for part in item["story_directions"])
            lines.append("")
        if item["fact_check_risks"]:
            lines += ["**Факты для проверки:**"]
            lines.extend(f"- {risk}" for risk in item["fact_check_risks"])
            lines.append("")

        lines += ["**Видео-основания:**", "", "| Дата публикации | Просмотры | Канал | Видео |", "|---|---:|---|---|"]
        for video in evidence["evidence_videos"]:
            title = str(video["title"]).replace("|", "\\|").replace("\n", " ")
            channel = str(video["channel_title"] or "н/д").replace("|", "\\|").replace("\n", " ")
            views = "н/д" if video["view_count"] is None else f"{video['view_count']:,}".replace(",", " ")
            lines.append(f"| {video['published_at'] or 'н/д'} | {views} | {channel} | [{title}]({video['url']}) |")
        lines.append("")

    if result["watchlist"]:
        lines += ["## Наблюдать: недостаточно независимых источников", ""]
        for item in result["watchlist"]:
            lines += [f"### {item['theme']}", "", f"**Причина:** {item['coherence_review']['reason']}", ""]
            farm = item["farm_feasibility"]
            lines.append(f"**Производимость:** {farm['rating']} — {farm['recommended_format']}")
            lines.append(f"**Возможная адаптация:** {farm['adaptation_idea']}")
            lines.append("")

    if result["not_suitable_for_farm"]:
        lines += ["## Актуально, но не подходит текущим возможностям фермы", ""]
        for item in result["not_suitable_for_farm"]:
            farm = item["farm_feasibility"]
            lines += [
                f"### {item['theme']}",
                "",
                f"**Почему не допущено:** {farm['recommended_format']}",
                f"**Недоступные визуалы:** {', '.join(farm['unavailable_visuals']) or 'не указаны'}",
                f"**Возможная адаптация:** {farm['adaptation_idea']}",
                "",
            ]

    if result["coherence_rejections"]:
        lines += ["## Отклонённые группировки: источники не подтверждают один конкретный сюжет", ""]
        for item in result["coherence_rejections"]:
            lines += [f"### {item['theme']}", "", f"**Причина:** {item['reason']}", ""]
            for source in item["evidence_videos"]:
                lines.append(f"- {source['channel_title'] or 'Канал не указан'}: {source['title']} (`{source['video_id']}`)")
            lines.append("")

    if not result["recommendations"]:
        lines += [
            "## Нет тем, допущенных к производству",
            "",
            "Это не ошибка: в текущей выборке могли отсутствовать темы, одновременно подтверждённые независимыми источниками и реализуемые средствами фермы. Программа не заполняет рейтинг искусственно.",
            "",
        ]

    lines += ["## Методика и ограничения", ""]
    lines.extend(f"- {note}" for note in result["notes"])
    lines += ["", "## Состав предварительной выборки", ""]
    for row in selection["search_sets"]:
        lines.append(
            f"- {row['market']} / {row['stream_id']} / {row['window_days']} дн. / {row['order']}: "
            f"{len(row['selected_video_ids'])} отобрано из {row['eligible_video_count']} кандидатов"
        )
    return "\n".join(lines) + "\n"


def _review_topic_production_fit(
    router: LLMRouter,
    recommendations: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Audit topic coherence and production feasibility in one bounded LLM call.

    The voiceover must carry the complete explanation. Presentation graphics and
    stock media reinforce it; they must never be the only source of key information.
    """
    if not recommendations:
        return {
            "approved": [],
            "watchlist": [],
            "not_suitable": [],
            "coherence_rejections": [],
            "reviewed_count": 0,
            "llm_call_count": 0,
        }

    topic_rows: list[dict[str, Any]] = []
    topic_by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(recommendations, 1):
        topic_id = f"T{index}"
        evidence_ids = list(dict.fromkeys(item.get("evidence", {}).get("video_ids", [])))
        source_rows = []
        for video_id in evidence_ids:
            video = by_id.get(video_id)
            if video is None:
                continue
            source_rows.append({
                "video_id": video_id,
                "title": video.get("title"),
                "channel_id": video.get("channel_id"),
                "channel_title": video.get("channel_title"),
                "published_at": video.get("published_at"),
                "view_count_raw": video.get("view_count"),
            })
        topic_rows.append({
            "topic_id": topic_id,
            "theme": item.get("theme"),
            "original_idea": item.get("original_idea"),
            "evidence_video_ids": evidence_ids,
            "sources": source_rows,
        })
        topic_by_id[topic_id] = item

    system = (
        "Ты независимый редактор и технический продюсер автоматической видеостудии KadrFarm. "
        "Проверяй доказательность конкретной темы и реалистичность её производства. "
        "Не делай вид, что смотрел ролики или проверял наличие стоков. Верни только JSON."
    )
    prompt = f"""Выполни ФИНАЛЬНЫЙ АУДИТ каждой предложенной темы. В одном ответе проверь ДВА обязательных условия: смысловую целостность источников и ПРОИЗВОДИМОСТЬЮ СРЕДСТВАМИ KadrFarm.

КОНТЕКСТ КАНАЛА:
Канал универсальный, постоянной ниши нет. Все категории рассматриваются на равных. Не отдавай преимущество или штраф теме только из-за её категории.

ВОЗМОЖНОСТИ KadrFarm:
- полноценная дикторская дорожка на русском языке;
- «живая презентация»: авторские слайды и последовательное визуальное объяснение;
- самостоятельно создаваемые схемы, диаграммы, таблицы, таймлайны, карты, подписи и простая инфографика;
- фотографии и иллюстрации;
- видеовставки из фотостоков; в текущей конфигурации Pixabay настроен, Pexels сейчас не настроен. Сам факт доступности конкретного материала ещё не проверен;
- монтаж и чередование презентационных сцен, графики, фотографий и стокового B-roll.

ОГРАНИЧЕНИЯ:
- ферма не снимает собственные репортажи, интервью, эксперименты, демонстрации и уникальные события;
- у фермы нет гарантированного доступа к конкретным игровым кадрам, записям с места происшествия, закрытым архивам, эксклюзивным трансляциям или кадрам конкретного человека/объекта;
- нельзя считать, что подходящий кадр обязательно найдётся в Pixabay/Pexels;
- случайный тематически похожий B-roll не заменяет точную демонстрацию и не должен создавать ложное впечатление, что показан именно описываемый случай;
- ключевое объяснение, факты и выводы должны быть понятны ТОЛЬКО из дикторского рассказа. Изображения и видео усиливают рассказ, но не несут единственную важную информацию.

ЧАСТЬ A — СМЫСЛОВАЯ ЦЕЛОСТНОСТЬ:
1. Проверь каждый заголовок отдельно. Все evidence_video_ids должны подтверждать один и тот же конкретный сюжет, событие, объект, проблему, метод или узкий вопрос зрителя.
2. Совпадение только по широкой категории («ремонт», «бизнес», «игры», «авиация», «животные», «технологии») недостаточно.
3. Разные игры, разные бизнес-модели, разные виды ремонта и разные технические вопросы не образуют один сюжет только из-за общей рубрики.
4. Если хотя бы один источник не подтверждает общий конкретный сюжет, coherence.decision = reject. Не спасай кластер общими словами.
5. Для подтверждения актуального интереса нужны минимум два разных видео и минимум два независимых channel_id. Несколько выпусков одного автора не являются независимым подтверждением. Если канал один — decision = watchlist, а не approve.
6. Не выдумывай связи и не добавляй видео-ID.

ЧАСТЬ B — ПРОИЗВОДИМОСТЬ:
Оцени, можно ли сделать полноценный, содержательный и визуально убедительный ролик именно перечисленными средствами фермы.
- high: тема хорошо раскрывается диктором; важные визуалы можно создать в презентации/графике и дополнить типовыми стоковыми фото/видео; незаменимые уникальные кадры не нужны.
- conditional: тема интересна, но исходный угол требует недоступных или сомнительных кадров. Есть конкретный способ адаптировать подачу в объясняющий/документальный формат с авторской графикой и доступными типами стоков.
- low: главная ценность темы зависит от уникальных кадров, интервью, демонстрации или точного видео конкретного события; адаптация разрушит смысл или сделает ролик вводящим в заблуждение.

Не ставь high только потому, что можно вставить общие стоковые кадры. Проверяй, иллюстрируют ли визуалы именно содержание дикторского рассказа. Укажи конкретные потребности к фото и видео как поисковые концепции, но НЕ утверждай, что такие материалы уже существуют или доступны в фотостоке.

Для каждой темы верни объект farm_feasibility со всеми полями:
- rating: строго high, conditional или low;
- recommended_format: рекомендуемый формат объясняющего ролика;
- narration_core: что диктор должен полностью объяснить словами;
- presentation_visuals: список сцен/схем/диаграмм/карт/таймлайнов, которые ферма может создать сама;
- stock_photo_needs: какие типы фотографий/иллюстраций нужны;
- stock_video_needs: какие типы B-roll можно искать в фотостоках;
- unavailable_visuals: какие конкретные кадры нельзя гарантированно получить средствами фермы;
- production_risks: риски визуального несоответствия, фактической точности или прав;
- adaptation_idea: как изменить угол темы, чтобы сделать её средствами фермы; для high можно указать «адаптация не требуется».

Верни ВСЕХ кандидатов, не пропускай записи. Не создавай дополнительные ID.

СТРОГИЙ JSON:
{{"reviews":[{{"topic_id":"T1","coherence":{{"decision":"approve|watchlist|reject","shared_specific_subject":"конкретный общий сюжет или пустая строка","all_sources_support_same_subject":true,"coherent_video_ids":["ID из темы"],"reason":"объяснение"}},"farm_feasibility":{{"rating":"high|conditional|low","recommended_format":"...","narration_core":"...","presentation_visuals":["..."],"stock_photo_needs":["..."],"stock_video_needs":["..."],"unavailable_visuals":["..."],"production_risks":["..."],"adaptation_idea":"..."}}]}}

СПИСОК ТЕМ И ИСХОДНЫХ ВИДЕО (JSON):
{json.dumps(topic_rows, ensure_ascii=False, separators=(",", ":"))}"""

    response = _json_response(router, system, prompt, max_tokens=6500)
    raw_reviews = response.get("reviews", [])
    review_by_id: dict[str, dict[str, Any]] = {}
    if isinstance(raw_reviews, list):
        for row in raw_reviews:
            if isinstance(row, dict):
                key = str(row.get("topic_id") or "").strip()
                if key and key not in review_by_id:
                    review_by_id[key] = row

    approved: list[dict[str, Any]] = []
    watchlist: list[dict[str, Any]] = []
    not_suitable: list[dict[str, Any]] = []
    coherence_rejections: list[dict[str, Any]] = []

    for topic_id, item in topic_by_id.items():
        review = review_by_id.get(topic_id)
        expected_ids = list(dict.fromkeys(item.get("evidence", {}).get("video_ids", [])))
        expected_set = set(expected_ids)
        coherence = review.get("coherence", {}) if isinstance(review, dict) else {}
        farm = review.get("farm_feasibility", {}) if isinstance(review, dict) else {}
        raw_coherent = coherence.get("coherent_video_ids", []) if isinstance(coherence, dict) else []
        coherent_ids = {
            str(value).strip() for value in raw_coherent
            if str(value).strip() in expected_set
        } if isinstance(raw_coherent, list) else set()
        shared_subject = str(coherence.get("shared_specific_subject") or "").strip() if isinstance(coherence, dict) else ""
        coherence_reason = str(coherence.get("reason") or "Проверка смысловой целостности отсутствует.").strip() if isinstance(coherence, dict) else "Проверка смысловой целостности отсутствует."
        decision = str(coherence.get("decision") or "reject").strip().lower() if isinstance(coherence, dict) else "reject"

        channel_keys: set[str] = set()
        for video_id in expected_ids:
            video = by_id.get(video_id, {})
            channel_id = str(video.get("channel_id") or "").strip()
            channel_title = str(video.get("channel_title") or "").strip()
            channel_keys.add(channel_id or channel_title.casefold() or "unknown-channel")

        coherent = (
            decision in {"approve", "watchlist"}
            and bool(shared_subject)
            and coherence.get("all_sources_support_same_subject") is True
            and len(expected_set) >= 2
            and coherent_ids == expected_set
        )
        if not coherent or decision == "reject":
            coherence_rejections.append({
                "theme": item.get("theme", "Без названия"),
                "reason": coherence_reason[:1000],
                "supporting_video_count": len(expected_ids),
                "independent_channel_count": len(channel_keys),
                "evidence_videos": [
                    {
                        "video_id": video_id,
                        "title": by_id.get(video_id, {}).get("title", ""),
                        "channel_title": by_id.get(video_id, {}).get("channel_title", ""),
                    }
                    for video_id in expected_ids
                ],
            })
            continue

        farm_rating = str(farm.get("rating") or "").strip().lower() if isinstance(farm, dict) else ""
        required_text = (
            str(farm.get("recommended_format") or "").strip(),
            str(farm.get("narration_core") or "").strip(),
            str(farm.get("adaptation_idea") or "").strip(),
        ) if isinstance(farm, dict) else ("", "", "")
        farm_complete = farm_rating in {"high", "conditional", "low"} and all(required_text)
        farm_data = {
            "rating": farm_rating if farm_rating in {"high", "conditional", "low"} else "unassessed",
            "recommended_format": required_text[0] or "Оценка не сформирована",
            "narration_core": required_text[1] or "Оценка не сформирована",
            "presentation_visuals": [str(x).strip()[:300] for x in farm.get("presentation_visuals", []) if str(x).strip()][:10]
                if isinstance(farm.get("presentation_visuals"), list) else [],
            "stock_photo_needs": [str(x).strip()[:250] for x in farm.get("stock_photo_needs", []) if str(x).strip()][:10]
                if isinstance(farm.get("stock_photo_needs"), list) else [],
            "stock_video_needs": [str(x).strip()[:250] for x in farm.get("stock_video_needs", []) if str(x).strip()][:10]
                if isinstance(farm.get("stock_video_needs"), list) else [],
            "unavailable_visuals": [str(x).strip()[:250] for x in farm.get("unavailable_visuals", []) if str(x).strip()][:10]
                if isinstance(farm.get("unavailable_visuals"), list) else [],
            "production_risks": [str(x).strip()[:300] for x in farm.get("production_risks", []) if str(x).strip()][:10]
                if isinstance(farm.get("production_risks"), list) else [],
            "adaptation_idea": required_text[2] or "Нужна ручная редакторская адаптация",
        }
        audited_item = dict(item)
        audited_item["coherence_review"] = {
            "status": "passed",
            "shared_specific_subject": shared_subject[:250],
            "reason": coherence_reason[:700],
            "independent_channel_count": len(channel_keys),
        }
        audited_item["farm_feasibility"] = farm_data

        if len(channel_keys) < 2 or decision == "watchlist":
            audited_item["production_status"] = "watchlist"
            audited_item["production_status_label"] = "Наблюдать: мало независимых источников"
            audited_item["coherence_review"]["status"] = "watchlist"
            watchlist.append(audited_item)
        elif not farm_complete:
            audited_item["production_status"] = "not_assessed"
            audited_item["production_status_label"] = "Не допущено: неполная оценка производимости"
            not_suitable.append(audited_item)
        elif farm_rating == "low":
            audited_item["production_status"] = "not_suitable"
            audited_item["production_status_label"] = "Не подходит текущим возможностям фермы"
            not_suitable.append(audited_item)
        elif farm_rating == "conditional":
            audited_item["production_status"] = "adaptation_required"
            audited_item["production_status_label"] = "Требует адаптации под возможности фермы"
            approved.append(audited_item)
        else:
            audited_item["production_status"] = "ready"
            audited_item["production_status_label"] = "Готово к производству"
            approved.append(audited_item)

    for rank, item in enumerate(approved, 1):
        item["rank"] = rank
    for item in watchlist:
        item["rank"] = None
    for item in not_suitable:
        item["rank"] = None

    return {
        "approved": approved,
        "watchlist": watchlist,
        "not_suitable": not_suitable,
        "coherence_rejections": coherence_rejections,
        "reviewed_count": len(recommendations),
        "llm_call_count": 1,
    }


def recommend_discovery_topics(
    settings: Settings,
    source_json: Path,
    source_markdown: Path,
    out_dir: Path,
    *,
    limit: int = 10,
    batch_size: int = 50,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Preselect a balanced sample, then rank universal editorial topics."""
    if not 3 <= int(limit) <= 15:
        raise ValueError("limit должен быть в диапазоне от 3 до 15")
    if not 2 <= int(batch_size) <= 300:
        raise ValueError("batch_size должен быть в диапазоне от 2 до 300")
    current = now or dt.datetime.now(dt.timezone.utc)
    report = _validate_source_pair(Path(source_json), Path(source_markdown), current)
    all_candidates = _prepare_candidates(report)
    candidates, preselection = _preselect_candidates(
        all_candidates,
        report,
        per_search_set=4,
        chart_limit=10,
        max_search_candidates=160,
    )
    by_id = {item["video_id"]: item for item in candidates}

    # Provider order is inherited from Settings: PRIMARY first, SECONDARY fallback.
    # Only this bounded sample is sent to the LLM; the Discovery source files remain untouched.
    router = LLMRouter(settings, honor_provider_timeouts=True)
    clusters = _cluster_batches(router, candidates, batch_size=int(batch_size))
    if not clusters:
        raise RuntimeError(
            "LLM не выделила повторяющиеся тематические кластеры с подтверждением "
            "минимум двумя разными видео. Исходные файлы не изменены."
        )

    catalogue = _build_cluster_catalogue(clusters, by_id)
    all_cluster_ids = sorted({
        video_id for cluster in clusters for video_id in cluster["evidence_video_ids"]
    })
    final_input = json.dumps(catalogue, ensure_ascii=False, separators=(",", ":"))
    final_prompt = f"""Ниже приведены тематические кластеры из предварительно отобранной выборки Discovery. Сначала объедини кластеры, если они описывают один и тот же конкретный сюжет, но сохрани разные темы, если у них разные вопросы зрителя.

КОНТЕКСТ КАНАЛА: канал УНИВЕРСАЛЬНЫЙ, без постоянной ниши. Ищи темы, интересные аудитории YouTube в текущий момент, независимо от категории. Рассматривай на равных новости, общество, политику, экономику, работу и деньги, технологии, науку, историю, спорт, игры, кино, музыку, автомобили, путешествия, еду, животных, дом, ремонт, природу, происшествия и любые другие направления, присутствующие в данных. Не предпочитай ИИ/технологии, не ограничивайся ими и не отбрасывай категорию из-за её тематики.

Выбери ДО {int(limit)} самых перспективных тем. Не нужно обязательно заполнять лимит: верни меньше рекомендаций, если только меньшее число кластеров имеет убедительные основания. Не придумывай темы, которых нет в исходных видео.

РАНЖИРОВАНИЕ ДОЛЖНО УЧИТЫВАТЬ ПРОЗРАЧНЫЕ СИГНАЛЫ:
- сколько уникальных видео поддерживают конкретный сюжет;
- сколько независимых каналов представлено; несколько выпусков одного автора или одной серии — не то же самое, что независимый интерес нескольких авторов;
- сколько видео попало в поисковые выборки за 24 часа и за 7 дней;
- насколько подтверждение присутствует в обеих поисковых сортировках: viewCount и date;
- исходные view_count как контекст масштаба, но не сравнивай их без оглядки на возраст публикаций и не позволяй одному старому ролику определять рейтинг;
- насколько конкретен и понятен сюжет зрителю и можно ли сделать самостоятельный содержательный ролик.

Поля videos_in_24h_search / videos_in_7d_search означают число уникальных видео, найденных в соответствующих поисковых контекстах; это НЕ прирост просмотров и НЕ доказательство ускорения тренда. view_count — общий счётчик API на момент сбора. Не рассчитывай скорость роста, viral score, проценты или единый математический рейтинг. Не делай вывод «тема взлетает» только по большому числу просмотров.

Для каждой темы верни: theme, original_idea (оригинальная концепция, не повторяющая заголовки), audience_hook, why_recommended (объяснение с опорой на числа и независимость источников из evidence_signals), evidence_video_ids (только ID из каталога), story_directions (3–5 пунктов), fact_check_risks (массив). Минимум два реальных видео-основания; если они все с одного канала, прямо пометь сигнал как ограниченный в why_recommended. Не выдумывай факты и не выдавай заголовки за проверенное содержание. Отсортируй от наиболее к наименее перспективной. Не добавляй общий трендовый балл.

Строгий JSON:
{{"recommendations":[{{"theme":"...","original_idea":"...","audience_hook":"...","why_recommended":"...","evidence_video_ids":["..."],"story_directions":["..."],"fact_check_risks":["..."]}}],"overall_notes":["..."]}}

ДОПУСТИМЫЕ ID: {json.dumps(all_cluster_ids, ensure_ascii=False)}

КАТАЛОГ КЛАСТЕРОВ С ИЗМЕРИМЫМИ СИГНАЛАМИ (JSON):
{final_input}"""
    final = _json_response(router, FINAL_SYSTEM, final_prompt, max_tokens=6000)
    raw_recommendations = final.get("recommendations", [])
    if not isinstance(raw_recommendations, list):
        raise ValueError("LLM вернула recommendations не в виде списка")

    allowed_ids = set(all_cluster_ids)
    recommendations: list[dict[str, Any]] = []
    seen_themes: set[str] = set()
    for raw in raw_recommendations:
        if not isinstance(raw, dict):
            continue
        theme = str(raw.get("theme") or "").strip()
        idea = str(raw.get("original_idea") or "").strip()
        if not theme or not idea:
            continue
        key = re.sub(r"\s+", " ", theme).casefold()
        if key in seen_themes:
            continue
        ids = list(dict.fromkeys(
            str(value).strip() for value in raw.get("evidence_video_ids", [])
            if str(value).strip() in allowed_ids and str(value).strip() in by_id
        )) if isinstance(raw.get("evidence_video_ids"), list) else []
        if len(ids) < 2:
            continue
        seen_themes.add(key)
        directions = raw.get("story_directions", [])
        risks = raw.get("fact_check_risks", [])
        recommendations.append({
            "rank": len(recommendations) + 1,
            "theme": theme[:180],
            "original_idea": idea[:700],
            "audience_hook": str(raw.get("audience_hook") or "").strip()[:500],
            "why_recommended": str(raw.get("why_recommended") or "").strip()[:1000],
            "story_directions": [str(x).strip()[:300] for x in directions if str(x).strip()][:5]
                if isinstance(directions, list) else [],
            "fact_check_risks": [str(x).strip()[:300] for x in risks if str(x).strip()][:8]
                if isinstance(risks, list) else [],
            "evidence": _evidence_summary(ids, by_id),
        })
        if len(recommendations) >= int(limit):
            break
    if not recommendations:
        raise RuntimeError(
            "LLM не вернула ни одной рекомендации с двумя или более реальными "
            "видео-основаниями. Проверьте логи провайдера; отчёт не создан."
        )

    proposed_recommendation_count = len(recommendations)
    farm_review = _review_topic_production_fit(router, recommendations, by_id)
    recommendations = farm_review["approved"]
    overall_notes = final.get("overall_notes", [])
    notes = [
        f"В исходном Discovery было {len(all_candidates)} уникальных видео. До LLM локально отобрано {len(candidates)}: до четырёх кандидатов из каждого поискового набора (viewCount — по исходному view_count, date — по published_at) плюс до десяти видео российского mostPopular; повторы по video_id устранены.",
        "Все категории рассматриваются на равных: канал универсальный, фиксированной ниши и тематического фильтра нет.",
        "LLM получила только предварительную выборку, а не весь исходный массив; исходный discovery.json не изменяется.",
        "Независимость источников оценивается по channel_id, с резервным использованием названия канала; несколько видео одного канала видны отдельно.",
        "Счётчики по окнам 24 часа/7 дней и сортировкам viewCount/date показывают происхождение видео в поисковой выдаче, а не скорость роста просмотров.",
        "discovery.json является структурированным источником; discovery.md проверен по дате формирования и количеству видео, но не дублировался в запросах к LLM.",
        "LLM анализировала заголовки, каналы, дату публикации, исходные счётчики и происхождение из поисковых наборов; сами видеоролики не просматривались.",
        "Для каждой предложенной темы выполнен единый дополнительный LLM-аудит: проверка конкретного общего сюжета по всем видео-основаниям и оценка производимости средствами KadrFarm.",
        "KadrFarm располагает дикторской дорожкой, живой презентацией, авторскими слайдами, схемами, диаграммами, картами, фотографиями и стоковым B-roll. В текущей конфигурации Pixabay настроен, Pexels не настроен.",
        "Ключевое объяснение обязано полностью содержаться в дикторском рассказе; слайды и стоковые фото/видео только усиливают его. Доступность конкретного стокового материала проверяется отдельным этапом.",
        "Темы с недостаточно конкретной связью между источниками отклоняются; темы с одним независимым каналом остаются в списке наблюдения, а не в основном рейтинге.",
        "Канал универсальный: нет фиксированной ниши или тематического фильтра; все категории рассматриваются на равных.",
        "Приоритет — редакторское суждение модели, а не официальный рейтинг YouTube и не математический трендовый балл.",
        "Независимость подтверждения оценивается по числу уникальных channel_id (при отсутствии ID — по названию канала); повторы одного автора отдельно видны в отчёте.",
        "Счётчики видео в окнах 24 часа/7 дней и сортировках viewCount/date считают уникальные видео по происхождению в search.list; это не измерение динамики просмотров.",
        "view_count сохранён только как исходное значение API. Скорость прироста, нормализация по возрасту, viral score и отношения лайков/просмотров не рассчитывались.",
        "Наличие RU в поисковом контексте не доказывает язык ролика или местонахождение автора.",
        "Видео-основания нужно открыть и проверить вручную до сценария: заголовок не подтверждает содержание и факты.",
        "В отчёте есть данные YouTube API; обновите или удалите рекомендации не позднее исходного refresh_or_delete_by_utc.",
    ]
    if isinstance(overall_notes, list):
        notes.extend(str(value).strip() for value in overall_notes if str(value).strip())

    result = {
        "schema_version": "1.2",
        "source": "YouTube Discovery v2 + configured LLM providers",
        "source_json_file": Path(source_json).name,
        "source_markdown_file": Path(source_markdown).name,
        "source_generated_at_utc": report["_validated_generated_at"],
        "source_refresh_or_delete_by_utc": report["_validated_deadline"],
        "generated_at_utc": current.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "method": {
            "candidate_count_total": len(all_candidates),
            "candidate_count_selected": len(candidates),
            "candidate_count_analyzed": len(candidates),
            "preselection": preselection,
            "batch_size": int(batch_size),
            "batch_count": (len(candidates) + int(batch_size) - 1) // int(batch_size),
            "semantic_clustering": True,
            "ranking_type": "LLM editorial judgment using explicit freshness, cross-channel, and search-provenance evidence; not a YouTube trend score",
            "channel_scope": "universal_all_categories_no_fixed_niche",
            "topic_category_preference": None,
            "evidence_signals": ["unique_supporting_videos", "independent_channels", "24h_search_presence", "7d_search_presence", "viewCount_order_presence", "date_order_presence"],
            "view_velocity_calculated": False,
            "fixed_channel_niche_filter": False,
            "farm_feasibility_review_performed": True,
            "farm_feasibility_review_call_count": farm_review["llm_call_count"],
            "farm_production_capabilities": [
                "full_voiceover_narration",
                "live_presentation_slides",
                "custom_diagrams_charts_maps_timelines",
                "stock_photos_and_illustrations",
                "stock_video_broll_pixabay_configured_pexels_unconfigured",
            ],
            "voiceover_is_primary_information_carrier": True,
            "video_content_watched": False,
        },
        "cluster_count_before_consolidation": len(clusters),
        "recommendation_count": len(recommendations),
        "proposed_recommendation_count": proposed_recommendation_count,
        "farm_review": {
            "performed": True,
            "reviewed_count": farm_review["reviewed_count"],
            "approved_count": len(recommendations),
            "watchlist_count": len(farm_review["watchlist"]),
            "not_suitable_count": len(farm_review["not_suitable"]),
            "coherence_rejected_count": len(farm_review["coherence_rejections"]),
            "llm_call_count": farm_review["llm_call_count"],
        },
        "watchlist": farm_review["watchlist"],
        "not_suitable_for_farm": farm_review["not_suitable"],
        "coherence_rejections": farm_review["coherence_rejections"],
        "recommendations": recommendations,
        "notes": notes,
    }

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "recommendations.json", result)
    markdown = _render_markdown(result)
    temp = out_dir / "recommendations.md.tmp"
    temp.write_text(markdown, encoding="utf-8")
    temp.replace(out_dir / "recommendations.md")
    return result
