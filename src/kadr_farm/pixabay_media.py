"""Pixabay API adapter with compliant search caching and resilient downloads.

The API response cache is kept for 24 hours. A 429 response is surfaced immediately;
we never try alternate CDN URLs to evade a rate limit.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Iterable

import requests


SEARCH_CACHE_TTL_SECONDS = 24 * 60 * 60
MAX_SEARCH_QUERIES = 4
MAX_DOWNLOAD_CANDIDATES = 5
MAX_DOWNLOAD_ATTEMPTS = 2
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MIN_API_REQUEST_INTERVAL_SECONDS = 0.7  # at most ~86 uncached API calls per minute
_LAST_API_REQUEST_AT = 0.0

# Persistent CDN cooldown is stored outside individual output runs.
PIXABAY_CDN_COOLDOWN_FILE = Path("data/cache/pixabay/cdn_cooldown.json")
DEFAULT_CDN_COOLDOWN_SECONDS = 15 * 60


class PixabayError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None,
                 retry_after: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after

    @property
    def is_rate_limited(self) -> bool:
        return self.status_code == 429


# ENGINE_2_4_CDN_GUARD
# A CDN 429 is persisted across runs. The API's X-RateLimit-Reset header is deliberately
# not interpreted here: Pixabay documents it for API quota responses, not CDN file URLs.
_CDN_LOG_REDACTION_PATTERN = re.compile(r"(?i)([?&]key=)[^&\s\"']+")


class _PixabayLogRedactionFilter(logging.Filter):
    """Keep Pixabay API credentials out of urllib3 DEBUG request lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            redacted = _CDN_LOG_REDACTION_PATTERN.sub(r"\1[REDACTED]", message)
            if redacted != message:
                record.msg = redacted
                record.args = ()
        except Exception:
            # Logging must never break media retrieval.
            pass
        return True


for _logger_name in ("urllib3.connectionpool", "requests.packages.urllib3.connectionpool"):
    _pixabay_logger = logging.getLogger(_logger_name)
    if not any(isinstance(item, _PixabayLogRedactionFilter) for item in _pixabay_logger.filters):
        _pixabay_logger.addFilter(_PixabayLogRedactionFilter())


def _cdn_cooldown_path() -> Path:
    path = Path(PIXABAY_CDN_COOLDOWN_FILE)
    return path if path.is_absolute() else Path.cwd() / path


