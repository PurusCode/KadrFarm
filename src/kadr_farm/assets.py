from __future__ import annotations
import logging, re, time, json
from pathlib import Path
from typing import Any
import requests
from .config import Settings
from .common import ensure_dir, read_json, write_json, stable_hash

log = logging.getLogger(__name__)
PEXELS_API = "https://api.pexels.com/v1"
PIXABAY_VIDEO_API = "https://pixabay.com/api/videos/"
PIXABAY_IMAGE_API = "https://pixabay.com/api/"
MAX_BYTES = 50 * 1024 * 1024

class StockSearch:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "video-farm/0.1 (local personal video workflow)"})
        self.cache_dir = settings.cache_dir / "stock_search"
        ensure_dir(self.cache_dir)

    def cached_json(self, source: str, query: str, url: str, params: dict[str, Any], headers: dict[str, str] | None = None, ttl: int = 86400) -> dict[str, Any]:
        cache_path = self.cache_dir / f"{source}_{stable_hash(query.lower())}.json"
        if cache_path.exists() and time.time() - cache_path.stat().st_mtime < ttl:
            try: return json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception: pass
        r = self.session.get(url, params=params, headers=headers or {}, timeout=45)
        if r.status_code == 429:
            raise RuntimeError(f"{source} API вернул 429 — превышен лимит. Автоматическое обходное поведение отключено.")
        if r.status_code >= 400:
            raise RuntimeError(f"{source} API {r.status_code}: {r.text[:500]}")
        data = r.json()
        cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def download(self, url: str, target: Path, max_bytes: int = MAX_BYTES) -> int:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > 0:
            return target.stat().st_size
        tmp = target.with_suffix(target.suffix + ".part")
        total = 0
        with self.session.get(url, stream=True, timeout=90) as r:
            r.raise_for_status()
            length = int(r.headers.get("Content-Length", "0") or 0)
            if length and length > max_bytes:
                raise RuntimeError(f"Файл {length / 1024 / 1024:.1f} МБ больше лимита 50 МБ")
            try:
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(1024 * 128):
                        if not chunk: continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise RuntimeError("Загрузка остановлена: превышен лимит 50 МБ")
                        f.write(chunk)
                tmp.replace(target)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
        return total


def _safe_keywords(script: dict[str, Any]) -> list[str]:
    all_kw: list[str] = []
    seen: set[str] = set()
    for b in script.get("blocks", []):
        for kw in b.get("broll_keywords", []):
            val = re.sub(r"[^A-Za-z0-9 -]", "", str(kw)).strip()
            if len(val) >= 3 and val.lower() not in seen:
                all_kw.append(val); seen.add(val.lower())
        for s in b.get("slides", []):
            kw = re.sub(r"[^A-Za-z0-9 -]", "", str(s.get("photo_keyword", ""))).strip()
            if kw and kw.lower() not in seen:
                all_kw.append(kw); seen.add(kw.lower())
    return all_kw


