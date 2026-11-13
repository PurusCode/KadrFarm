from __future__ import annotations
import argparse, importlib.metadata, json, logging, os, shutil, subprocess, sys
from pathlib import Path
from .config import load_settings
from .common import ROOT, ensure_dir, write_json, read_json
from .discovery import (discover_youtube, purge_expired_discovery_output,
                        DEFAULT_MAX_RESULTS, DEFAULT_MAX_PAGES, DEFAULT_SEARCH_CALL_BUDGET)
from .discovery_analysis import analyze_discovery
from .discovery_editor import build_review_page
from .content import generate_topics, generate_script
from .tts import synthesize
from .assets import download_assets
from .slides import render_slides
from .assemble import assemble_video, create_description
from .text_normalization import normalizer_backend


def _logger(verbose: bool = False) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", datefmt="%H:%M:%S")


def _path(value: str, base: Path | None = None) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute(): p = (base or ROOT) / p
    return p.resolve()


def cmd_doctor(args: argparse.Namespace) -> int:
    s = load_settings()
    print("VIDEO-FARM — проверка окружения")
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    print(f"ОС: {sys.platform}; разрядность Python: {'64-bit' if sys.maxsize > 2**32 else '32-bit'}")
    print(f"Корень проекта: {s.root}")
    problems = []
    if sys.version_info < (3, 12) or sys.version_info >= (3, 14):
        problems.append("Нужен Python 3.12 или 3.13.")
    if sys.maxsize <= 2**32:
        problems.append("Нужен 64-разрядный Python.")

    for exe in (s.ffmpeg, s.ffprobe):
        found = shutil.which(exe) or (str(Path(exe).resolve()) if Path(exe).is_file() else None)
        print(f"{exe}: {'OK — ' + str(found) if found else 'НЕ НАЙДЕН'}")
        if not found:
            problems.append(f"Не найден {exe}; установите FFmpeg и добавьте bin в PATH.")
            continue
        try:
            version = subprocess.run([found, "-version"], capture_output=True, text=True, timeout=10)
            first_line = (version.stdout or version.stderr).splitlines()[0]
            print(f"  Версия: {first_line}")
            if version.returncode != 0:
                problems.append(f"Не удалось получить версию {exe}.")
        except Exception as exc:
            problems.append(f"Не удалось запустить {exe}: {exc}")

    print(f"YouTube API: {'настроен' if s.youtube_api_key else 'не настроен (анализ YouTube недоступен)'}")
    print(f"Pexels API: {'настроен' if s.pexels_api_key else 'не настроен (B-roll не будет скачан)'}")
    print(f"Pixabay API: {'настроен' if s.pixabay_api_key else 'не настроен (B-roll не будет скачан)'}")
    print(f"LLM: {', '.join(f'{p.name}:{p.model}' for p in s.providers) if s.providers else 'нет провайдеров'}")
    if not s.providers:
        print("  Заполните LLM_PRIMARY_* в .env, если нужна генерация сценария через LLM.")
    print(f"TORCH_HOME: {os.environ.get('TORCH_HOME')}")

    for module, label in (("PIL", "Pillow"), ("soundfile", "soundfile")):
        try:
            mod = __import__(module)
            print(f"{label}: OK ({getattr(mod, '__version__', 'версия не определена')})")
        except Exception as exc:
            problems.append(f"Не установлен или не загружается {label}: {exc}")

    normalization_status = normalizer_backend()
    if normalization_status.startswith("UNAVAILABLE"):
        print("Числовая нормализация TTS: НЕ ГОТОВА (цифры могут остаться в тексте; установите .[tts])")
        problems.append("Не установлен num2words для числовой нормализации TTS. Выполните: python -m pip install -e '.[tts]'.")
    else:
        print(f"Числовая нормализация TTS: OK ({normalization_status})")

    print(f"TTS engine: {s.tts_engine} / speaker={s.tts_speaker}")
    if s.tts_engine != "kokoro" or s.tts_speaker != "dima":
        problems.append("Для production требуется TTS_ENGINE=kokoro и TTS_SPEAKER=dima.")

    required_packages = (
        ("onnxruntime", "ONNX Runtime"),
        ("numpy", "NumPy"),
        ("soundfile", "SoundFile"),
        ("huggingface-hub", "Hugging Face Hub"),
        ("ruaccent", "RUAccent"),
        ("misaki", "Misaki"),
        ("phonemizer-fork", "phonemizer-fork"),
        ("espeakng-loader", "espeakng-loader"),
    )
    for package, label in required_packages:
        try:
            version = importlib.metadata.version(package)
            print(f"{label}: OK ({version})")
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"Не установлен {label}. Выполните install.ps1 для установки production-зависимостей Kokoro.")

    model_root = s.data_dir / "models" / "kokoro-ru-v2-onnx"
    model_file = model_root / "onnx" / "model_dima_quantized.onnx"
    voice_file = model_root / "voices" / "dima.bin"
    if model_file.is_file() and voice_file.is_file():
        print(f"Kokoro model: кэширована ({model_file.stat().st_size:,} байт; Dima voice pack найден)")
    else:
        print(f"Kokoro model: ещё не загружена; первая команда tts скачает revision d649c57 в {model_root}")

    accent_root = s.data_dir / "models" / "ruaccent-accentuator"
    accent_required = (
        accent_root / "dictionary",
        accent_root / "nn" / "nn_accent",
        accent_root / "nn" / "nn_stress_usage_predictor",
        accent_root / "nn" / "nn_yo_homograph_resolver",
    )
    missing_accent = [str(path) for path in accent_required if not path.is_dir()]
    if missing_accent:
        problems.append(
            "Не найдены локальные данные RUAccent: " + ", ".join(missing_accent)
            + ". Production TTS не будет запускаться без этих данных."
        )
    else:
        print(f"RUAccent assets: OK ({accent_root})")

    stress_config = s.root / "data" / "kokoro_stress_config.json"
    if stress_config.is_file():
        print(f"Kokoro stress config: OK ({stress_config})")
    else:
        problems.append(f"Не найден файл контекстных ударений Kokoro: {stress_config}")

    if problems:
        print("\nПроблемы:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nБазовые зависимости в порядке. При первом запуске TTS потребуется интернет для загрузки модели Kokoro. Локальные RUAccent assets должны быть заранее подготовлены в data/models/ruaccent-accentuator.")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    raise RuntimeError(
        "Старая команда analyze временно отключена: она рассчитывает собственные "
        "производные показатели (viral_ratio, ai_suitability, частоты слов и др.) "
        "по YouTube API Data. До одобрения YouTube на дополнительные аналитические "
        "метрики используйте безопасную команду discover."
    )


