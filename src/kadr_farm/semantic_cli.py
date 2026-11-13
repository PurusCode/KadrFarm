"""CLI for the semantic-first production pipeline (legacy CLI remains intact)."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from .common import ROOT, ensure_dir
from .config import load_settings
from .storyboard import create_storyboard
from .visual_planner import plan_visuals
from .scene_validator import validate_storyboard
from .media_producer import produce_media
from .assemble_v2 import assemble_v2
from .qc_scene_sync import run_scene_qc
from .publish_package import create_publish_package


def _path(value: str) -> Path:
    p = Path(value).expanduser()
    return (p if p.is_absolute() else ROOT / p).resolve()


def _logger(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _pipeline(args: argparse.Namespace) -> int:
    settings = load_settings()
    script = _path(args.script)
    out = _path(args.out)
    ensure_dir(out)
    draft = out / "storyboard_draft.json"
    planned = out / "storyboard_planned.json"
    validated = out / "storyboard_validated.json"
    produced = out / "storyboard_produced.json"
    video = out / (args.name + ".mp4")
    qc_path = out / (args.name + ".qc.json")

    logging.info("[1/7] Семантическая разметка текста")
    create_storyboard(settings, script, draft)
    logging.info("[2/7] Визуальное планирование")
    plan_visuals(settings, draft, planned, batch_size=getattr(args, "batch_size", 2))
    logging.info("[3/7] Проверка структуры и источников")
    validate_storyboard(script, planned, validated, probe_urls=args.probe_sources)
    logging.info("[4/7] Сценовый Kokoro TTS и визуальные материалы (Pixabay only)")
    produce_media(settings, validated, out, speed=args.speed)
    logging.info("[5/7] Пакет публикации, атрибуция и ручная проверка прав/фактов")
    create_publish_package(script, produced, out)
    logging.info("[6/7] Монтаж по фактической длительности WAV каждой сцены")
    assemble_v2(settings, produced, video)
    logging.info("[7/7] QC синхронизации")
    report = run_scene_qc(settings.ffprobe, produced, video, qc_path)
    print(f"Видео: {video}")
    print(f"QC: {qc_path}")
    print(f"Статус: {report['status']}")
    print(f"Сцен: {report['scene_count']}; длительность: {report['video_duration_seconds']:.2f} сек.")
    for warning in report["warnings"]:
        print("WARNING:", warning)
    for error in report["errors"]:
        print("ERROR:", error)
    return 0 if not report["errors"] else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="video-farm-semantic",
        description="Новый редакторский конвейер: смысловые сцены, сценовый TTS и монтаж по WAV.")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Полный новый конвейер")
    run.add_argument("--script", required=True, help="Путь к готовому script.json")
    run.add_argument("--out", default="output/semantic_runs/run")
    run.add_argument("--name", default="semantic-video")
    run.add_argument("--speed", type=float, default=0.9)
    run.add_argument("--probe-sources", action="store_true")
    run.add_argument("--batch-size", type=int, default=2,
                     help="Число сцен в одном запросе визуального планировщика (1..4; default: 2)")
    run.set_defaults(func=_pipeline)

    sb = sub.add_parser("storyboard", help="Только смысловая разбивка текста")
    sb.add_argument("--script", required=True)
    sb.add_argument("--out", default="output/semantic_runs/storyboard_draft.json")
    sb.set_defaults(func=lambda a: (create_storyboard(load_settings(), _path(a.script), _path(a.out)) and 0))

    plan = sub.add_parser("plan", help="Добавить визуальную режиссуру")
    plan.add_argument("--storyboard", required=True)
    plan.add_argument("--out", default="output/semantic_runs/storyboard_planned.json")
    plan.add_argument("--batch-size", type=int, default=2,
                      help="Число сцен в одном запросе (1..4; default: 2)")
    plan.set_defaults(func=lambda a: (plan_visuals(
        load_settings(), _path(a.storyboard), _path(a.out), batch_size=a.batch_size) and 0))

    validate = sub.add_parser("validate", help="Проверить сцены и claims")
    validate.add_argument("--script", required=True)
    validate.add_argument("--storyboard", required=True)
    validate.add_argument("--out", default="output/semantic_runs/storyboard_validated.json")
    validate.add_argument("--probe-sources", action="store_true")
    validate.set_defaults(func=lambda a: (validate_storyboard(
        _path(a.script), _path(a.storyboard), _path(a.out), probe_urls=a.probe_sources) and 0))

    produce = sub.add_parser("produce", help="Сценовый TTS и визуалы")
    produce.add_argument("--storyboard", required=True)
    produce.add_argument("--out", default="output/semantic_runs/production")
    produce.add_argument("--speed", type=float, default=0.9)
    produce.set_defaults(func=lambda a: (produce_media(
        load_settings(), _path(a.storyboard), _path(a.out), speed=a.speed) and 0))

    assemble = sub.add_parser("assemble", help="Монтаж уже произведённых сцен")
    assemble.add_argument("--storyboard", required=True)
    assemble.add_argument("--out", required=True)
    assemble.set_defaults(func=lambda a: (assemble_v2(
        load_settings(), _path(a.storyboard), _path(a.out)) and 0))

    publish = sub.add_parser("publish", help="Собрать пакет описания, атрибуции и ручной проверки")
    publish.add_argument("--script", required=True)
    publish.add_argument("--storyboard", required=True)
    publish.add_argument("--out", default="output/semantic_runs/publish")
    publish.set_defaults(func=lambda a: (create_publish_package(
        _path(a.script), _path(a.storyboard), _path(a.out)) and 0))

    qc = sub.add_parser("qc", help="Проверка синхронизации MP4")
    qc.add_argument("--storyboard", required=True)
    qc.add_argument("--video", required=True)
    qc.add_argument("--out", default="output/semantic_runs/qc.json")
    qc.set_defaults(func=lambda a: (run_scene_qc(
        load_settings().ffprobe, _path(a.storyboard), _path(a.video), _path(a.out)) and 0))

    args = parser.parse_args(argv)
    _logger(args.verbose)
    try:
        return int(args.func(args))
    except Exception as exc:
        logging.exception("Semantic pipeline failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