def _search_pexels_video(client: StockSearch, key: str, query: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    data = client.cached_json("pexels_v", query, f"{PEXELS_API}/videos/search",
        {"query": query, "orientation": "landscape", "size": "medium", "per_page": 8, "page": 1},
        {"Authorization": key}, ttl=86400)
    cand = []
    for v in data.get("videos", []):
        duration = int(v.get("duration", 0))
        if not 5 <= duration <= 60: continue
        files = [f for f in v.get("video_files", []) if f.get("link") and int(f.get("width", 0)) >= 1280 and int(f.get("height", 0)) >= 720 and "mp4" in str(f.get("file_type", "video/mp4"))]
        if files:
            chosen = min(files, key=lambda f: (abs(int(f.get("width", 0)) - 1920), -int(f.get("height", 0))))
            cand.append((v, chosen))
    cand.sort(key=lambda p: (abs(int(p[0].get("duration", 0)) - 20), -int(p[1].get("width", 0))))
    return cand[0] if cand else None


def _search_pixabay_video(client: StockSearch, key: str, query: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    data = client.cached_json("pixabay_v", query, PIXABAY_VIDEO_API,
        {"key": key, "q": query, "min_width": 1280, "min_height": 720, "per_page": 20, "order": "popular", "safesearch": "true", "lang": "en"}, ttl=86400)
    cand = []
    for v in data.get("hits", []):
        duration = int(v.get("duration", 0))
        if not 5 <= duration <= 60: continue
        renditions = v.get("videos", {})
        choices = [renditions.get(k, {}) for k in ("medium", "large")]
        choices = [r for r in choices if r.get("url") and int(r.get("width", 0)) >= 1280 and int(r.get("height", 0)) >= 720 and (not r.get("size") or int(r.get("size", 0)) <= MAX_BYTES)]
        if choices:
            chosen = min(choices, key=lambda r: (abs(int(r.get("width", 0)) - 1920), -int(r.get("height", 0))))
            cand.append((v, chosen))
    cand.sort(key=lambda p: (abs(int(p[0].get("duration", 0)) - 20), -int(p[1].get("width", 0))))
    return cand[0] if cand else None


def _search_pexels_photo(client: StockSearch, key: str, query: str) -> dict[str, Any] | None:
    data = client.cached_json("pexels_p", query, f"{PEXELS_API}/search",
        {"query": query, "orientation": "landscape", "size": "large", "per_page": 5, "page": 1},
        {"Authorization": key}, ttl=86400)
    return data.get("photos", [None])[0]


def _search_pixabay_photo(client: StockSearch, key: str, query: str) -> dict[str, Any] | None:
    data = client.cached_json("pixabay_p", query, PIXABAY_IMAGE_API,
        {"key": key, "q": query, "image_type": "photo", "orientation": "horizontal", "min_width": 1280, "min_height": 720,
         "per_page": 10, "safesearch": "true", "lang": "en"}, ttl=86400)
    return data.get("hits", [None])[0]


def download_assets(settings: Settings, script_path: Path, out_manifest: Path, *, max_keywords: int = 30) -> dict[str, Any]:
    script = read_json(script_path)
    keywords = _safe_keywords(script)[:max_keywords]
    if not settings.pexels_api_key and not settings.pixabay_api_key:
        manifest = {"source": [], "status": "skipped_no_api_key", "keywords_requested": keywords,
                    "broll": [], "images": [], "notice": "Задайте PEXELS_API_KEY и/или PIXABAY_API_KEY в .env. Стоковые материалы не скачивались."}
        write_json(out_manifest, manifest)
        log.warning("Не заданы ключи стоков: скачивание B-roll пропущено.")
        return manifest
    client = StockSearch(settings)
    ensure_dir(settings.broll_dir); ensure_dir(settings.images_dir)
    broll_items, image_items = [], []
    for idx, query in enumerate(keywords, 1):
        keyhash = stable_hash(query.lower())
        preferred = "pixabay" if settings.pixabay_api_key and (not settings.pexels_api_key or idx % 2 == 0) else "pexels"
        providers = [preferred, "pexels" if preferred == "pixabay" else "pixabay"]
        chosen_provider = None
        for provider in providers:
            api_key = settings.pixabay_api_key if provider == "pixabay" else settings.pexels_api_key
            if not api_key: continue
            try:
                result = _search_pixabay_video(client, api_key, query) if provider == "pixabay" else _search_pexels_video(client, api_key, query)
                if not result: continue
                video, file_info = result
                vid = video.get("id")
                target = settings.broll_dir / f"{provider}_{keyhash}_{vid}.mp4"
                size = client.download(file_info.get("url") if provider == "pixabay" else file_info.get("link"), target)
                item = {"query": query, "file": str(target), "duration_seconds": int(video.get("duration", 0)),
                    "width": file_info.get("width"), "height": file_info.get("height"), "bytes": size,
                    "source": "Pixabay" if provider == "pixabay" else "Pexels",
                    "page_url": video.get("pageURL", "") if provider == "pixabay" else video.get("url", ""),
                    "creator": video.get("user", "") if provider == "pixabay" else video.get("user", {}).get("name", ""),
                    "creator_url": (f"https://pixabay.com/users/{video.get('user', '')}-{video.get('user_id', '')}/" if provider == "pixabay" else video.get("user", {}).get("url", ""))}
                broll_items.append(item); chosen_provider = provider
                break
            except Exception as exc:
                log.warning("Поиск/загрузка %s для '%s' не удались: %s", provider, query, exc)
        # Photos use the provider that delivered the video first, then try the other configured provider.
        photo_done = False
        photo_providers = ([chosen_provider] if chosen_provider else []) + [x for x in providers if x != chosen_provider]
        for provider in photo_providers:
            if not provider: continue
            api_key = settings.pixabay_api_key if provider == "pixabay" else settings.pexels_api_key
            if not api_key: continue
            try:
                photo = _search_pixabay_photo(client, api_key, query) if provider == "pixabay" else _search_pexels_photo(client, api_key, query)
                if not photo: continue
                url = (photo.get("largeImageURL") or photo.get("webformatURL")) if provider == "pixabay" else (photo.get("src", {}).get("large2x") or photo.get("src", {}).get("large"))
                if not url: continue
                target = settings.images_dir / f"{provider}_{keyhash}_{photo.get('id')}.jpg"
                size = client.download(url, target)
                image_items.append({"query": query, "file": str(target), "bytes": size,
                    "source": "Pixabay" if provider == "pixabay" else "Pexels",
                    "page_url": photo.get("pageURL", "") if provider == "pixabay" else photo.get("url", ""),
                    "creator": photo.get("user", "") if provider == "pixabay" else photo.get("photographer", ""),
                    "creator_url": (f"https://pixabay.com/users/{photo.get('user', '')}-{photo.get('user_id', '')}/" if provider == "pixabay" else photo.get("photographer_url", ""))})
                photo_done = True; break
            except Exception as exc:
                log.warning("Фото %s для '%s' не найдено/не скачано: %s", provider, query, exc)
        if not chosen_provider:
            log.info("Для запроса '%s' подходящего B-roll не найдено.", query)
        time.sleep(0.15)  # polite pacing; stock APIs are cached for 24 hours.
    sources = sorted({x["source"] for x in broll_items})
    manifest = {"source": sources, "status": "complete", "keywords_requested": keywords,
                "broll": broll_items, "images": image_items,
                "note": "Проверьте страницу и действующую лицензию каждого материала перед коммерческой публикацией. API-результаты кэшируются на 24 часа; обход лимитов не выполняется.",
                "attribution": "B-roll: " + ", ".join(sources)}
    write_json(out_manifest, manifest)
    return manifest