def cmd_discover(args: argparse.Namespace) -> int:
    s = load_settings()
    out = _path(args.out)
    report = discover_youtube(
        s, out, query=args.query, region=args.region,
        language=args.language, max_results=args.max_results,
        max_pages=args.max_pages, search_call_budget=args.search_call_budget,
    )
    print(f"Готово: {out / 'discovery.json'} и {out / 'discovery.md'}")
    print(f"Уникальных видео: {report['candidate_count']}")
    print(f"Поисковых вызовов search.list: {report['quota_usage']['search_list_calls']} / {args.search_call_budget}")
    daily = report["quota_usage"]["search_list_daily_accounting"]
    print(f"Учтено за Pacific-день {daily['pacific_date']}: {daily['total_accounted_calls']} / {daily['daily_ceiling']}; осталось {daily['remaining']}")
    print(f"Запросов videos.list: {report['quota_usage']['videos_list_calls']}")
    if report['search_budget_exhausted']:
        print("ВНИМАНИЕ: достигнут лимит поисковых вызовов; отчёт может быть неполным.")
    print("Показаны исходные данные API; скорость роста и трендовый балл не рассчитываются.")
    return 0


def cmd_discover_quota(args: argparse.Namespace) -> int:
    s = load_settings()
    from .discovery_quota import YouTubeSearchQuotaLedger
    ledger = YouTubeSearchQuotaLedger(s.data_dir / "usage" / "youtube_search_quota.json")
    if args.external_search_calls is not None:
        ledger.set_external_usage(args.external_search_calls)
        print(f"Внешние вызовы search.list за текущий Pacific-день записаны: {args.external_search_calls}")
    status = ledger.status()
    print(f"Pacific-дата: {status['pacific_date']} ({status['timezone']})")
    print(f"Вызовы приложения: {status['application_attempts']}")
    print(f"Внешнее использование: {status['external_calls']}")
    print(f"Всего учтено: {status['total_accounted_calls']} / {status['daily_ceiling']}")
    print(f"Остаток локального лимита: {status['remaining']}")
    print(f"Журнал: {status['ledger_path']}")
    print("Примечание: локальный журнал не видит обращения из Google Cloud Console, других ПК и приложений без ручной сверки.")
    return 0