def _retry_after_delay(value: str | None, *, now: float | None = None) -> float | None:
    """Parse HTTP Retry-After as delta seconds or an HTTP date."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    current = time.time() if now is None else float(now)
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            from datetime import timezone
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, parsed.timestamp() - current)
    except (TypeError, ValueError, OverflowError):
        return None


def _read_cdn_cooldown(*, now: float | None = None) -> dict[str, Any] | None:
    path = _cdn_cooldown_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        until = float(payload.get("cooldown_until_epoch", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    current = time.time() if now is None else float(now)
    return payload if until > current else None


def _persist_cdn_cooldown(retry_after: str | None, *, host: str = "cdn.pixabay.com",
                          now: float | None = None) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    delay = _retry_after_delay(retry_after, now=current)
    if delay is None:
        # Conservative local safety interval when the CDN omits Retry-After.
        delay = float(DEFAULT_CDN_COOLDOWN_SECONDS)
    delay = max(1.0, delay)
    path = _cdn_cooldown_path()
    previous = _read_cdn_cooldown(now=current)
    until = max(current + delay, float(previous.get("cooldown_until_epoch", 0)) if previous else 0.0)
    payload = {
        "status": "cooldown",
        "scope": "pixabay_cdn",
        "host": host,
        "observed_at_epoch": current,
        "cooldown_until_epoch": until,
        "retry_after_header": str(retry_after)[:128] if retry_after is not None else None,
        "reason": "HTTP 429",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return payload


def _raise_if_cdn_cooldown_active() -> None:
    state = _read_cdn_cooldown()
    if not state:
        return
    remaining = max(1, int(math.ceil(float(state["cooldown_until_epoch"]) - time.time())))
    raise PixabayError(
        f"Pixabay CDN на паузе после HTTP 429; до следующей разрешённой попытки около {remaining} сек.",
        status_code=429,
        retry_after=str(remaining),
    )


def _redact_pixabay_key_from_text(value: str, api_key: str = "") -> str:
    text = str(value or "")
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    return _CDN_LOG_REDACTION_PATTERN.sub(r"\1[REDACTED]", text)


def _safe_name(value: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in value.lower())[:48] or "asset"


def _search_terms(value: str) -> set[str]:
    stop = {"the", "and", "for", "with", "from", "как", "что", "для", "это", "или", "при"}
    return {
        token for token in re.findall(r"[a-zа-яё0-9]{2,}", str(value or "").lower())
        if token not in stop
    }


def _candidate_score(query: str, hit: dict[str, Any], kind: str,
                     variant: dict[str, Any] | None = None) -> float:
    query_terms = _search_terms(query)
    tag_terms = _search_terms(hit.get("tags", ""))
    overlap = len(query_terms & tag_terms)
    # Never accept an unrelated hit just because it is popular or high-resolution.
    if overlap == 0:
        return -1.0
    coverage = overlap / max(1, len(query_terms))
    width = int((variant or {}).get("width") or hit.get("imageWidth") or hit.get("webformatWidth") or 0)
    height = int((variant or {}).get("height") or hit.get("imageHeight") or hit.get("webformatHeight") or 0)
    pixels = max(0, width * height)
    resolution = min(1.5, pixels / float(1920 * 1080))
    likes = max(0, int(hit.get("likes") or 0))
    downloads = max(0, int(hit.get("downloads") or 0))
    popularity = min(1.0, math.log1p(likes + downloads) / 16.0)
    duration = max(0.0, float(hit.get("duration") or 0))
    duration_bonus = min(1.0, duration / 12.0) if kind == "video" else 0.0
    return coverage * 100.0 + resolution * 7.0 + popularity * 2.0 + duration_bonus


def _cache_file(settings: Any, destination: Path, kind: str, query: str,
                api_key: str) -> Path:
    configured = getattr(settings, "cache_dir", None)
    cache_root = Path(configured) if configured else destination / ".pixabay_cache"
    # Hash the key rather than putting the credential in a path or cache document.
    key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
    digest = hashlib.sha256(f"{key_hash}:{kind}:{query.lower()}".encode("utf-8")).hexdigest()
    return cache_root / "pixabay" / "search" / f"{digest}.json"


def _read_search_cache(path: Path) -> dict[str, Any] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        cached_at = float(document.get("cached_at", 0))
        age = time.time() - cached_at
        payload = document.get("payload")
        if 0 <= age <= SEARCH_CACHE_TTL_SECONDS and isinstance(payload, dict):
            return payload
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def _write_search_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"cached_at": time.time(), "payload": payload}, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _pace_api_request() -> None:
    global _LAST_API_REQUEST_AT
    now = time.monotonic()
    delay = MIN_API_REQUEST_INTERVAL_SECONDS - (now - _LAST_API_REQUEST_AT)
    if delay > 0:
        time.sleep(delay)
    _LAST_API_REQUEST_AT = time.monotonic()


def _search_pixabay(settings: Any, query: str, kind: str, destination: Path,
                    api_key: str) -> tuple[dict[str, Any], bool]:
    cache_path = _cache_file(settings, destination, kind, query, api_key)
    cached = _read_search_cache(cache_path)
    if cached is not None:
        return cached, True

    endpoint = "https://pixabay.com/api/videos/" if kind == "video" else "https://pixabay.com/api/"
    params: dict[str, Any] = {
        "key": api_key,
        "q": query,
        "safesearch": "true",
        "per_page": 20,
        "orientation": "horizontal",
    }
    if kind == "photo":
        params.update({"image_type": "photo", "min_width": 1280, "min_height": 720})

    try:
        _pace_api_request()
        response = requests.get(endpoint, params=params, timeout=25)
        status = int(getattr(response, "status_code", 200))
        headers = getattr(response, "headers", {}) or {}
        if status == 429:
            raise PixabayError(
                "Pixabay API вернул 429 Too Many Requests",
                status_code=429,
                retry_after=headers.get("Retry-After") or headers.get("X-RateLimit-Reset"),
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Pixabay API returned a non-object JSON response")
        _write_search_cache(cache_path, payload)
        return payload, False
    except PixabayError:
        raise
    except (requests.RequestException, ValueError) as exc:
        detail = str(exc).replace(api_key, "[REDACTED]")
        raise PixabayError(f"Ошибка поиска Pixabay ({kind}): {detail}") from exc


def _download_candidate(media_url: str, target: Path, api_key: str) -> None:
    if target.is_file() and target.stat().st_size >= 1024:
        return

    _raise_if_cdn_cooldown_active()
    temporary = target.with_suffix(target.suffix + ".part")
    last_error: Exception | None = None
    for attempt in range(MAX_DOWNLOAD_ATTEMPTS):
        temporary.unlink(missing_ok=True)
        try:
            with requests.get(media_url, stream=True, timeout=60) as response:
                status = int(getattr(response, "status_code", 200))
                headers = getattr(response, "headers", {}) or {}
                if status == 429:
                    retry_after = headers.get("Retry-After")
                    host = urlsplit(media_url).hostname or "cdn.pixabay.com"
                    state = _persist_cdn_cooldown(retry_after, host=host)
                    wait_seconds = max(1, int(math.ceil(state["cooldown_until_epoch"] - state["observed_at_epoch"])))
                    raise PixabayError(
                        f"CDN Pixabay вернул 429 Too Many Requests; пауза сохранена на {wait_seconds} сек.",
                        status_code=429,
                        retry_after=str(retry_after) if retry_after is not None else None,
                    )
                response.raise_for_status()
                content_length = int(headers.get("Content-Length") or 0)
                if content_length > MAX_DOWNLOAD_BYTES:
                    raise PixabayError("Материал Pixabay превышает лимит 100 MB")
                written = 0
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > MAX_DOWNLOAD_BYTES:
                            raise PixabayError("Материал Pixabay превысил лимит 100 MB при скачивании")
                        output.write(chunk)
                if written < 1024:
                    raise PixabayError("Pixabay вернул слишком маленький или пустой файл")
            temporary.replace(target)
            return
        except PixabayError:
            temporary.unlink(missing_ok=True)
            raise
        except requests.HTTPError as exc:
            temporary.unlink(missing_ok=True)
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # A non-rate-limited HTTP error is candidate-specific; try another candidate.
            raise PixabayError(f"HTTP ошибка CDN Pixabay: {exc}", status_code=status) from exc
        except requests.RequestException as exc:
            temporary.unlink(missing_ok=True)
            last_error = exc
            if attempt + 1 < MAX_DOWNLOAD_ATTEMPTS:
                time.sleep(0.5 * (attempt + 1))
                continue
            break
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise PixabayError(f"Не удалось сохранить материал Pixabay: {exc}") from exc

    detail = str(last_error or "unknown download error").replace(api_key, "[REDACTED]")
    raise PixabayError(f"Не удалось скачать материал Pixabay после {MAX_DOWNLOAD_ATTEMPTS} попыток: {detail}") from last_error


def _candidate_rows(payload: dict[str, Any], query: str, kind: str,
                    cache_hit: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for hit in payload.get("hits") or []:
        if not isinstance(hit, dict):
            continue
        if kind == "video":
            variants = hit.get("videos") or {}
            usable = [value for value in variants.values()
                      if isinstance(value, dict) and value.get("url")]
            if not usable:
                continue
            variant = max(
                usable,
                key=lambda value: int(value.get("width") or 0) * int(value.get("height") or 0),
            )
            media_url = str(variant["url"])
        else:
            variant = None
            media_url = str(hit.get("largeImageURL") or hit.get("webformatURL") or "")
            if not media_url:
                continue
        score = _candidate_score(query, hit, kind, variant)
        if score >= 0:
            rows.append({
                "score": score,
                "hit": hit,
                "variant": variant,
                "media_url": media_url,
                "query": query,
                "cache_hit": cache_hit,
            })
    return rows


def fetch_pixabay(settings: Any, query: str | Iterable[str], kind: str,
                  destination: Path, *, exclude_source_urls: set[str] | None = None,
                  download_budget: dict[str, int] | None = None) -> dict[str, Any] | None:
    """Find and download the best relevant candidate across up to four search phrases.

    Search JSON is cached for 24 hours. A CDN 429 stops immediately; ordinary candidate
    download failures permit trying another ranked candidate, up to a small fixed limit.
    """
    api_key = str(getattr(settings, "pixabay_api_key", "") or "").strip()
    if not api_key:
        return None
    kind = "video" if kind == "video" else "photo"
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    raw_queries = [query] if isinstance(query, str) else list(query or [])
    queries: list[str] = []
    seen_queries: set[str] = set()
    for raw in raw_queries:
        clean = " ".join(str(raw or "").strip().split())[:100]
        normalized = clean.casefold()
        if clean and normalized not in seen_queries:
            queries.append(clean)
            seen_queries.add(normalized)
        if len(queries) >= MAX_SEARCH_QUERIES:
            break
    if not queries:
        return None

    # A mutable budget can be shared by multiple searches belonging to one beat.
    budget = download_budget if download_budget is not None else {
        "limit": MAX_DOWNLOAD_CANDIDATES,
        "used": 0,
    }

    if not isinstance(budget, dict):
        raise TypeError("download_budget must be a dictionary")

    try:
        budget_limit = int(budget.get("limit", MAX_DOWNLOAD_CANDIDATES))
        budget_used = int(budget.get("used", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid Pixabay download budget") from exc

    if budget_limit < 0 or budget_used < 0:
        raise ValueError("Pixabay download budget cannot be negative")

    budget["limit"] = budget_limit
    budget["used"] = budget_used

    # Do not issue more API requests when this beat has exhausted its budget.
    if budget_used >= budget_limit:
        return None

    candidates: list[dict[str, Any]] = []
    for clean_query in queries:
        payload, was_cached = _search_pixabay(settings, clean_query, kind, destination, api_key)
        candidates.extend(_candidate_rows(payload, clean_query, kind, was_cached))

    excluded = exclude_source_urls or set()
    unique: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        hit = candidate["hit"]
        identity = str(hit.get("pageURL") or hit.get("id") or candidate["media_url"])
        if identity in excluded:
            continue
        current = unique.get(identity)
        if current is None or candidate["score"] > current["score"]:
            unique[identity] = candidate
    ranked = sorted(unique.values(), key=lambda row: row["score"], reverse=True)
    if not ranked:
        return None

    used_before = int(budget["used"])
    per_call_limit = min(
        int(budget["limit"]),
        used_before + MAX_DOWNLOAD_CANDIDATES,
    )

    failures: list[str] = []
    for candidate in ranked:
        if int(budget["used"]) >= per_call_limit:
            break

        # Count a candidate once, regardless of its internal network retries.
        budget["used"] = int(budget["used"]) + 1
        selected = candidate["hit"]
        media_url = candidate["media_url"]
        source_identity = selected.get("id") or selected.get("pageURL") or media_url
        digest = hashlib.sha256(
            f"{kind}:{candidate['query']}:{source_identity}".encode("utf-8")
        ).hexdigest()[:12]
        target = destination / f"pixabay_{digest}{'.mp4' if kind == 'video' else '.jpg'}"
        try:
            _download_candidate(media_url, target, api_key)
        except PixabayError as exc:
            if exc.is_rate_limited:
                raise
            failures.append(str(exc))
            continue

        variant = candidate["variant"]
        return {
            "path": str(target.resolve()),
            "media_type": "video" if kind == "video" else "image",
            "pixabay_type": selected.get("type") or ("video" if kind == "video" else "photo"),
            "source": "Pixabay",
            "source_url": selected.get("pageURL"),
            "creator": selected.get("user"),
            "license_name": "Pixabay Content License — human review required",
            "rights_status": "review_required",
            "query": candidate["query"],
            "candidate_count": len(ranked),
            "selection_score": round(candidate["score"], 3),
            "selected_tags": selected.get("tags", ""),
            "selected_resolution": (
                [variant.get("width"), variant.get("height")] if variant else [
                    selected.get("imageWidth") or selected.get("webformatWidth"),
                    selected.get("imageHeight") or selected.get("webformatHeight"),
                ]
            ),
            "search_cache_hit": candidate["cache_hit"],
            "search_queries_checked": len(queries),
            "download_candidates_tried": int(budget["used"]) - used_before,
        }

    # Do not disguise a temporary CDN failure as "no search results".
    raise PixabayError(
        f"Не удалось скачать ни одного из {min(len(ranked), MAX_DOWNLOAD_CANDIDATES)} "
        f"лучших кандидатов Pixabay: {' | '.join(failures)}"
    )
