"""Autonomous director: storyboard -> direction -> critique -> repair -> evidence audit
-> Kokoro -> visuals (timed from measured WAV) -> edit -> QC.

Изменения v2.1: скорость 1.0, паузы 0.22 с + панчлайн-паузы, рендер по реальной
длительности, фото-вставки с Ken Burns, критик не блокирует прогон (human-queue).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import wave
from copy import copy
from pathlib import Path
from typing import Any

from PIL import Image

from .common import read_json
from .config import load_settings
from .storyboard import create_storyboard
from .html_renderer import render_html_video
from .llm_structured import StructuredLLMRouter
from .director_models import DirectorBatch, CritiqueBatch
from .director_visuals import (render_stock_overlay, audit_visual_assets,
                               audit_final_video_frames)
from .director_sources import audit_claim_sources
from .director_audio import build_master_audio, SAMPLE_RATE
from .director_assembler import assemble_director_video
from .text_normalization import normalize_for_speech
from .kokoro_engine import synthesize_kokoro
from .pixabay_media import fetch_pixabay, PixabayError, MAX_DOWNLOAD_CANDIDATES
from .director_shots import plan_shots

log = logging.getLogger("kadr_farm.production_director")

DIRECTOR_POLICY_VERSION = "semantic-media-2.0"

DIRECTOR_SYSTEM = """Ты — главный режиссёр монтажа русскоязычного технологического видеоканала.
Тебе дана готовая раскадровка с точными voice_text. Не переписывай и не сокращай речь.
Раздели voice_text каждой сцены на последовательные точные фрагменты spoken_text.
Объединение фрагментов в исходном порядке должно в точности сохранить voice_text.

ГЛАВНЫЙ РЕДАКТОРСКИЙ ПРИНЦИП — ОДНА МЫСЛЬ, ОДИН ОСНОВНОЙ ВИЗУАЛ:
- Обычно одна законченная мысль должна сопровождаться одним beat и одним основным образом.
- Не назначай фиксированную длительность 2–4 секунды и не дроби речь по таймеру.
- Не создавай отдельный beat для каждого предложения, существительного, слова или пункта перечисления.
- Запятая, пауза, смена формулировки и длина фразы сами по себе не требуют нового кадра.
- Новый beat нужен, только когда меняется предмет объяснения, физическое действие,
  состояние процесса, причинно-следственный этап или самостоятельный визуальный аргумент.
- Несколько соседних фраз можно объединить в один beat, если они раскрывают одну мысль.
- Более длинную мысль при необходимости разделит shot-планировщик, но только по реальной
  смысловой смене. Не придумывай временные метки и не подгоняй изображение вместо речи.

ЗВУК — САМОСТОЯТЕЛЬНЫЙ КАНАЛ ОБЪЯСНЕНИЯ:
Вся существенная информация, факты, цифры, выводы и причинные связи должны быть понятны
из дикторской дорожки. Кадр усиливает и иллюстрирует речь, но не несёт единственную
важную информацию и не заменяет объяснение диктора.

ВЫБОР ФОТО, ВИДЕО И ГРАФИКИ ПО СМЫСЛУ:
1) stock / pixabay:
- Для конкретного статичного реального объекта, человека, устройства, места или обстановки
  предпочитай visual_type="photo": компьютер, смартфон, серверная, рабочее место,
  документы, человек за работой, офис, техника.
- Для реального физического действия, где важно движение (печатает, листает, нажимает,
  собирает, взаимодействует с устройством), предпочитай visual_type="video" или "broll".
- Если фото лучше показывает предмет, не требуй видео только ради движения.
- Для каждого stock beat укажи 2–4 конкретных английских search_queries с предметом,
  действием и/или контекстом кадра. Строй их по смыслу всего spoken_text, а не по одному
  случайному слову. Запросы должны описывать то, что реально может быть найдено на фото
  или видео Pixabay.
- Не используй абстрактные слова вроде "success", "optimization", "concept", "future",
  "abstract", "technology" без конкретного физического объекта.
2) rendered / internal_renderer:
- Используй локальную анимированную графику для невидимых механизмов, логики,
  причинно-следственных связей, токенизации, потоков данных, концептуальных сравнений
  и последовательностей состояний.
- Схема должна наглядно объяснять механизм, а не быть декоративной карточкой.
- Для реальных предметов и действий не подменяй уместный stock абстрактной графикой.

НЕ ИСПОЛЬЗУЙ КВОТЫ:
- Не чередуй stock и rendered искусственно и не ограничивай число фото/видео подряд.
- Не добавляй нерелевантные фото ради разнообразия или заданного процента.
- Соответствие конкретной произносимой мысли важнее количества фото, видео и графики.

ДОСТОВЕРНОСТЬ:
- Не создавай фальшивые интерфейсы браузера, поисковика, чата, мессенджера, терминала,
  панели приложения, сайта, документа, письма или отчёта.
- Не выдумывай названия авторов и организаций, даты, URL, цитаты, логотипы, версии,
  названия кнопок, результаты поиска, числа, проценты, метрики и другие фактические детали.
- Реалистичный интерфейс или документ разрешён только при наличии точного проверенного
  содержания в исходных материалах. Одного упоминания темы недостаточно.
- Условную ситуацию можно иллюстрировать нейтральным подходящим stock-кадром, но нельзя
  выдавать его за документальную запись конкретного события или доказательство факта.
- Не создавай графики и числовые сравнения без исходных данных. Условная схема должна
  быть явно концептуальной и не имитировать реальные измерения.

ЭКРАННЫЙ ТЕКСТ:
- on_screen_text — только короткая смысловая метка, ключевой термин или подпись к объекту.
- Надпись должна уже присутствовать в речи либо быть точно подтверждена материалами проекта.
- Не копируй на экран spoken_text, целое предложение или описание для художника.
- Не показывай выдуманные имена, даты, URL, цифры, цитаты, результаты или интерфейсные поля.
- Для схемы используй только реальные короткие подписи этапов; не придумывай данные.
- visual_description — внутренняя инструкция движку и никогда не видимый текст.

РАЗРЕШЁННЫЕ АДАПТЕРЫ — ТОЛЬКО ДВА:
- stock / pixabay: реальные фото или видеоматериалы с конкретными search_queries.
- rendered / internal_renderer: локальная оригинальная графика.
Не возвращай manual, screencast strategy, generated или другие провайдеры.
Для каждого beat заполни все поля схемы. Если claims/SFX/подписи отсутствуют —
верни пустые списки; если music_cue отсутствует — пустую строку; gen_prompt — null.
Не генерируй музыкальные мелодии и частые SFX: музыка подключается отдельным реальным
файлом при сведении. Возвращай ровно один план на каждый переданный scene_id.
"""

CRITIC_SYSTEM = """Ты — независимый AI-редактор и технический режиссёр.
Сравни каждый beat с точным spoken_text и editorial_intent. Проверяй:
1) semantic_relevance: кадр иллюстрирует именно произносимую мысль;
2) narration_coverage: текст полностью сохранён, без пропусков, повторов и перефразирования;
3) media_choice: статичный реальный объект — подходящее фото; физическое действие —
   подходящее видео; абстрактный механизм — объясняющая анимация/схема;
4) audio_independence: всю существенную информацию можно понять только на слух;
5) factual_integrity: нет выдуманных интерфейсов, имён, дат, URL, цифр, процентов, цитат,
   логотипов и результатов;
6) visual_clarity: короткие подписи не заменяют содержательный визуал и не копируют речь;
7) editorial_rhythm: смена плана оправдана сменой мысли, действия или визуального аргумента.
Не требуй смены кадра только из-за длительности фразы. Длинный beat допустим, когда одна
мысль действительно удерживается одним сильным образом. Несколько очень коротких beats
подряд без смысловой причины — замечание.
Существенное несоответствие или выдуманная фактическая деталь -> verdict=revise
с конкретным revision_brief. Эстетическое предпочтение само по себе не блокирует план.
relevance_score, feasibility_score, narration_coverage_score — целые 0..5;
factual_risk обязателен. problems=[] и revision_brief="" при отсутствии замечаний.
Верни ровно один review на scene_id."""

REPAIR_SYSTEM = """Ты — режиссёр-исполнитель. Исправь только переданные сцены по замечаниям критика.
Сохрани spoken_text как точные непрерывные фрагменты исходного voice_text, полностью
и в исходном порядке. Обычно одна законченная мысль остаётся в одном beat.
Не дроби речь ради 2–4 секунд, отдельного предложения, запятой или смены слов.
Выбирай фото для конкретных статичных объектов, видео для реальных физических действий,
локальную объясняющую графику для абстрактных механизмов. Для каждого stock beat —
2–4 конкретных английских search_queries. Не добавляй квоты и нерелевантные кадры.
Не выдумывай интерфейсы, факты, цифры, имена, даты, URL, цитаты или метрики.
Экранные подписи короткие и уже произнесённые/подтверждённые. Только stock/pixabay
либо rendered/internal_renderer. Верни полный DirectorBatch для всех переданных scene_id."""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _director_plan_fingerprint(scenes: list[dict[str, Any]]) -> str:
    payload = {"scenes": scenes, "director_policy_version": DIRECTOR_POLICY_VERSION}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _check_exact_coverage(plan: dict[str, Any], scene: dict[str, Any]) -> None:
    combined = " ".join(x["spoken_text"] for x in plan["beats"])
    if _norm(combined) != _norm(scene["voice_text"]):
        raise ValueError(f"{scene['id']}: spoken_text beats do not exactly preserve narration")
    if not plan["beats"]:
        raise ValueError(f"{scene['id']}: no visual beats")


def _make_director_plan(settings, scenes, batch_size, out_path):
    progress_path = out_path.with_suffix(".progress.json")
    source_fp = _director_plan_fingerprint(scenes)
    completed = {}
    if progress_path.exists():
        saved = read_json(progress_path)
        if saved.get("source_sha256") != source_fp:
            log.warning("Discarding director checkpoint created under a different narration or editorial policy")
            saved = {}
        completed = saved.get("completed", {})
    pending = [s for s in scenes if s["id"] not in completed]
    for offset in range(0, len(pending), batch_size):
        batch = pending[offset:offset + batch_size]
        payload = [{"scene_id": s["id"], "voice_text": s["voice_text"],
                    "meaning": s.get("meaning", ""), "purpose": s.get("purpose", "")} for s in batch]
        prompt = "Создай точный режиссёрский план для этих сцен:\n" + json.dumps(payload, ensure_ascii=False)
        result = StructuredLLMRouter(settings).complete(
            DirectorBatch, DIRECTOR_SYSTEM, prompt, temperature=0.35, max_tokens=8500).plans
        if {p.scene_id for p in result} != {s["id"] for s in batch} or len(result) != len(batch):
            raise ValueError("Director returned missing/duplicate scene IDs")
        invalid = []
        for p in result:
            pdata = p.model_dump(mode="json")
            scene = next(x for x in batch if x["id"] == p.scene_id)
            try:
                _check_exact_coverage(pdata, scene)
                completed[p.scene_id] = pdata
            except ValueError as exc:
                invalid.append({"scene": scene, "bad_plan": pdata, "error": str(exc)})
        if invalid:
            repair_payload = [{"scene_id": x["scene"]["id"], "voice_text": x["scene"]["voice_text"],
                               "previous_plan": x["bad_plan"], "validation_error": x["error"]} for x in invalid]
            corrected = StructuredLLMRouter(settings).complete(
                DirectorBatch, REPAIR_SYSTEM,
                "КРИТИЧЕСКАЯ ОШИБКА: spoken_text должен в точности покрывать исходную речь. "
                "Исправь только эти планы.\n" + json.dumps(repair_payload, ensure_ascii=False),
                temperature=0.05, max_tokens=8500).plans
            expected = {x["scene"]["id"] for x in invalid}
            if {p.scene_id for p in corrected} != expected or len(corrected) != len(expected):
                raise ValueError("Narration exact-coverage repair returned incomplete scene set")
            for p in corrected:
                pdata = p.model_dump(mode="json")
                scene = next(x["scene"] for x in invalid if x["scene"]["id"] == p.scene_id)
                _check_exact_coverage(pdata, scene)
                completed[p.scene_id] = pdata
        _atomic_json(progress_path, {"source_sha256": source_fp, "completed": completed, "status": "in_progress"})
        log.info("AI director checkpoint: %d/%d scenes", len(completed), len(scenes))
    plans = [completed[s["id"]] for s in scenes]
    _atomic_json(out_path, {"plans": plans, "status": "complete",
                            "scene_count": len(plans), "source_sha256": source_fp})
    _atomic_json(progress_path, {"source_sha256": source_fp, "completed": completed, "status": "complete"})
    return plans


def _critic_settings(settings):
    policy = os.getenv("DIRECTOR_CRITIC_PROVIDER", "auto").strip().lower()
    if policy == "auto":
        return settings
    if policy not in ("primary", "secondary"):
        raise ValueError("DIRECTOR_CRITIC_PROVIDER must be auto, primary or secondary")
    selected = [p for p in list(getattr(settings, "providers", []))
                if str(getattr(p, "name", "")).lower() == policy]
    if not selected:
        raise ValueError(f"DIRECTOR_CRITIC_PROVIDER={policy} is not configured in .env")
    clone = copy(settings)
    clone.providers = selected
    return clone


def _critique(settings, scenes, plans):
    payload = []
    plan_by = {p["scene_id"]: p for p in plans}
    for s in scenes:
        payload.append({"scene_id": s["id"], "voice_text": s["voice_text"],
                        "meaning": s.get("meaning"), "plan": plan_by[s["id"]]})
    result = StructuredLLMRouter(_critic_settings(settings)).complete(
        CritiqueBatch, CRITIC_SYSTEM,
        "Проведи независимую редакторскую проверку:\n" + json.dumps(payload, ensure_ascii=False),
        temperature=0.1, max_tokens=min(8500, 1800 + len(scenes) * 600))
    ids = {s["id"] for s in scenes}
    if {x.scene_id for x in result.reviews} != ids or len(result.reviews) != len(ids):
        raise ValueError("AI critic did not return exactly one review per scene")
    return result.model_dump(mode="json")


def _is_bad(review: dict[str, Any]) -> bool:
    return (review["verdict"] == "revise" or review["factual_risk"] == "high"
            or min(review["relevance_score"], review["feasibility_score"],
                   review["narration_coverage_score"]) < 3)


def _repair(settings, scenes, plans, critique):
    bad = {r["scene_id"]: r for r in critique["reviews"] if _is_bad(r)}
    if not bad:
        return plans
    scene_by = {s["id"]: s for s in scenes}
    plan_by = {p["scene_id"]: p for p in plans}
    payload = [{"scene_id": sid, "voice_text": scene_by[sid]["voice_text"],
                "meaning": scene_by[sid].get("meaning"), "current_plan": plan_by[sid],
                "critic_problems": r["problems"], "revision_brief": r["revision_brief"]}
               for sid, r in bad.items()]
    revised = StructuredLLMRouter(settings).complete(
        DirectorBatch, REPAIR_SYSTEM,
        "Исправь проблемные режиссёрские планы:\n" + json.dumps(payload, ensure_ascii=False),
        temperature=0.1, max_tokens=min(8500, 2200 + len(bad) * 1400)).plans
    if {p.scene_id for p in revised} != set(bad) or len(revised) != len(bad):
        raise ValueError("Auto-repair returned missing/duplicate scene IDs")
    out = []
    for old in plans:
        replacement = next((p.model_dump(mode="json") for p in revised if p.scene_id == old["scene_id"]), None)
        out.append(replacement or old)
    for plan in out:
        _check_exact_coverage(plan, scene_by[plan["scene_id"]])
    return out


def _validate_pixabay_asset(settings, asset, kind):
    path = Path(asset.get("path") or "")
    if not path.is_file():
        return False, "file_missing"
    if path.stat().st_size < 1024:
        return False, "file_too_small"
    if kind == "photo":
        try:
            with Image.open(path) as image:
                width, height = image.size
                image.verify()
            if width < 640 or height < 360:
                return False, f"image_resolution_too_small_{width}x{height}"
            return True, f"image_ok_{width}x{height}"
        except Exception as exc:
            return False, f"image_decode_failed_{type(exc).__name__}"
    try:
        result = subprocess.run(
            [str(settings.ffprobe), "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height", "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
        probe = json.loads(result.stdout)
        streams = [x for x in probe.get("streams", []) if x.get("codec_type") == "video"]
        if not streams:
            return False, "video_stream_missing"
        width = int(streams[0].get("width") or 0)
        height = int(streams[0].get("height") or 0)
        duration = float(probe.get("format", {}).get("duration") or 0)
        if width < 640 or height < 360:
            return False, f"video_resolution_too_small_{width}x{height}"
        if duration < 0.6:
            return False, f"video_duration_too_short_{duration:.2f}s"
        return True, f"video_ok_{width}x{height}_{duration:.2f}s"
    except Exception as exc:
        return False, f"video_probe_failed_{type(exc).__name__}"


def _asset_for_beat(settings, scene, beat, visuals_dir, warnings, index, duration,
                    used_source_urls=None):
    """Select a narration-relevant asset; approved stock is never silently replaced."""
    strategy = str(beat.get("asset_strategy") or "").strip().lower()
    used_source_urls = used_source_urls if used_source_urls is not None else set()

    if strategy == "stock":
        requested_kind = (
            "video"
            if beat.get("visual_type") in ("broll", "video")
            else "photo"
        )

        raw_queries = beat.get("search_queries") or []
        if isinstance(raw_queries, str):
            raw_queries = [raw_queries]

        queries = [
            str(q).strip()
            for q in raw_queries[:4]
            if str(q).strip()
        ]

        errors = []
        rate_limited = False

        # One candidate budget is shared by video and photo fallback.
        download_budget = {
            "limit": MAX_DOWNLOAD_CANDIDATES,
            "used": 0,
        }

        # Include previously used sources and candidates rejected for this beat.
        excluded_for_beat = set(used_source_urls)

        def find_valid_candidate(kind):
            while download_budget["used"] < download_budget["limit"]:
                before = download_budget["used"]

                try:
                    candidate = fetch_pixabay(
                        settings,
                        queries,
                        kind,
                        visuals_dir,
                        exclude_source_urls=excluded_for_beat,
                        download_budget=download_budget,
                    )
                except PixabayError as exc:
                    errors.append(f"{kind}: {exc}")
                    warnings.append(f"{scene['id']}: Pixabay {kind}: {exc}")
                    return None, None, bool(exc.is_rate_limited)

                if candidate is None:
                    return None, None, False

                # Defensive accounting for adapters or test doubles that do
                # not mutate the shared budget themselves.
                if download_budget["used"] == before:
                    download_budget["used"] += 1

                valid, check_note = _validate_pixabay_asset(
                    settings, candidate, kind
                )

                if valid:
                    return candidate, check_note, False

                errors.append(
                    f"{kind}: selected candidate rejected: {check_note}"
                )
                warnings.append(
                    f"{scene['id']}: Pixabay candidate rejected ({check_note})"
                )

                source_url = candidate.get("source_url")
                if not source_url:
                    return None, None, False

                excluded_for_beat.add(source_url)

            return None, None, False

        asset, check_note, rate_limited = find_valid_candidate(requested_kind)

        selected_kind = requested_kind
        selection_fallback = None
        fallback_reason = None

        if asset is None and requested_kind == "video" and not rate_limited:
            asset, check_note, photo_rate_limited = find_valid_candidate("photo")
            rate_limited = rate_limited or photo_rate_limited

            if asset is not None:
                selected_kind = "photo"
                selection_fallback = "video_to_photo"
                fallback_reason = "no_usable_video_candidate"

        if asset is None:
            failure_path = (
                visuals_dir / f"{scene['id']}_{index:02d}_asset_failure.json"
            )
            failure_path.parent.mkdir(parents=True, exist_ok=True)

            failure = {
                "status": "manual_review_required",
                "scene_id": scene["id"],
                "beat_index": index,
                "shot_id": beat.get("shot_id"),
                "source_beat_id": beat.get("source_beat_id"),
                "spoken_text": beat.get("spoken_text", ""),
                "requested_media_type": requested_kind,
                "search_queries": queries,
                "excluded_source_urls": sorted(used_source_urls),
                "rejected_source_urls": sorted(
                    excluded_for_beat - set(used_source_urls)
                ),
                "download_candidates_tried": download_budget["used"],
                "download_candidates_limit": download_budget["limit"],
                "errors": errors,
                "rate_limited": rate_limited,
                "policy": (
                    "approved stock is never silently replaced by generated graphics"
                ),
            }

            failure_path.write_text(
                json.dumps(failure, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            warnings.append(
                f"{scene['id']}: suitable Pixabay {requested_kind} not found; "
                "manual review required"
            )

            raise RuntimeError(
                f"{scene['id']}/b{index:02d}: "
                f"\u0410\u0432\u0442\u043e\u0437\u0430\u043c\u0435\u043d\u0430 "
                f"\u0433\u0440\u0430\u0444\u0438\u043a\u043e\u0439 "
                f"\u0437\u0430\u043f\u0440\u0435\u0449\u0435\u043d\u0430. "
                f"Manual review report: {failure_path}"
            )

        original_path = Path(asset["path"])
        source_sha256 = hashlib.sha256(original_path.read_bytes()).hexdigest()
        source_url = asset.get("source_url")
        if source_url:
            used_source_urls.add(source_url)
        asset.update({
            "sha256": source_sha256,
            "source_sha256": source_sha256,
            "source_asset_path": str(original_path.resolve()),
            "asset_check": check_note,
            "requested_media_type": requested_kind,
            "selection_fallback": selection_fallback,
            "fallback_reason": fallback_reason,
            "beat_download_candidates_tried": download_budget["used"],
            "beat_download_candidates_limit": download_budget["limit"],
        })

        if selected_kind == "photo":
            beat_data = dict(beat)
            beat_data["visual_type"] = "photo"
            beat_data["image_path"] = str(original_path.resolve())
            video_file = visuals_dir / f"{scene['id']}_{index:02d}_photo.mp4"
            render_html_video(beat_data, duration=duration, output_path=video_file)
            rendered_sha256 = hashlib.sha256(video_file.read_bytes()).hexdigest()
            asset.update({
                "path": str(video_file.resolve()),
                "media_type": "video",
                "sha256": rendered_sha256,
                "rendered_sha256": rendered_sha256,
                "overlay_path": None,
                "template_id": "html_photo_kenburns",
                "visual_family": "photo_stock",
            })
            return asset

        if beat.get("on_screen_text"):
            overlay = render_stock_overlay(
                beat["on_screen_text"], visuals_dir / f"{scene['id']}_{index:02d}_overlay.png")
            asset["overlay_path"] = str(overlay.resolve())
        return asset

    if strategy != "rendered":
        raise ValueError(f"{scene['id']}/b{index:02d}: unsupported asset_strategy={strategy!r}")

    beat_data = dict(beat)
    beat_data["scene_meaning"] = scene.get("meaning", "")
    video_file = visuals_dir / f"{scene['id']}_{index:02d}_rendered.mp4"
    render_html_video(beat_data, duration=duration, output_path=video_file)
    return {
        "path": str(video_file.resolve()), "media_type": "video",
        "source": "HTML/CSS Renderer", "source_url": None, "creator": "Project renderer",
        "license_name": "Project original graphics", "rights_status": "project_generated",
        "sha256": hashlib.sha256(video_file.read_bytes()).hexdigest(),
        "template_id": "html_css", "visual_family": "warm",
        "requested_media_type": "rendered",
    }

def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        if wf.getframerate() <= 0:
            raise ValueError(f"Invalid WAV sample rate: {path}")
        return wf.getnframes() / wf.getframerate()


def _punchline_pause(text: str, visual_type: str, base: float) -> float:
    if base <= 0:
        return 0.0
    stripped = _norm(text).rstrip('"»)\u201d')
    if stripped.endswith(("!", "?", "…")) or str(visual_type).lower() == "joke":
        return base
    return 0.0


def _padded_wav(src: Path, extra_sec: float, pad_dir: Path) -> Path:
    """Идемпотентная копия WAV с хвостовой тишиной (панчлайн-пауза)."""
    pad_dir.mkdir(parents=True, exist_ok=True)
    dst = pad_dir / f"{src.stem}_pad{int(extra_sec * 1000)}.wav"
    with wave.open(str(src), "rb") as wf:
        params = wf.getparams()
        frames = wf.readframes(wf.getnframes())
    silence = b"\x00\x00" * int(params.framerate * extra_sec) * params.nchannels
    with wave.open(str(dst), "wb") as wf:
        wf.setparams(params)
        wf.writeframes(frames + silence)
    return dst


def _produce(settings, scenes, plans, out_dir, speed, pause_seconds, punchline_pause):
    if str(getattr(settings, "tts_engine", "kokoro")).lower() != "kokoro" or \
       str(getattr(settings, "tts_speaker", "dima")).lower() != "dima":
        raise RuntimeError("Autonomous production is pinned to Kokoro Dima; no silent engine fallback")
    if not 0.5 <= speed <= 2.0:
        raise ValueError("TTS speed must be 0.5..2.0")
    audio_dir = out_dir / "audio"
    visuals_dir = out_dir / "visuals"
    audio_dir.mkdir(parents=True, exist_ok=True)
    visuals_dir.mkdir(parents=True, exist_ok=True)
    scene_by = {s["id"]: s for s in scenes}

    prepared, flat = [], []
    for plan in plans:
        scene = scene_by[plan["scene_id"]]
        for bi, beat in enumerate(plan["beats"], 1):
            block_id = f"{scene['id']}_b{bi:02d}"
            raw = beat["spoken_text"].strip()
            normalized = normalize_for_speech(raw)
            if not normalized:
                raise ValueError(f"{block_id}: empty text after TTS normalization")
            prepared.append((block_id, {"heading": scene.get("meaning", "")}, raw, normalized))
            flat.append((scene, plan, beat, block_id, bi))

    audio_manifest = synthesize_kokoro(settings, prepared, audio_dir, speed)
    if audio_manifest.get("status") != "complete":
        raise RuntimeError("Kokoro did not synthesize all beat WAVs")
    audio_by = {x["id"]: x for x in audio_manifest["blocks"]}

    beats, warnings = [], []
    used_source_urls: set[str] = set()
    cursor = 0.0
    for scene, plan, beat, block_id, bi in flat:
        row = audio_by.get(block_id)
        if not row:
            raise RuntimeError(f"Kokoro manifest missing {block_id}")
        wav = audio_dir / row["file"]
        extra = _punchline_pause(beat["spoken_text"], beat.get("visual_type", ""), punchline_pause)
        if extra > 0:
            wav = _padded_wav(wav, extra, audio_dir / "padded")
        duration = _wav_duration(wav)
        # Визуал рендерится ПОСЛЕ измерения речи и ровно под её длительность.
        asset = _asset_for_beat(settings, scene, beat, visuals_dir, warnings, bi, duration,
                                 used_source_urls=used_source_urls)
        beats.append({
            "scene_id": scene["id"], "beat_id": f"b{bi:02d}",
            "spoken_text": beat["spoken_text"], "audio_path": str(wav.resolve()),
            "audio_duration_sec": duration, "visual_path": asset["path"],
            "media_type": asset["media_type"], "overlay_path": asset.get("overlay_path"),
            "source": asset.get("source"), "source_url": asset.get("source_url"),
            "creator": asset.get("creator"), "license_name": asset.get("license_name"),
            "sha256": asset.get("sha256"),
            "asset_check": asset.get("asset_check", "local_renderer_video_created"),
            "asset_query": asset.get("query"),
            "candidate_count": asset.get("candidate_count"),
            "selection_score": asset.get("selection_score"),
            "selected_tags": asset.get("selected_tags"),
            "selected_resolution": asset.get("selected_resolution"),
            "pixabay_type": asset.get("pixabay_type"),
            "rights_status": asset.get("rights_status"),
            "requested_media_type": asset.get("requested_media_type"),
            "selection_fallback": asset.get("selection_fallback"),
            "fallback_reason": asset.get("fallback_reason"),
            "source_asset_path": asset.get("source_asset_path"),
            "source_sha256": asset.get("source_sha256"),
            "rendered_sha256": asset.get("rendered_sha256"),
            "search_queries_checked": asset.get("search_queries_checked"),
            "download_candidates_tried": asset.get("download_candidates_tried"),
            "duration_ratio": None, "motion": beat.get("motion", "static"),
            "transition_in": beat.get("transition_in", "cut"),
            "audio_sfx": [x.model_dump(mode="json") if hasattr(x, "model_dump") else x
                          for x in beat.get("audio_sfx", [])],
            "music_cue": plan.get("music_cue", ""),
            "on_screen_text": beat.get("on_screen_text", []),
            "visual_type": beat.get("visual_type"),
            "visual_description": beat.get("visual_description"),
            "template_id": asset.get("template_id"), "visual_family": asset.get("visual_family"),
            "steps": asset.get("steps"),
            "start_sec_estimate": round(cursor, 4),
        })
        cursor += duration + pause_seconds
    return beats, {
        "status": "complete",
        "audio_manifest": str((audio_dir / "manifest.json").resolve()),
        "beat_count": len(beats),
        "speech_seconds": round(sum(x["audio_duration_sec"] for x in beats), 4),
        "warnings": warnings,
    }


def _post_qc(settings, beats, source_audit, audio_report, assembly, video_path,
             out_path, visual_audit=None):
    errors, warnings = [], []
    if visual_audit:
        errors.extend(visual_audit.get("errors", []))
        warnings.extend(visual_audit.get("warnings", []))
    if not video_path.is_file():
        errors.append("Final MP4 missing")
    if len(beats) != assembly.get("beat_count"):
        errors.append("Beat count mismatch")
    for b in beats:
        if not Path(b["audio_path"]).is_file():
            errors.append(f"{b['scene_id']}/{b['beat_id']}: WAV missing")
        if not Path(b["visual_path"]).is_file():
            errors.append(f"{b['scene_id']}/{b['beat_id']}: visual missing")
    probe = assembly.get("ffprobe", {})
    streams = probe.get("streams", [])
    if not any(x.get("codec_type") == "video" for x in streams):
        errors.append("MP4 video stream missing")
    if not any(x.get("codec_type") == "audio" for x in streams):
        errors.append("MP4 audio stream missing")
    sync_tolerance = max(0.12, len(beats) / max(1, int(getattr(settings, "fps", 25))) + 0.08)
    if abs(float(audio_report.get("duration_seconds", 0)) - float(assembly.get("duration_sec", 0))) > sync_tolerance:
        errors.append("Master audio and final video durations do not match")
    audio_slots = audio_report.get("timeline", [])
    video_slots = assembly.get("timeline", [])
    if len(audio_slots) != len(beats) or len(video_slots) != len(beats):
        errors.append("Audio/video timeline beat count mismatch")
    else:
        for a, v in zip(audio_slots, video_slots):
            if a["scene_id"] != v["scene_id"] or a["beat_id"] != v["beat_id"]:
                errors.append(f"Timeline identity mismatch: {a['scene_id']}/{a['beat_id']}")
            if abs(float(a["start_sec"]) - float(v["start_sec"])) > 0.02 or \
               abs(float(a["end_sec"]) - float(v["end_sec"])) > 0.03:
                errors.append(f"Timeline offset mismatch: {a['scene_id']}/{a['beat_id']}")
    if not source_audit.get("facts_verified", False):
        warnings.append("Facts remain unverified; human review required")
    report = {
        "status": "failed" if errors else ("passed_with_warnings" if warnings else "passed"),
        "errors": errors, "warnings": warnings, "video": str(video_path.resolve()),
        "beat_count": len(beats), "duration_sec": assembly.get("duration_sec"),
        "technical_audio_video_sync": "passed" if not errors else "failed",
        "semantic_frame_to_speech_review": "human_review_required",
        "visual_audit": visual_audit or {"status": "not_run"},
        "facts_verified": False, "claims_policy": "No automated claim is marked verified.",
    }
    _atomic_json(out_path, report)
    return report


def run_pipeline(*, script_path: Path | None, storyboard_path: Path | None, out_dir: Path,
                 scene_limit: int | None = 10, batch_size: int = 2, speed: float = 1.0,
                 revision_rounds: int = 1, settings: Any | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    if (script_path is None) == (storyboard_path is None):
        raise ValueError("Specify exactly one of script_path or storyboard_path")
    if not 1 <= batch_size <= 4:
        raise ValueError("batch_size must be 1..4")
    if not 0 <= revision_rounds <= 3:
        raise ValueError("revision_rounds must be 0..3")
    pause_seconds = float(os.getenv("KADR_PAUSE_SECONDS", "0.22"))
    punchline_pause = float(os.getenv("KADR_PUNCHLINE_PAUSE", "0.18"))
    strict_critic = os.getenv("KADR_STRICT_CRITIC", "0").strip() == "1"

    out_dir.mkdir(parents=True, exist_ok=True)
    draft_path = out_dir / "storyboard_draft.json"
    if script_path:
        script_fp = hashlib.sha256(
            json.dumps(read_json(script_path), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        reuse = draft_path.exists() and read_json(draft_path).get("source_script_sha256") == script_fp
        if not reuse:
            create_storyboard(settings, script_path, draft_path)
    else:
        shutil.copy2(storyboard_path, draft_path)
    source = read_json(draft_path)
    scenes = source.get("scenes") or []
    if scene_limit:
        if scene_limit < 1:
            raise ValueError("scene_limit must be >=1")
        scenes = scenes[:scene_limit]
    if not scenes:
        raise ValueError("No scenes available after scene limit")
    ids = [x["id"] for x in scenes]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate scene IDs in storyboard")
    source["scenes"] = scenes
    _atomic_json(out_dir / "pilot_storyboard.json", source)

    plan_path = out_dir / "director_plan.json"
    expected_fp = _director_plan_fingerprint(scenes)
    reuse_plan = False
    if plan_path.exists():
        cached = read_json(plan_path)
        cached_plans = cached.get("plans", [])
        cached_ids = {x.get("scene_id") for x in cached_plans}
        if cached.get("source_sha256") == expected_fp and cached_ids == set(ids):
            try:
                for p in cached_plans:
                    _check_exact_coverage(p, next(s for s in scenes if s["id"] == p["scene_id"]))
                plans = cached_plans
                reuse_plan = True
            except (KeyError, ValueError):
                log.warning("Cached director plan failed current validation; regenerating it")
        else:
            log.info("Cached director plan uses an older policy or different narration; regenerating it")
    if not reuse_plan:
        plans = _make_director_plan(settings, scenes, batch_size, plan_path)

    review_path = out_dir / "director_review.json"
    plan_fp = hashlib.sha256(json.dumps(
        {"scenes": scenes, "plans": plans, "director_policy_version": DIRECTOR_POLICY_VERSION}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    cached_review = read_json(review_path) if review_path.exists() else None
    if cached_review and cached_review.get("plan_sha256") == plan_fp:
        review_history = cached_review.get("history", [])
        unresolved = cached_review.get("unresolved", [])
        log.info("Director review reused from cache (%d unresolved)", len(unresolved))
    else:
        review_history = []
        for round_no in range(revision_rounds + 1):
            critique = _critique(settings, scenes, plans)
            review_history.append({"round": round_no, "critique": critique})
            _atomic_json(out_dir / f"director_critique_round_{round_no + 1}.json", critique)
            bad = [x for x in critique["reviews"] if _is_bad(x)]
            if not bad or round_no == revision_rounds:
                break
            plans = _repair(settings, scenes, plans, critique)
            _atomic_json(plan_path, {"plans": plans, "repaired": True, "round": round_no + 1,
                                     "source_sha256": _director_plan_fingerprint(scenes)})
        unresolved = [x for x in critique["reviews"] if _is_bad(x)]
        plan_fp = hashlib.sha256(json.dumps(
            {"scenes": scenes, "plans": plans, "director_policy_version": DIRECTOR_POLICY_VERSION}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        _atomic_json(review_path, {
            "history": review_history, "unresolved": unresolved,
            "plan_sha256": plan_fp,
            "status": "needs_human" if unresolved else "accepted",
            "human_review_required": True,
        })
    if unresolved:
        if strict_critic:
            raise RuntimeError(f"AI critic reports {len(unresolved)} unresolved scene(s); see director_review.json")
        log.warning("AI critic: %d unresolved scene(s) -> human queue (production continues)", len(unresolved))
    _atomic_json(plan_path, {"plans": plans,
                             "status": "accepted_with_warnings" if unresolved else "accepted_by_ai_critic",
                             "scene_count": len(plans),
                             "source_sha256": _director_plan_fingerprint(scenes)})

    shot_plan_path = out_dir / "shot_plan.json"
    shot_result = plan_shots(settings, scenes, plans, shot_plan_path, batch_size=batch_size)
    shot_plans = shot_result["plans"]
    source_audit = audit_claim_sources(settings, plans, out_dir / "source_audit.json")
    beats, production = _produce(settings, scenes, shot_plans, out_dir / "production",
                                 speed, pause_seconds, punchline_pause)
    if unresolved:
        production["warnings"].append(
            f"critic_unresolved_scenes={[r['scene_id'] for r in unresolved]}")

    asset_audit = audit_visual_assets(beats, out_dir / "visual_asset_audit.json", ffmpeg=settings.ffmpeg)
    production["visual_asset_audit_status"] = asset_audit["status"]
    production["visual_asset_audit_errors"] = asset_audit["errors"]
    production["visual_asset_audit_warnings"] = asset_audit["warnings"]
    audio_report = build_master_audio(beats, out_dir / "production" / "audio_master.wav",
                                      pause_seconds=pause_seconds)
    assembly = assemble_director_video(settings, beats, Path(audio_report["path"]),
                                       out_dir / "final.mp4", out_dir / "production" / "assembly",
                                       pause_seconds=pause_seconds)
    final_visual_audit = audit_final_video_frames(
        out_dir / "final.mp4", assembly.get("timeline", []), out_dir / "visual_audit.json",
        out_dir / "visual_contact_sheet.jpg", ffmpeg=settings.ffmpeg)
    visual_errors = asset_audit.get("errors", []) + final_visual_audit.get("errors", [])
    visual_warnings = asset_audit.get("warnings", []) + final_visual_audit.get("warnings", [])
    visual_audit = {
        "status": "failed" if visual_errors else ("passed_with_warnings" if visual_warnings else "passed"),
        "errors": visual_errors, "warnings": visual_warnings,
        "asset_audit_path": str((out_dir / "visual_asset_audit.json").resolve()),
        "final_video_audit_path": str((out_dir / "visual_audit.json").resolve()),
        "final_video": final_visual_audit,
    }
    production["visual_audit_status"] = visual_audit["status"]
    production["visual_audit_errors"] = visual_audit["errors"]
    production["visual_audit_warnings"] = visual_audit["warnings"]
    qc = _post_qc(settings, beats, source_audit, audio_report, assembly, out_dir / "final.mp4",
                  out_dir / "postproduction_qc.json", visual_audit=visual_audit)

    timeline_beats = []
    for b, slot in zip(beats, audio_report.get("timeline", [])):
        timeline_beats.append({
            **{k: b.get(k) for k in ("scene_id", "beat_id", "spoken_text", "audio_path",
                                      "audio_duration_sec", "visual_path", "media_type", "source",
                                      "source_url", "creator", "license_name", "sha256", "asset_query", "candidate_count",
                                      "selection_score", "selected_tags", "selected_resolution",
                                      "requested_media_type", "selection_fallback", "source_asset_path",
                                      "source_sha256", "motion",
                                      "transition_in", "visual_type", "template_id", "visual_family")},
            "start_sec": slot["start_sec"], "voice_end_sec": slot["voice_end_sec"], "end_sec": slot["end_sec"],
        })
    _atomic_json(out_dir / "production_timeline.json", {"audio": audio_report, "beats": timeline_beats})
    media_manifest = {
        "status": "complete", "asset_count": len(beats),
        "assets": [{"scene_id": b["scene_id"], "beat_id": b["beat_id"], "path": b["visual_path"],
                    "media_type": b["media_type"], "source": b["source"], "source_url": b.get("source_url"),
                    "creator": b.get("creator"), "license_name": b.get("license_name"),
                    "rights_status": "review_required" if b.get("source_url") else "project_generated",
                    "sha256": b.get("sha256"), "asset_check": b.get("asset_check"),
                    "visual_type": b.get("visual_type"), "template_id": b.get("template_id"),
                    "visual_family": b.get("visual_family")} for b in beats],
        "audio_manifest": production.get("audio_manifest"),
        "audio_master": audio_report["path"],
        "music_track": audio_report.get("music_track"),
        "note": "Pixabay attribution and rights must be reviewed before publication.",
    }
    _atomic_json(out_dir / "media_manifest.json", media_manifest)
    report = {
        "status": "complete" if not qc["errors"] else "failed",
        "scene_count": len(scenes), "beat_count": len(beats),
        "video": str((out_dir / "final.mp4").resolve()),
        "contact_sheet": str(Path(final_visual_audit["contact_sheet"]).resolve()),
        "director_plan": str(plan_path.resolve()),
        "source_audit": str((out_dir / "source_audit.json").resolve()),
        "visual_audit": str((out_dir / "visual_audit.json").resolve()),
        "visual_asset_audit": str((out_dir / "visual_asset_audit.json").resolve()),
        "media_manifest": str((out_dir / "media_manifest.json").resolve()),
        "production": production, "audio": audio_report, "assembly": assembly,
        "postproduction_qc": qc, "critic_unresolved": len(unresolved),
        "facts_verified": False, "human_review_required": True,
    }
    _atomic_json(out_dir / "director_report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="video-farm-director",
                                     description="Autonomous AI Video Factory production director")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--script", type=Path, help="script.json: farm creates semantic storyboard")
    source.add_argument("--storyboard", type=Path, help="existing semantic storyboard draft")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scene-limit", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--revision-rounds", type=int, default=1)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", datefmt="%H:%M:%S")
    try:
        report = run_pipeline(script_path=args.script, storyboard_path=args.storyboard,
                              out_dir=args.out, scene_limit=args.scene_limit,
                              batch_size=args.batch_size, speed=args.speed,
                              revision_rounds=args.revision_rounds)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "complete" else 2
    except Exception:
        log.exception("Autonomous production failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())