def cmd_discover_analyze(args: argparse.Namespace) -> int:
    source = _path(args.input)
    out = _path(args.out)
    report = analyze_discovery(source, out, limit=args.limit)
    print(f"Редакторские подборки: {out / 'topics.json'} и {out / 'topics.md'}")
    print(f"Обработано уникальных видео: {report['candidate_count']}")
    print("Автоматическая семантическая классификация и трендовые метрики не выполняются.")
    return 0


def cmd_discover_editor(args: argparse.Namespace) -> int:
    source = _path(args.input)
    out = _path(args.out)
    page = out / "editor.html"
    result = build_review_page(source, page)
    print(f"Редакторская панель: {page}")
    print(f"Уникальных видео загружено: {result['candidate_count']}")
    print(f"Данные обновить или удалить до: {result['source_refresh_or_delete_by_utc']}")
    print("Откройте editor.html в браузере. Страница работает локально и не вызывает YouTube API.")
    return 0


def cmd_discover_cleanup(args: argparse.Namespace) -> int:
    out = _path(args.out)
    removed = purge_expired_discovery_output(out)
    if removed:
        print(f"Удалён просроченный YouTube API отчёт: {out}")
    else:
        print(f"Просроченный отчёт не найден или срок хранения ещё не истёк: {out}")
    return 0


def cmd_topics(args: argparse.Namespace) -> int:
    s = load_settings(); analysis = _path(args.analysis) if args.analysis else None
    if analysis and analysis.exists():
        raise RuntimeError(
            "Старая генерация topics на основе YouTube analysis временно отключена: "
            "она обрабатывает производные показатели и объединённые данные API. "
            "В первой версии discover выбери кандидата непосредственно по исходным данным."
        )
    out = _path(args.out)
    payload = generate_topics(s, None, out, count=args.count)
    print(f"Сохранено шаблонных тем: {len(payload.get('topics', []))} -> {out} (source={payload.get('source')})")
    return 0


def cmd_script(args: argparse.Namespace) -> int:
    s = load_settings(); research = _path(args.research) if args.research else None
    out = _path(args.out)
    payload = generate_script(s, args.topic, out, research_path=research, min_words=args.min_words)
    print(f"Сценарий: {out}; слов: {payload.get('word_count')}")
    for warning in payload.get("quality_warnings", []): print(f"ПРЕДУПРЕЖДЕНИЕ: {warning}")
    return 0


def cmd_tts(args: argparse.Namespace) -> int:
    s = load_settings(); script = _path(args.script); out = _path(args.out)
    manifest = synthesize(s, script, out, speed=args.speed)
    print(f"Озвучено блоков: {len(manifest['blocks'])}; длительность: {manifest['total_duration_seconds'] / 60:.2f} мин; манифест: {out / 'manifest.json'}")
    return 0


def cmd_assets(args: argparse.Namespace) -> int:
    s = load_settings(); script = _path(args.script); out = _path(args.out)
    manifest = download_assets(s, script, out, max_keywords=args.max_keywords)
    print(f"Клипов: {len(manifest.get('broll', []))}; фото: {len(manifest.get('images', []))}; статус: {manifest.get('status')}")
    if manifest.get("notice"): print("Примечание:", manifest["notice"])
    return 0


def cmd_slides(args: argparse.Namespace) -> int:
    s = load_settings(); script = _path(args.script); out = _path(args.out)
    assets = _path(args.assets) if args.assets else None
    manifest = render_slides(s, script, out, assets)
    print(f"PNG-слайдов: {len(manifest['slides'])}; каталог: {out}; RGB={all(x.get('mode') == 'RGB' for x in manifest['slides'])}")
    return 0


