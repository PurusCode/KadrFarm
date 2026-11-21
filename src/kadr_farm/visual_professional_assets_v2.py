"""Professional media resolver for KadrFarm Visual Story V2.

Unlike the first experimental resolver, this module keeps videos as video files.
It never expands a video into thousands of PNG frames. All downloads are bounded,
written atomically, and the existing Pixabay policy/candidate scorer is reused.
"""
from __future__ import annotations

import hashlib
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .visual_story_v2 import Shot, VisualObject


class ProfessionalAssetError(RuntimeError):
    """Raised when a requested visual asset cannot be prepared safely."""


@dataclass(frozen=True)
class ResolvedMedia:
    object_id: str
    path: Path
    media_type: str
    source: str = "local"
    source_url: str | None = None
    creator: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_uri(self) -> str:
        return self.path.resolve().as_uri()


class ProfessionalAssetResolver:
    """Resolve image/video objects to bounded local files for browser compositing."""

    def __init__(
        self,
        settings: Any,
        cache_dir: str | Path,
        base_dir: str | Path | None = None,
        max_download_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        self.settings = settings
        self.cache_dir = Path(cache_dir).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.base_dir = Path(base_dir or Path.cwd()).resolve()
        self.max_download_bytes = int(max_download_bytes)
        if self.max_download_bytes <= 0:
            raise ValueError("max_download_bytes must be positive")

    def _local_path(self, raw: str) -> Path:
        value = str(raw).strip()
        path = Path(value)
        if not path.is_absolute():
            path = self.base_dir / path
        path = path.resolve()
        if not path.is_file():
            raise ProfessionalAssetError(f"Local media file does not exist: {path}")
        return path

    def _download(self, url: str) -> Path:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProfessionalAssetError("Media URL must be an absolute HTTP(S) URL")
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        suffix = Path(parsed.path).suffix.lower()[:10]
        destination = self.cache_dir / f"remote_{digest}{suffix}"
        if destination.is_file() and 0 < destination.stat().st_size <= self.max_download_bytes:
            return destination

        request = urllib.request.Request(
            url,
            headers={"User-Agent": "KadrFarm-Visual-Engine/2.0"},
        )
        temporary = destination.with_suffix(destination.suffix + ".part")
        total = 0
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                content_length = int(response.headers.get("Content-Length") or 0)
                if content_length > self.max_download_bytes:
                    raise ProfessionalAssetError(
                        f"Remote media exceeds the {self.max_download_bytes} byte limit"
                    )
                with temporary.open("wb") as stream:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self.max_download_bytes:
                            raise ProfessionalAssetError(
                                "Remote media exceeded the configured download limit"
                            )
                        stream.write(chunk)
            if total < 1024:
                raise ProfessionalAssetError("Remote media response is empty or suspiciously small")
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    def resolve_object(self, obj: VisualObject) -> ResolvedMedia:
        if obj.kind not in {"image", "video"}:
            raise ValueError(f"{obj.object_id} is not an image/video object")

        media_type = "video" if obj.kind == "video" else "image"
        content = obj.content
        metadata: dict[str, Any] = {}

        if content.get("path"):
            path = self._local_path(str(content["path"]))
            source = "local"
        elif content.get("url"):
            path = self._download(str(content["url"]))
            source = "url"
        else:
            queries = content.get("search_queries") or []
            if isinstance(queries, str):
                queries = [queries]
            queries = [str(item).strip() for item in queries if str(item).strip()][:4]
            if not queries:
                raise ProfessionalAssetError(
                    f"{obj.object_id}: provide path, url or search_queries"
                )
            from .pixabay_media import fetch_pixabay

            target = self.cache_dir / "pixabay" / obj.object_id
            result = fetch_pixabay(
                self.settings,
                queries,
                "video" if media_type == "video" else "photo",
                target,
            )
            if not result or not result.get("path"):
                raise ProfessionalAssetError(
                    f"{obj.object_id}: Pixabay returned no suitable {media_type}"
                )
            path = Path(result["path"]).resolve()
            source = "Pixabay"
            metadata = dict(result)

        if not path.is_file():
            raise ProfessionalAssetError(f"Resolved file disappeared: {path}")
        if path.stat().st_size <= 0:
            raise ProfessionalAssetError(f"Resolved media file is empty: {path}")

        source_url = metadata.get("source_url")
        creator = metadata.get("creator")
        return ResolvedMedia(
            object_id=obj.object_id,
            path=path,
            media_type=media_type,
            source=source,
            source_url=str(source_url) if source_url else None,
            creator=str(creator) if creator else None,
            metadata=metadata,
        )

    def resolve_shot(self, shot: Shot) -> dict[str, ResolvedMedia]:
        """Resolve media recursively, including children of group objects."""
        result: dict[str, ResolvedMedia] = {}
        seen: set[str] = set()

        def visit(obj: VisualObject, depth: int = 0) -> None:
            if depth > 8:
                raise ProfessionalAssetError("Visual group nesting exceeds 8 levels")
            if obj.object_id in seen:
                raise ProfessionalAssetError(
                    f"Duplicate object_id in nested composition: {obj.object_id}"
                )
            seen.add(obj.object_id)
            if obj.kind in {"image", "video"}:
                result[obj.object_id] = self.resolve_object(obj)
            elif obj.kind == "group":
                for child_data in obj.content.get("children", []):
                    child = VisualObject.model_validate(child_data)
                    visit(child, depth + 1)

        for item in shot.objects:
            visit(item)
        return result
