"""Resolve local, URL and Pixabay visual assets for Visual Story V2."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .visual_story_v2 import Shot, VisualObject


class AssetResolutionError(RuntimeError):
    pass


class VisualAssetResolver:
    def __init__(
        self,
        settings: Any,
        cache_dir: str | Path,
        base_dir: str | Path | None = None,
    ):
        self.settings = settings

        self.cache_dir = Path(cache_dir).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.base_dir = (
            Path(base_dir).resolve()
            if base_dir
            else Path.cwd().resolve()
        )

        self.ffmpeg = str(settings.ffmpeg)

    def _resolve_local_path(self, raw_path: str) -> Path:
        path = Path(raw_path)

        if not path.is_absolute():
            path = self.base_dir / path

        path = path.resolve()

        if not path.is_file():
            raise AssetResolutionError(
                f"Local asset does not exist: {path}"
            )

        return path

    def _download_url(self, url: str) -> Path:
        parsed = urllib.parse.urlparse(url)

        if parsed.scheme not in ("http", "https"):
            raise AssetResolutionError(
                "Only HTTP(S) asset URLs are allowed"
            )

        suffix = Path(parsed.path).suffix[:10]

        key = hashlib.sha256(
            url.encode("utf-8")
        ).hexdigest()

        destination = self.cache_dir / f"url_{key}{suffix}"

        if destination.is_file():
            return destination

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "KadrFarm-Visual-Story-V2",
            },
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=45,
            ) as response:
                data = response.read()

        except Exception as exc:
            raise AssetResolutionError(
                f"Unable to download asset: {exc}"
            ) from exc

        if not data:
            raise AssetResolutionError("Downloaded asset is empty")

        destination.write_bytes(data)

        return destination

    def resolve(
        self,
        obj: VisualObject,
    ) -> Path:
        """Resolve one image or video object."""

        if obj.kind not in ("image", "video"):
            raise ValueError(
                f"{obj.kind} is not a media object"
            )

        content = obj.content

        if content.get("path"):
            return self._resolve_local_path(
                str(content["path"])
            )

        if content.get("url"):
            return self._download_url(
                str(content["url"])
            )

        queries = content.get("search_queries") or []

        if isinstance(queries, str):
            queries = [queries]

        queries = [
            str(query).strip()
            for query in queries
            if str(query).strip()
        ][:4]

        if not queries:
            raise AssetResolutionError(
                f"{obj.object_id}: no usable search queries"
            )

        # Use the existing project implementation.
        # This keeps Pixabay API handling, caching and rate limiting
        # consistent with the rest of KadrFarm.
        from .pixabay_media import (
            fetch_pixabay,
            PixabayError,
        )

        errors = []

        kind = "video" if obj.kind == "video" else "photo"

        target_dir = (
            self.cache_dir
            / "pixabay"
            / obj.object_id
        )

        target_dir.mkdir(parents=True, exist_ok=True)

        try:
            asset = fetch_pixabay(
                self.settings,
                queries,
                kind,
                target_dir,
            )

            if asset and asset.get("path"):
                path = Path(asset["path"]).resolve()

                if path.is_file():
                    return path

        except PixabayError as exc:
            errors.append(str(exc))

        except Exception as exc:
            errors.append(str(exc))

        raise AssetResolutionError(
            f"{obj.object_id}: Pixabay asset unavailable. "
            + "; ".join(errors)
        )

    def extract_video_frames(
        self,
        video_path: Path,
        output_dir: str | Path,
        *,
        width: int,
        height: int,
        fps: int,
        duration_sec: float,
        loop_allowed: bool = True,
    ) -> list[Path]:
        """Extract a deterministic video frame sequence using FFmpeg."""

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        existing = sorted(
            output_dir.glob("frame_*.png")
        )

        required = max(
            1,
            round(duration_sec * fps),
        )

        if len(existing) >= required:
            return existing[:required]

        for old in existing:
            old.unlink(missing_ok=True)

        frame_pattern = output_dir / "frame_%06d.png"

        command = [
            self.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
        ]

        if loop_allowed:
            command += [
                "-stream_loop",
                "-1",
            ]

        command += [
            "-i",
            str(video_path),
            "-vf",
            (
                f"fps={fps},"
                f"scale={width}:{height}:"
                "force_original_aspect_ratio=increase,"
                f"crop={width}:{height},"
                "setsar=1"
            ),
            "-frames:v",
            str(required),
            str(frame_pattern),
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise AssetResolutionError(
                "FFmpeg video extraction failed: "
                + result.stderr[-3000:]
            )

        frames = sorted(
            output_dir.glob("frame_*.png")
        )

        if not frames:
            raise AssetResolutionError(
                f"No frames extracted from video: {video_path}"
            )

        return frames

    def prepare_shot(
        self,
        shot: Shot,
        *,
        width: int,
        height: int,
        fps: int,
        duration_sec: float,
    ) -> dict[str, Any]:
        """Prepare media required by a shot."""

        assets: dict[str, Any] = {}

        for obj in shot.objects:
            if obj.kind == "image":
                assets[obj.object_id] = self.resolve(obj)

            elif obj.kind == "video":
                video_path = self.resolve(obj)

                target = (
                    self.cache_dir
                    / "video_frames"
                    / shot.shot_id
                    / obj.object_id
                )

                frames = self.extract_video_frames(
                    video_path,
                    target,
                    width=width,
                    height=height,
                    fps=fps,
                    duration_sec=duration_sec,
                    loop_allowed=bool(
                        obj.content.get("loop_allowed", True)
                    ),
                )

                assets[obj.object_id] = frames

            elif obj.kind == "group":
                # Group children are ordinary VisualObject payloads.
                # Prepare child media as well.
                for child_data in obj.content.get("children", []):
                    child = VisualObject.model_validate(child_data)

                    if child.kind == "image":
                        assets[child.object_id] = self.resolve(child)

                    elif child.kind == "video":
                        video_path = self.resolve(child)

                        target = (
                            self.cache_dir
                            / "video_frames"
                            / shot.shot_id
                            / child.object_id
                        )

                        assets[child.object_id] = self.extract_video_frames(
                            video_path,
                            target,
                            width=width,
                            height=height,
                            fps=fps,
                            duration_sec=duration_sec,
                            loop_allowed=bool(
                                child.content.get("loop_allowed", True)
                            ),
                        )

        return assets