def cmd_assemble(args: argparse.Namespace) -> int:
    s = load_settings(); script = _path(args.script); audio = _path(args.audio_dir); slides = _path(args.slides_dir)
    assets = _path(args.assets); out = _path(args.out)
    report = assemble_video(s, script, audio, slides, assets, out, draft=args.draft)
    desc_path = args.description or str(out.with_suffix(".txt"))
    create_description(script, _path(desc_path), assets, used_media=report.get("used_media", []))
    print(f"Видео: {out}\nДлительность: {report['duration_minutes']} мин; размер: {report['video'].get('width')}x{report['video'].get('height')}\nQC: {out.with_suffix('.qc.json')}")
    for warning in report.get("warnings", []): print("ПРЕДУПРЕЖДЕНИЕ:", warning)
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    s = load_settings(); assets = _path(args.assets) if args.assets else None
    out = _path(args.out)
    create_description(_path(args.script), out, assets)
    print(f"Описание: {out}; JSON: {out.with_suffix('.json')}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    s = load_settings()
    run_id = args.name or "video"
    base = ensure_dir(_path(args.out_dir)) / run_id
    ensure_dir(base)
    if args.script_file:
        script_path = _path(args.script_file)
        if not script_path.exists(): raise FileNotFoundError(f"Не найден сценарий: {script_path}")
        print(f"[1/6] Используется готовый сценарий: {script_path}")
    else:
        if not args.topic: raise ValueError("Задайте --topic или --script-file")
        script_path = base / "script.json"
        print("[1/6] Генерация сценария...")
        generate_script(s, args.topic, script_path, _path(args.research) if args.research else None)
    voice_dir = base / "audio"
    print("[2/6] Локальная озвучка (Kokoro-ru v2 Dima / CPU)...")
    synth = synthesize(s, script_path, voice_dir, speed=args.speed)
    assets_manifest = base / "assets_manifest.json"
    print("[3/6] Поиск лицензируемых видео и фото...")
    assets = download_assets(s, script_path, assets_manifest, max_keywords=args.max_keywords)
    slides_dir = base / "slides"
    print("[4/6] Рендер слайдов 1920x1080 RGB...")
    slides = render_slides(s, script_path, slides_dir, assets_manifest)
    video_path = base / f"{run_id}{'_draft' if args.draft else ''}.mp4"
    description_path = base / "publish.txt"
    print("[5/6] Сборка и проверка MP4 через FFmpeg/ffprobe...")
    report = assemble_video(s, script_path, voice_dir, slides_dir, assets_manifest, video_path, draft=args.draft)
    create_description(script_path, description_path, assets_manifest, used_media=report.get("used_media", []))
    print("[6/6] Готово")
    print(f"Сценарий: {script_path}")
    print(f"Озвучка: {voice_dir} ({synth['total_duration_seconds']/60:.2f} мин)")
    print(f"Медиа: {len(assets.get('broll', []))} клипов, {len(assets.get('images', []))} фото")
    print(f"Слайды: {len(slides.get('slides', []))}")
    print(f"Видео: {video_path} ({report['duration_minutes']} мин)")
    print(f"Описание: {description_path}")
    print(f"QC: {video_path.with_suffix('.qc.json')}")
    warnings = list(report.get("warnings", [])) + list(read_json(script_path).get("quality_warnings", []))
    if warnings:
        print("\nТребует внимания:")
        for w in warnings: print(" -", w)
    print("\nПеред публикацией: проверьте факты и лицензии; загрузка в Rutube/VK не выполняется автоматически.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="video-farm", description="Локальная фабрика видео для Rutube / VK Video")
    p.add_argument("--verbose", action="store_true", help="Подробный лог")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor", help="Проверка локального окружения"); d.set_defaults(func=cmd_doctor)
    a = sub.add_parser("analyze", help="LEGACY: отключено до разрешения на производные аналитические метрики YouTube")
    a.add_argument("--queries", nargs="+", help="Поисковые запросы")
    a.add_argument("--queries-file", default="data/queries.txt")
    a.add_argument("--out", default="output/analysis")
    a.add_argument("--per-query", type=int, default=25)
    a.add_argument("--region", default="RU"); a.add_argument("--days", type=int, default=365)
    a.set_defaults(func=cmd_analyze)
    disc = sub.add_parser("discover", help="Ежедневный широкий поиск популярных видео YouTube по 10 направлениям")
    disc.add_argument("--query", default=None, help="Необязательно: вместо широкого поиска выполнить один пользовательский запрос; OR можно задавать через |")
    disc.add_argument("--region", default="RU", help="Регион доступности российской выдачи, ISO 3166-1 alpha-2")
    disc.add_argument("--language", default="ru", help="Языковой приоритет российской выдачи")
    disc.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS, help="Число результатов на страницу (1–50)")
    disc.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="Максимум страниц на поисковый набор (1–3); по умолчанию 1; дополнительные страницы используют остаток дневного бюджета")
    disc.add_argument("--search-call-budget", type=int, default=DEFAULT_SEARCH_CALL_BUDGET, help="Лимит вызовов search.list на запуск (1–100); по умолчанию 90")
    disc.add_argument("--out", default="output/discovery", help="Каталог отчётов (файлы discovery.json и discovery.md перезаписываются)")
    disc.set_defaults(func=cmd_discover)
    dq = sub.add_parser("discover-quota", help="Проверить и сверить дневной учёт search.list")
    dq.add_argument("--external-search-calls", type=int, default=None, help="Фактическое число search.list за текущий Pacific-день из других приложений/ПК")
    dq.set_defaults(func=cmd_discover_quota)
    da = sub.add_parser("discover-analyze", help="Create editorial lists from a saved discovery.json")
    da.add_argument("--input", default="output/discovery/discovery.json", help="Source discovery.json, schema 2.0")
    da.add_argument("--out", default="output/discovery", help="Output directory for topics.json and topics.md")
    da.add_argument("--limit", type=int, default=20, help="Maximum videos in each list (1–100)")
    da.set_defaults(func=cmd_discover_analyze)
    de = sub.add_parser("discover-editor", help="Создать локальную HTML-панель ручного отбора всех видео Discovery")
    de.add_argument("--input", default="output/discovery/discovery.json", help="Исходный discovery.json schema 2.0")
    de.add_argument("--out", default="output/discovery", help="Каталог для editor.html")
    de.set_defaults(func=cmd_discover_editor)
    disc_cleanup = sub.add_parser("discover-cleanup", help="Удалить просроченный отчёт discover старше 30 дней")
    disc_cleanup.add_argument("--out", default="output/discovery", help="Каталог отчётов discover")
    disc_cleanup.set_defaults(func=cmd_discover_cleanup)
    t = sub.add_parser("topics", help="LEGACY: шаблонные темы; анализ данных YouTube отключён")
    t.add_argument("--analysis", default="output/analysis/analysis.json"); t.add_argument("--out", default="output/topics.json")
    t.add_argument("--count", type=int, default=20); t.set_defaults(func=cmd_topics)
    sc = sub.add_parser("script", help="Создание структурированного сценария")
    sc.add_argument("--topic", required=True); sc.add_argument("--research"); sc.add_argument("--out", default="output/script.json")
    sc.add_argument("--min-words", type=int, default=1100); sc.set_defaults(func=cmd_script)
    tt = sub.add_parser("tts", help="Локальная Kokoro-ru v2 Dima TTS (CPU)")
    tt.add_argument("--script", required=True); tt.add_argument("--out", default="output/audio"); tt.add_argument("--speed", type=float, default=0.9); tt.set_defaults(func=cmd_tts)
    asst = sub.add_parser("assets", help="Скачивание стоковых B-roll и фото")
    asst.add_argument("--script", required=True); asst.add_argument("--out", default="output/assets_manifest.json")
    asst.add_argument("--max-keywords", type=int, default=30); asst.set_defaults(func=cmd_assets)
    sl = sub.add_parser("slides", help="Рендер tech-слайдов в PNG RGB")
    sl.add_argument("--script", required=True); sl.add_argument("--assets"); sl.add_argument("--out", default="output/slides"); sl.set_defaults(func=cmd_slides)
    ass = sub.add_parser("assemble", help="Сборка MP4 и QC")
    ass.add_argument("--script", required=True); ass.add_argument("--audio-dir", required=True); ass.add_argument("--slides-dir", required=True)
    ass.add_argument("--assets", default="output/assets_manifest.json"); ass.add_argument("--out", default="output/video.mp4")
    ass.add_argument("--description"); ass.add_argument("--draft", action="store_true"); ass.set_defaults(func=cmd_assemble)
    de = sub.add_parser("describe", help="Создать описание для публикации")
    de.add_argument("--script", required=True); de.add_argument("--assets"); de.add_argument("--out", default="output/publish.txt"); de.set_defaults(func=cmd_describe)
    r = sub.add_parser("run", help="Полный конвейер сценарий → WAV → медиа → PNG → MP4")
    r.add_argument("--topic"); r.add_argument("--script-file", help="Взять готовый JSON-сценарий, без вызова LLM")
    r.add_argument("--research"); r.add_argument("--name", default="video")
    r.add_argument("--out-dir", default="output/runs"); r.add_argument("--speed", type=float, default=0.9)
    r.add_argument("--max-keywords", type=int, default=30); r.add_argument("--draft", action="store_true")
    r.set_defaults(func=cmd_run)
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    _logger(args.verbose)
    try: code = args.func(args)
    except KeyboardInterrupt:
        print("Остановлено пользователем.", file=sys.stderr); code = 130
    except Exception as exc:
        logging.getLogger("kadr_farm").exception("Ошибка выполнения")
        print(f"ОШИБКА: {exc}", file=sys.stderr); code = 1
    raise SystemExit(code)

if __name__ == "__main__": main()
