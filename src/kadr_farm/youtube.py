from __future__ import annotations
import collections, datetime as dt, logging, re, statistics
from pathlib import Path
from typing import Any
import requests
from .config import Settings
from .common import write_json

log = logging.getLogger(__name__)
API = "https://www.googleapis.com/youtube/v3"


def iso_duration_seconds(value: str) -> int:
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not m:
        return 0
    h, minute, sec = (int(x or 0) for x in m.groups())
    return h * 3600 + minute * 60 + sec


def _get(path: str, params: dict[str, Any], api_key: str) -> dict[str, Any]:
    r = requests.get(f"{API}/{path}", params={**params, "key": api_key}, timeout=35)
    if r.status_code >= 400:
        raise RuntimeError(f"YouTube API {r.status_code}: {r.text[:700]}")
    return r.json()


def analyze_youtube(settings: Settings, queries: list[str], out_dir: Path, *, per_query: int = 25,
                    region: str = "RU", max_age_days: int = 365) -> dict[str, Any]:
    if not settings.youtube_api_key:
        raise RuntimeError("Не задан YOUTUBE_API_KEY. Анализ использует официальный YouTube Data API v3, без скрейпинга сайта.")
    all_ids: list[str] = []
    query_map: dict[str, list[str]] = {}
    published_after = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)).isoformat(timespec="seconds").replace("+00:00", "Z")
    for query in queries:
        res = _get("search", {"part": "snippet", "type": "video", "q": query, "maxResults": min(50, max(1, per_query)),
                              "order": "viewCount", "publishedAfter": published_after, "regionCode": region,
                              "relevanceLanguage": "ru", "safeSearch": "moderate"}, settings.youtube_api_key)
        ids = [x.get("id", {}).get("videoId") for x in res.get("items", [])]
        ids = [x for x in ids if x]
        query_map[query] = ids
        all_ids.extend(ids)
    ids = list(dict.fromkeys(all_ids))
    videos: list[dict[str, Any]] = []
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        if not batch: continue
        res = _get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(batch)}, settings.youtube_api_key)
        for x in res.get("items", []):
            stats, snippet = x.get("statistics", {}), x.get("snippet", {})
            videos.append({
                "video_id": x["id"], "url": f"https://www.youtube.com/watch?v={x['id']}",
                "title": snippet.get("title", ""), "channel_id": snippet.get("channelId", ""),
                "channel_title": snippet.get("channelTitle", ""), "published_at": snippet.get("publishedAt", ""),
                "description": snippet.get("description", "")[:1000],
                "view_count": int(stats.get("viewCount", 0)),
                "like_count": int(stats["likeCount"]) if "likeCount" in stats else None,
                "comment_count": int(stats["commentCount"]) if "commentCount" in stats else None,
                "duration_seconds": iso_duration_seconds(x.get("contentDetails", {}).get("duration", "")),
                "tags": snippet.get("tags", [])[:30],
            })
    channel_ids = list(dict.fromkeys(v["channel_id"] for v in videos if v["channel_id"]))
    channels: dict[str, dict[str, Any]] = {}
    for start in range(0, len(channel_ids), 50):
        res = _get("channels", {"part": "statistics,snippet", "id": ",".join(channel_ids[start:start + 50])}, settings.youtube_api_key)
        for c in res.get("items", []):
            st = c.get("statistics", {})
            channels[c["id"]] = {"channel_title": c.get("snippet", {}).get("title", ""),
                                 "subscriber_count": int(st["subscriberCount"]) if "subscriberCount" in st else None,
                                 "hidden_subscriber_count": st.get("hiddenSubscriberCount", False),
                                 "video_count": int(st.get("videoCount", 0)) if "videoCount" in st else None}
    views_by_channel: dict[str, list[int]] = collections.defaultdict(list)
    for v in videos: views_by_channel[v["channel_id"]].append(v["view_count"])
    title_lengths = [len(v["title"]) for v in videos]
    title_words: collections.Counter[str] = collections.Counter()
    bigrams: collections.Counter[str] = collections.Counter()
    stop = set("и в во на но а или что как для это из с по к у о от до не без при над под про уже ещё все всё чем если где когда почему какой какая какие самый самые топ обзор как бы ли за об из-за".split())
    for v in videos:
        tokens = [t.lower() for t in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", v["title"]) if len(t) > 2 and t.lower() not in stop]
        title_words.update(tokens)
        bigrams.update(f"{tokens[i]} {tokens[i+1]}" for i in range(len(tokens)-1))
    for v in videos:
        vals = views_by_channel[v["channel_id"]]
        med = statistics.median(vals) if vals else 0
        v["channel_median_views_in_sample"] = med
        v["viral_ratio"] = round(v["view_count"] / max(med, 1), 2)
        v["channel_stats"] = channels.get(v["channel_id"], {})
        v["title_features"] = {"length": len(v["title"]), "has_digit": bool(re.search(r"\d", v["title"])),
                                "has_question": "?" in v["title"], "caps_ratio": round(sum(ch.isupper() for ch in v["title"] if ch.isalpha()) / max(sum(ch.isalpha() for ch in v["title"]), 1), 2)}
        v["ai_suitability"] = round(0.55 + 0.15 * bool(re.search(r"(топ|обзор|сравнен|инструкц|гайд|что такое|нейросет|ии|ai|модель|программ|подборк|миф)", v["title"], re.I)), 2)
    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "YouTube Data API v3", "queries": queries, "region": region,
        "notes": ["Метрики отражают видимые поля API; лайки и число подписчиков могут быть скрыты.",
                  "viral_ratio рассчитывается относительно медианы выборки того же канала, а не всей истории канала.",
                  "AI suitability — эвристическая оценка по заголовку, требует редакторской проверки."],
        "summary": {"videos_found": len(videos), "channels_found": len(set(v["channel_id"] for v in videos)),
                     "median_views": statistics.median([v["view_count"] for v in videos]) if videos else 0,
                     "avg_title_length": round(statistics.mean(title_lengths), 1) if title_lengths else 0,
                     "viral_candidates": sum(v["viral_ratio"] >= 3 for v in videos)},
        "top_keywords": [{"keyword": k, "count": n} for k, n in title_words.most_common(40)],
        "top_bigrams": [{"bigram": k, "count": n} for k, n in bigrams.most_common(25)],
        "videos": sorted(videos, key=lambda v: v["view_count"], reverse=True),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "analysis.json", report)
    (out_dir / "analysis.md").write_text(_make_markdown(report), encoding="utf-8")
    return report


