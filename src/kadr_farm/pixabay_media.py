"""Pixabay-only media adapter for the semantic pipeline. Pexels is never called."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
import requests


class PixabayError(RuntimeError):
    pass


def _safe_name(value: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in value.lower())[:48] or "asset"


def fetch_pixabay(settings: Any, query: str, kind: str, destination: Path) -> dict[str, Any] | None:
    """Download one landscape photo or video. Returns None when key/results unavailable."""
    api_key = str(getattr(settings, "pixabay_api_key", "") or "").strip()
    if not api_key:
        return None
    kind = "video" if kind == "video" else "photo"
    destination.mkdir(parents=True, exist_ok=True)
    clean_query = " ".join(query.strip().split())[:100]
    if not clean_query:
        return None
    digest = hashlib.sha256(f"{kind}:{clean_query}".encode()).hexdigest()[:12]
    endpoint = "https://pixabay.com/api/videos/" if kind == "video" else "https://pixabay.com/api/"
    try:
        response = requests.get(endpoint, params={
            "key": api_key, "q": clean_query, "safesearch": "true",
            "per_page": 8, "orientation": "horizontal",
        }, timeout=25)
        if response.status_code == 429:
            raise PixabayError("Pixabay вернул 429 Too Many Requests")
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        detail = str(exc).replace(api_key, "[REDACTED]")
        raise PixabayError(f"Ошибка Pixabay ({kind}): {detail}") from exc

    hits = payload.get("hits") or []
    if not hits:
        return None
    selected = None
    media_url = None
    if kind == "video":
        for hit in hits:
            variants = hit.get("videos") or {}
            selected_variant = variants.get("medium") or variants.get("large") or variants.get("small")
            if selected_variant and selected_variant.get("url"):
                selected, media_url = hit, selected_variant["url"]
                break
    else:
        selected = hits[0]
        media_url = selected.get("largeImageURL") or selected.get("webformatURL")
    if not selected or not media_url:
        return None

    extension = ".mp4" if kind == "video" else ".jpg"
    target = destination / f"pixabay_{digest}{extension}"
    if not target.is_file():
        try:
            with requests.get(media_url, stream=True, timeout=60) as download:
                download.raise_for_status()
                content_length = int(download.headers.get("Content-Length") or 0)
                max_bytes = 100 * 1024 * 1024
                if content_length > max_bytes:
                    raise PixabayError("Pixabay asset превышает лимит 100 MB")
                written = 0
                with target.open("wb") as out:
                    for chunk in download.iter_content(1024 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > max_bytes:
                            raise PixabayError("Pixabay asset превысил лимит 100 MB при скачивании")
                        out.write(chunk)
                if written < 1024:
                    raise PixabayError("Pixabay вернул слишком маленький или пустой файл")
        except PixabayError:
            target.unlink(missing_ok=True)
            raise
        except requests.RequestException as exc:
            target.unlink(missing_ok=True)
            detail = str(exc).replace(api_key, "[REDACTED]")
            raise PixabayError(f"Не удалось скачать материал Pixabay: {detail}") from exc

    return {
        "path": str(target.resolve()),
        "media_type": "video" if kind == "video" else "image",
        "source": "Pixabay",
        "source_url": selected.get("pageURL"),
        "creator": selected.get("user"),
        "license_name": "Pixabay Content License — human review required",
        "rights_status": "review_required",
        "query": clean_query,
    }
