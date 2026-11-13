"""Python-only validation and provenance enrichment for semantic scenes."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib
import requests

from .common import read_json, write_json
from .models import (
    Scene, Claim, ClaimStatus, SourceAvailability, AudioSFX,
    AssetStrategy, VisualType,
)
from .storyboard import validate_storyboard_coverage


def _probe_source(url: str, timeout: float = 8.0) -> SourceAvailability:
    try:
        response = requests.head(url, allow_redirects=True, timeout=timeout,
                                 headers={"User-Agent": "AI-Video-Factory-RU/semantic-check"})
        if response.status_code == 405:
            response = requests.get(url, allow_redirects=True, timeout=timeout,
                                    headers={"User-Agent": "AI-Video-Factory-RU/semantic-check"},
                                    stream=True)
        return SourceAvailability.REACHABLE if response.status_code < 400 else SourceAvailability.UNREACHABLE
    except requests.RequestException:
        return SourceAvailability.UNREACHABLE


def enrich_claims(raw_claims: list[dict[str, Any]], *, probe_urls: bool = False) -> list[dict[str, Any]]:
    claims = []
    for raw in raw_claims:
        urls = list(raw.get("source_urls") or [])
        availability = [
            _probe_source(url) if probe_urls else SourceAvailability.NOT_CHECKED
            for url in urls
        ]
        claims.append(Claim(
            text=str(raw["text"]),
            source_urls=urls,
            status=ClaimStatus.VERIFY_BEFORE_PUBLISH if urls else ClaimStatus.NO_SOURCE,
            source_availability=availability,
            verified_by_human=False,
        ).model_dump(mode="json"))
    return claims


def validate_storyboard(script_path: Path, storyboard_path: Path,
                        output_path: Path | None = None,
                        *, probe_urls: bool = False) -> dict[str, Any]:
    script = read_json(script_path)
    storyboard = read_json(storyboard_path)
    validate_storyboard_coverage(script, storyboard)
    warnings: list[str] = []
    validated = []
    for raw in storyboard.get("scenes", []):
        raw = dict(raw)
        raw["claims"] = enrich_claims(raw.get("claims", []), probe_urls=probe_urls)
        # Audio effects have no source or license until a real, reviewed asset is attached.
        raw["audio_sfx"] = [
            AudioSFX(name=str(x["name"]), purpose=str(x["purpose"]),
                     source_url=None, license_name=None,
                     rights_status="not_cleared").model_dump(mode="json")
            for x in raw.get("audio_sfx", [])
        ]
        strategy = raw.get("asset_strategy")
        if strategy == AssetStrategy.GENERATED.value:
            raise ValueError(
                f"Сцена {raw.get('id')}: generated provider не подключён. "
                "В первой версии используй stock/Pixabay или rendered/internal_renderer."
            )
        if strategy in (AssetStrategy.MANUAL.value, AssetStrategy.SCREENCAST.value):
            warnings.append(f"{raw.get('id')}: требуется ручной визуальный материал")
        if raw.get("visual_type") == VisualType.CHART.value:
            if not raw.get("chart_data"):
                raise ValueError(f"{raw.get('id')}: chart без chart_data")
        if not raw.get("claims"):
            warnings.append(f"{raw.get('id')}: claims отсутствуют; проверь фактические утверждения")
        scene = Scene.model_validate(raw)
        if len(scene.on_screen_text) and any(len(t) > 40 for t in scene.on_screen_text):
            raise ValueError(f"{scene.id}: слишком длинная экранная надпись")
        validated.append(scene.model_dump(mode="json"))
    storyboard["scenes"] = validated
    storyboard["validation"] = {
        "status": "passed_with_warnings" if warnings else "passed",
        "warnings": warnings,
        "source_probe_performed": bool(probe_urls),
        "fact_claims_are_human_verified": False,
    }
    if output_path:
        write_json(output_path, storyboard)
    return storyboard


def validate_timing(scene: dict[str, Any]) -> list[str]:
    warnings = []
    duration = scene.get("duration_sec")
    if duration is None:
        return warnings
    duration = float(duration)
    if duration < 2:
        warnings.append(f"{scene['id']}: визуальный акцент короче 2 секунд")
    elif duration < 3:
        warnings.append(f"{scene['id']}: сцена короче рекомендуемого диапазона")
    if duration > 30:
        warnings.append(f"{scene['id']}: сцена длиннее 30 секунд; проверь необходимость деления")
    elif duration > 25:
        warnings.append(f"{scene['id']}: сцена длиннее целевого диапазона 3–25 секунд")
    return warnings