def _make_markdown(r: dict[str, Any]) -> str:
    s = r["summary"]
    lines = ["# Анализ ниши YouTube", "", f"- Дата: {r['generated_at']}", f"- Видео: {s['videos_found']}",
             f"- Каналов: {s['channels_found']}", f"- Медиана просмотров: {s['median_views']:,}".replace(",", " "),
             f"- Кандидаты в выбросы (viral_ratio ≥ 3): {s['viral_candidates']}",
             f"- Средняя длина заголовка: {s['avg_title_length']} символов", "", "## Частые ключевые слова"]
    lines += [f"- `{x['keyword']}` — {x['count']}" for x in r["top_keywords"][:20]]
    lines += ["", "## Частые биграммы"] + [f"- `{x['bigram']}` — {x['count']}" for x in r["top_bigrams"][:15]]
    lines += ["", "## Самые просматриваемые видео", "", "| Просмотры | Канал | Заголовок | viral_ratio |", "|---:|---|---|---:|"]
    for v in r["videos"][:20]:
        title = v["title"].replace("|", "\\|")
        lines.append(f"| {v['view_count']:,} | {v['channel_title']} | [{title}]({v['url']}) | {v['viral_ratio']}x |".replace(",", " "))
    lines += ["", "## Вердикт", "", "Автоматическая оценка носит предварительный характер. Перед выбором формата проверьте руками 10–20 лидирующих видео: дату, соответствие запросу, монтаж, оригинальность подачи и возможность безопасно повторить тему без чужого контента.", "", "## Ограничения", ""]
    lines += [f"- {n}" for n in r.get("notes", [])]
    return "\n".join(lines) + "\n"
