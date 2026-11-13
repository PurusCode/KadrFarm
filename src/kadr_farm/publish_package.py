"""Human-review publication package for semantic production runs."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import read_json, write_json


def create_publish_package(script_path: Path, storyboard_path: Path,
                           output_dir: Path) -> dict[str, Any]:
    script = read_json(script_path)
    storyboard = read_json(storyboard_path)
    external_media: list[dict[str, Any]] = []
    all_media: list[dict[str, Any]] = []
    seen: set[str] = set()
    claims: list[dict[str, Any]] = []
    for scene in storyboard.get("scenes", []):
        sid = scene.get("id")
        for claim in scene.get("claims", []):
            claims.append({
                "scene_id": sid,
                "text": claim.get("text"),
                "source_urls": claim.get("source_urls", []),
                "status": claim.get("status", "verify_before_publish"),
                "source_availability": claim.get("source_availability", []),
                "verified_by_human": bool(claim.get("verified_by_human", False)),
            })
        refs = (scene.get("media_refs") or {}).get("visuals", [])
        for ref in refs:
            path = str(ref.get("path") or "")
            if not path or path in seen:
                continue
            seen.add(path)
            row = {
                "scene_id": sid,
                "path": path,
                "media_type": ref.get("media_type"),
                "source": ref.get("source"),
                "source_url": ref.get("source_url"),
                "creator": ref.get("creator"),
                "license_name": ref.get("license_name"),
                "rights_status": ref.get("rights_status", "review_required"),
                "sha256": ref.get("sha256"),
            }
            all_media.append(row)
            if str(row.get("source") or "").lower() == "pixabay":
                external_media.append(row)

    # Script-level fact checks are also untrusted until a human reviews them.
    for fact in script.get("fact_checks", []) if isinstance(script.get("fact_checks"), list) else []:
        claim_text = str(fact.get("claim") or fact.get("text") or "").strip()
        if not claim_text:
            continue
        candidate_url = str(fact.get("source_url") or "").strip()
        claims.append({
            "scene_id": None,
            "text": claim_text,
            "source_urls": [candidate_url] if candidate_url.startswith(("http://", "https://")) else [],
            "status": "verify_before_publish" if candidate_url.startswith(("http://", "https://")) else "no_source",
            "source_availability": ["not_checked"] if candidate_url.startswith(("http://", "https://")) else [],
            "verified_by_human": False,
        })
    for scene in storyboard.get("scenes", []):
        chart = scene.get("chart_data")
        if chart and chart.get("source_url"):
            claims.append({
                "scene_id": scene.get("id"),
                "text": f"Данные для графика «{chart.get('title','')}»: {chart.get('methodology','')}",
                "source_urls": [chart["source_url"]],
                "status": "verify_before_publish",
                "source_availability": ["not_checked"],
                "verified_by_human": False,
            })

    title = str(script.get("title") or storyboard.get("title") or "Без названия")
    summary = str(script.get("description_summary") or "Авторский видеоматериал.")
    hashtags = script.get("hashtags") if isinstance(script.get("hashtags"), list) else []
    unresolved_claims = [c for c in claims if c.get("status") != "verified" or not c.get("verified_by_human")]
    unreviewed_media = [m for m in external_media if m.get("rights_status") != "approved"]
    payload = {
        "title": title,
        "description_summary": summary,
        "hashtags": hashtags,
        "fact_review_status": "human_review_required" if unresolved_claims else "human_approved",
        "claims": claims,
        "claims_requiring_review": unresolved_claims,
        "used_media": all_media,
        "external_media_attribution": external_media,
        "rights_review_status": "human_review_required" if unreviewed_media else "approved_or_no_external_media",
        "external_media_requiring_review": unreviewed_media,
        "music_used": False,
        "sfx_used": False,
        "audio_cues_not_produced": [cue for scene in storyboard.get("scenes", [])
                                    for cue in scene.get("audio_sfx", [])],
        "music_cues_not_produced": [scene.get("music_cue") for scene in storyboard.get("scenes", [])
                                    if scene.get("music_cue")],
        "auto_publication": False,
        "publication_status": "not_published",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "publish.json", payload)
    lines = [title, "", summary]
    if hashtags:
        lines += ["", " ".join(str(x) for x in hashtags)]
    if external_media:
        lines += ["", "ИСТОЧНИКИ ВНЕШНИХ ВИЗУАЛЬНЫХ МАТЕРИАЛОВ:"]
        for item in external_media:
            creator = item.get("creator") or "Автор не указан в API"
            url = item.get("source_url") or "Ссылка на страницу материала отсутствует"
            lines.append(f"Pixabay — {creator}: {url}")
    if unresolved_claims:
        lines += ["", "ПЕРЕД ПУБЛИКАЦИЕЙ ПРОВЕРИТЬ ФАКТЫ:"]
        for claim in unresolved_claims:
            lines.append(f"- {claim['text']}")
            for url in claim.get("source_urls", []):
                lines.append(f"  Источник-кандидат: {url}")
    if unreviewed_media:
        lines += ["", "ПРАВА НА МЕДИА: требуется ручная проверка лицензии каждого внешнего материала."]
    lines += ["", "Автоматическая публикация не выполнялась."]
    (output_dir / "publish.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload
