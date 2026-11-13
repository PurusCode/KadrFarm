from __future__ import annotations
import json, logging, shutil, subprocess, wave
from pathlib import Path
from typing import Any
from .config import Settings
from .common import ensure_dir, read_json, write_json, word_count

log = logging.getLogger(__name__)


def _probe(settings: Settings, path: Path) -> dict[str, Any]:
    cmd = [settings.ffprobe, "-v", "error", "-show_entries", "format=duration,bit_rate,size:stream=codec_name,codec_type,width,height,r_frame_rate,profile,bit_rate,sample_rate,channels", "-of", "json", str(path)]
    r = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(r.stdout)



def _script_block_id(block: dict[str, Any], index: int) -> str:
    return str(block.get("id") or f"block_{index}")


def _validate_audio_manifest(script_blocks: list[dict[str, Any]], audio_manifest: dict[str, Any], audio_dir: Path) -> dict[str, dict[str, Any]]:
    """Reject incomplete/stale audio output instead of quietly creating a broken, out-of-sync video."""
    expected_ids = [_script_block_id(block, i) for i, block in enumerate(script_blocks, 1)]
    if len(expected_ids) != len(set(expected_ids)):
        raise RuntimeError("В сценарии повторяются ID блоков; сначала исправьте script.json.")

    rows = audio_manifest.get("blocks", [])
    if not isinstance(rows, list):
        raise RuntimeError("Некорректный audio/manifest.json: поле blocks должно быть списком.")
    actual_ids = [str(row.get("id") or "") for row in rows if isinstance(row, dict)]
    if len(actual_ids) != len(rows) or any(not block_id for block_id in actual_ids):
        raise RuntimeError("Некорректный audio/manifest.json: у блока отсутствует ID.")
    if len(actual_ids) != len(set(actual_ids)):
        raise RuntimeError("Некорректный audio/manifest.json: повторяются ID блоков.")
    if set(actual_ids) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(actual_ids))
        unexpected = sorted(set(actual_ids) - set(expected_ids))
        details = []
        if missing: details.append("нет блоков: " + ", ".join(missing))
        if unexpected: details.append("лишние блоки: " + ", ".join(unexpected))
        raise RuntimeError("Манифест озвучки не соответствует сценарию (" + "; ".join(details) + "). Пересоздайте озвучку.")

    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        filename = str(row.get("file") or "")
        if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
            raise RuntimeError(f"Небезопасное или пустое имя WAV в audio/manifest.json: {filename!r}.")
        wav_path = audio_dir / filename
        if not wav_path.is_file():
            raise RuntimeError(f"В манифесте указан отсутствующий WAV: {wav_path}.")
        try:
            with wave.open(str(wav_path), "rb") as wav:
                measured_duration = wav.getnframes() / max(wav.getframerate(), 1)
                if wav.getnframes() <= 0 or wav.getframerate() <= 0 or wav.getnchannels() <= 0:
                    raise RuntimeError(f"Пустой или некорректный WAV: {wav_path}.")
        except (wave.Error, OSError) as exc:
            raise RuntimeError(f"Повреждённый WAV: {wav_path}.") from exc
        try:
            declared_duration = float(row.get("duration_seconds", 0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"В манифесте некорректная длительность блока {row['id']}.") from exc
        if declared_duration <= 0 or abs(declared_duration - measured_duration) > max(0.1, measured_duration * 0.01):
            raise RuntimeError(
                f"Длительность WAV {row['id']} не совпадает с манифестом: "
                f"файл {measured_duration:.3f} с, манифест {declared_duration:.3f} с."
            )
        result[str(row["id"])] = row
    return result

def _choose_broll(block: dict[str, Any], assets: dict[str, Any], used: set[str]) -> list[dict[str, Any]]:
    clips = [x for x in assets.get("broll", []) if Path(x.get("file", "")).exists()]
    keywords = [str(k).lower() for k in block.get("broll_keywords", [])]
    matching = [x for x in clips if any(k in x.get("query", "").lower() or x.get("query", "").lower() in k for k in keywords)]
    ordered = matching + [x for x in clips if x not in matching]
    chosen = []
    for x in ordered:
        p = x.get("file", "")
        if p in used: continue
        chosen.append(x); used.add(p)
        if len(chosen) >= 2: break
    return chosen


def assemble_video(settings: Settings, script_path: Path, audio_dir: Path, slides_dir: Path,
                   assets_manifest_path: Path, out_path: Path, *, draft: bool = False) -> dict[str, Any]:
    script = read_json(script_path)
    audio_manifest_path = audio_dir / "manifest.json"
    if not audio_manifest_path.exists(): raise RuntimeError(f"Не найден манифест озвучки: {audio_manifest_path}")
    audio_manifest = read_json(audio_manifest_path)
    slide_manifest_path = slides_dir / "slides_manifest.json"
    if not slide_manifest_path.exists(): raise RuntimeError(f"Не найден манифест слайдов: {slide_manifest_path}")
    slide_manifest = read_json(slide_manifest_path)
    assets = read_json(assets_manifest_path) if assets_manifest_path.exists() else {"broll": [], "images": []}
    script_blocks = script.get("blocks", [])
    audio_by_id = _validate_audio_manifest(script_blocks, audio_manifest, audio_dir)
    slides_by_id: dict[str, list[dict[str, Any]]] = {}
    slide_rows = slide_manifest.get("slides", [])
    if not isinstance(slide_rows, list):
        raise RuntimeError("Некорректный slides_manifest.json: поле slides должно быть списком.")
    for sl in slide_rows:
        if not isinstance(sl, dict):
            raise RuntimeError("Некорректный slides_manifest.json: запись слайда должна быть объектом.")
        block_id = str(sl.get("block_id") or "")
        filename = str(sl.get("file") or "")
        if not block_id or not filename:
            raise RuntimeError("Некорректный slides_manifest.json: у слайда отсутствует block_id или file.")
        if not Path(filename).is_file():
            raise RuntimeError(f"В манифесте указан отсутствующий слайд: {filename}.")
        slides_by_id.setdefault(block_id, []).append(sl)
    used_clips: set[str] = set()
    timeline: list[dict[str, Any]] = []
    warnings: list[str] = []
    fps = settings.fps
    target_w, target_h = (1280, 720) if draft else (1920, 1080)
    # Allocate each audio block's exact duration between full-frame slides and standalone B-roll inserts.
    for i, block in enumerate(script_blocks, 1):
        bid = _script_block_id(block, i)
        audio_entry = audio_by_id[bid]
        wav = audio_dir / audio_entry["file"]
        slide_rows = slides_by_id.get(bid, [])
        if not slide_rows:
            raise RuntimeError(f"Нет слайдов для блока {bid}; сборка остановлена, чтобы не нарушать синхронизацию.")
        duration = float(audio_entry.get("duration_seconds", 0))
        clips = _choose_broll(block, assets, used_clips)
        # Don't let short audio blocks be swallowed by stock inserts.
        nclips = min(len(clips), max(0, min(2, len(slide_rows) - 1, int(duration // 35))))
        clips = clips[:nclips]
        clip_dur = min(2.8, max(1.8, duration * 0.035)) if clips else 0.0
        slide_dur = max(0.2, (duration - clip_dur * len(clips)) / len(slide_rows))
        entries: list[dict[str, Any]] = []
        for i, sl in enumerate(slide_rows):
            entries.append({"kind": "image", "file": sl["file"], "asset_file": sl.get("asset_file"),
                            "duration": slide_dur, "block_id": bid, "note": "full-frame slide; no overlay"})
            if i < len(clips):
                entries.append({"kind": "video", "file": clips[i]["file"], "asset_file": clips[i]["file"],
                                "duration": clip_dur, "block_id": bid, "note": "standalone B-roll insert"})
        # Quantize to frames, adjust last segment so visual time matches actual audio block length.
        if entries:
            for e in entries: e["frames"] = max(1, round(float(e["duration"]) * fps))
            target_frames = max(len(entries), round(duration * fps))
            entries[-1]["frames"] = max(1, target_frames - sum(e["frames"] for e in entries[:-1]))
            for e in entries: e["duration"] = e["frames"] / fps
        timeline.extend(entries)
    if not timeline: raise RuntimeError("Нечего собирать: проверьте WAV и PNG-манифесты.")
    if not any(e["kind"] == "video" for e in timeline):
        warnings.append("В финале нет стоковых B-roll-вставок: добавьте PEXELS_API_KEY и/или PIXABAY_API_KEY и повторите этап assets.")
    ensure_dir(out_path.parent)
    # FFmpeg filter_complex concatenates distinct complete frames/scenes; B-roll never overlays a slide.
    cmd: list[str] = [settings.ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for e in timeline:
        if e["kind"] == "image":
            cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{e['duration']:.6f}", "-i", e["file"]]
        else:
            cmd += ["-stream_loop", "-1", "-t", f"{e['duration']:.6f}", "-i", e["file"]]
    audio_inputs: list[Path] = []
    for i, block in enumerate(script_blocks, 1):
        entry = audio_by_id[_script_block_id(block, i)]
        audio_inputs.append(audio_dir / entry["file"])
    if not audio_inputs:
        raise RuntimeError("Не найдено ни одного WAV-файла для сборки аудио.")
    for wav in audio_inputs: cmd += ["-i", str(wav)]
    filters: list[str] = []
    video_labels = []
    for i, e in enumerate(timeline):
        dur = e["duration"]
        filters.append(f"[{i}:v]trim=duration={dur:.6f},setpts=PTS-STARTPTS,fps={fps},scale={target_w}:{target_h}:force_original_aspect_ratio=increase,crop={target_w}:{target_h},setsar=1,format=yuv420p[v{i}]")
        video_labels.append(f"[v{i}]")
    filters.append("".join(video_labels) + f"concat=n={len(video_labels)}:v=1:a=0[outv]")
    audio_labels = []
    first_audio_idx = len(timeline)
    for j, _ in enumerate(audio_inputs):
        label = f"a{j}"
        filters.append(f"[{first_audio_idx + j}:a]aresample=44100,aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,asetpts=PTS-STARTPTS[{label}]")
        audio_labels.append(f"[{label}]")
    filters.append("".join(audio_labels) + f"concat=n={len(audio_labels)}:v=0:a=1[outa]")
    bitrate = "1200k" if draft else settings.bitrate
    preset = "ultrafast" if draft else "slow"
    cmd += ["-filter_complex", ";".join(filters), "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", preset, "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-r", str(fps), "-g", str(settings.gop), "-keyint_min", str(settings.gop), "-sc_threshold", "0",
            "-b:v", bitrate, "-minrate", bitrate, "-maxrate", bitrate, "-bufsize", "9000k" if not draft else "2400k",
            "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart", "-shortest", str(out_path)]
    if not draft:
        # libx264 needs HRD signaling for genuine CBR; -b/-minrate/-maxrate alone may average much lower on static slides.
        # Append x264 parameters before the output path; avoid relying on source complexity for target bitrate.
        out_index = cmd.index(str(out_path))
        cmd[out_index:out_index] = ["-x264-params", "nal-hrd=cbr:force-cfr=1"]
    log.info("Запускаю FFmpeg: %s видеосцен, %s аудиоблоков", len(timeline), len(audio_inputs))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("FFmpeg завершился с ошибкой:\n" + result.stderr[-6000:])
    probe = _probe(settings, out_path)
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    dur = float(probe.get("format", {}).get("duration", 0))
    vbit = int(video.get("bit_rate") or 0)
    asset_rows = []
    for kind, field in (("broll", "broll"), ("image", "images")):
        for asset in assets.get(field, []):
            filename = str(asset.get("file") or "")
            if filename:
                asset_rows.append((Path(filename).resolve(), {**asset, "kind": kind}))
    assets_by_path = {str(path): item for path, item in asset_rows}
    used_media_by_path: dict[str, dict[str, Any]] = {}
    for entry in timeline:
        if entry.get("kind") == "video":
            key_path = str(Path(entry.get("file", "")).resolve())
        else:
            asset_file = entry.get("asset_file")
            if not asset_file:
                continue
            key_path = str(Path(asset_file).resolve())
        asset = assets_by_path.get(key_path)
        if asset:
            used_media_by_path[key_path] = {
                "kind": asset.get("kind"), "file": asset.get("file"), "source": asset.get("source", ""),
                "page_url": asset.get("page_url", ""), "creator": asset.get("creator", ""),
                "creator_url": asset.get("creator_url", ""), "query": asset.get("query", ""),
            }
    used_media = list(used_media_by_path.values())
    report = {"file": str(out_path), "draft": draft, "duration_seconds": round(dur, 2),
              "duration_minutes": round(dur / 60, 2), "size_bytes": out_path.stat().st_size,
              "video": {"codec": video.get("codec_name"), "profile": video.get("profile"),
                        "width": video.get("width"), "height": video.get("height"), "fps": video.get("r_frame_rate"),
                        "bitrate_reported": vbit, "gop_target": settings.gop},
              "audio": {"codec": audio.get("codec_name"), "sample_rate": audio.get("sample_rate"),
                        "channels": audio.get("channels"), "bitrate": audio.get("bit_rate")},
              "timeline_scene_count": len(timeline), "broll_scene_count": sum(e["kind"] == "video" for e in timeline),
              "used_media": used_media, "warnings": warnings, "ffprobe": probe}
    if not draft:
        if video.get("width") != 1920 or video.get("height") != 1080: warnings.append("Разрешение не 1920x1080")
        if dur < 420 or dur > 540: warnings.append(f"Длительность {dur/60:.2f} мин вне цели 7–9 минут")
        if audio.get("codec_name") != "aac": warnings.append("Аудиокодек не AAC")
        if audio.get("sample_rate") != "44100": warnings.append("Частота аудио не 44100 Hz")
        if audio.get("channels") != 2: warnings.append("Аудио не стерео")
        if vbit and vbit < 3600000: warnings.append(f"Битрейт видео {vbit} ниже 3.6 Mbps")
    report_path = out_path.with_suffix(".qc.json")
    write_json(report_path, report)
    return report


def create_description(script_path: Path, out_path: Path, assets_manifest_path: Path | None = None,
                       *, used_media: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    script = read_json(script_path)
    summary = str(script.get("description_summary", "Авторский разбор. Проверяйте факты по указанным источникам."))
    hashtags = script.get("hashtags", ["#обучение", "#технологии"])
    if used_media is None:
        attribution = (
            "Атрибуция внешних материалов не определена: описание создано отдельно от итогового видео. "
            "Сверьте изображения и B-roll с фактически использованными файлами перед публикацией."
        )
        media_status = "UNKNOWN_REVIEW_REQUIRED"
        media_rows: list[dict[str, Any]] = []
        media_output: list[dict[str, Any]] | None = None
    elif not used_media:
        attribution = "Внешние стоковые фото и видео в этом ролике не использованы; видеоряд состоит из авторских слайдов."
        media_status = "NO_EXTERNAL_MEDIA_USED"
        media_rows = []
        media_output = []
    else:
        lines = ["Использованные внешние материалы (проверьте лицензию каждого файла перед публикацией):"]
        for item in used_media:
            source = str(item.get("source") or "Источник не указан")
            creator = str(item.get("creator") or "Автор не указан")
            page_url = str(item.get("page_url") or "Ссылка на исходную страницу не указана")
            lines.append(f"- {source} — {creator}; {page_url}")
        attribution = "\n".join(lines)
        media_status = "REVIEW_REQUIRED"
        media_rows = [dict(item) for item in used_media]
        media_output = media_rows

    text = f"{script.get('title', script.get('topic', 'Авторский разбор'))}\n\n{summary}\n\nВ этом видео:\n"
    for block in script.get("blocks", []):
        if block.get("kind") not in ("hook", "cta"):
            text += f"• {block.get('heading', 'Раздел')}\n"
    text += "\n" + " ".join(hashtags) + "\n\n" + attribution + "\n\n" \
            "Материал носит информационный характер. Проверяйте факты и актуальность ссылок на дату просмотра."
    payload = {
        "title": script.get("title", ""), "description": text, "hashtags": hashtags,
        "media_attribution": attribution, "media_attribution_status": media_status,
        "used_media": media_output,
        "broll_sources": sorted({str(x.get("source")) for x in media_rows if x.get("kind") == "broll" and x.get("source")}),
        "fact_checks": script.get("fact_checks", []),
        "publish_gate": "HUMAN_REVIEW_REQUIRED — проверить утверждения, ссылки, фактические медиа и лицензии перед загрузкой.",
    }
    write_json(out_path.with_suffix(".json"), payload)
    out_path.write_text(text, encoding="utf-8")
    return payload

