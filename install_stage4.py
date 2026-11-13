from __future__ import annotations

import codecs
import datetime as dt
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PAYLOAD = ROOT / "stage4"
PYTHON = Path(sys.executable)


def read_utf8(path: Path) -> tuple[str, bool, str]:
    raw = path.read_bytes()
    bom = raw.startswith(codecs.BOM_UTF8)
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), bom, newline


def write_utf8(path: Path, text: str, bom: bool, newline: str) -> None:
    rendered = text.replace("\n", newline)
    data = rendered.encode("utf-8")
    if bom:
        data = codecs.BOM_UTF8 + data
    path.write_bytes(data)


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Точка установки '{label}' найдена {count} раз вместо одного. Файлы проекта не изменены.")
    return text.replace(old, new, 1)


def run(args: list[str]) -> None:
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode:
        raise RuntimeError(f"Команда завершилась с кодом {result.returncode}: {' '.join(args)}")


def main() -> int:
    print("[Stage 4] Проверка структуры проекта...", flush=True)
    if not (ROOT / "pyproject.toml").is_file() or not (ROOT / "src/kadr_farm/cli.py").is_file():
        raise RuntimeError("Распакуйте архив в корень video-farm (рядом с pyproject.toml).")
    if not (ROOT / ".venv/Scripts/python.exe").is_file():
        raise RuntimeError("Не найден .venv/Scripts/python.exe.")

    cli_path = ROOT / "src/kadr_farm/cli.py"
    discovery_path = ROOT / "src/kadr_farm/discovery.py"
    module_dest = ROOT / "src/kadr_farm/discovery_editor.py"
    test_dest = ROOT / "tests/test_discovery_editor.py"
    doc_dest = ROOT / "docs/TREND_DISCOVERY_STAGE4.md"
    payload_files = [
        (PAYLOAD / "src/kadr_farm/discovery_editor.py", module_dest),
        (PAYLOAD / "tests/test_discovery_editor.py", test_dest),
        (PAYLOAD / "docs/TREND_DISCOVERY_STAGE4.md", doc_dest),
    ]
    for source, dest in payload_files:
        if not source.is_file():
            raise RuntimeError(f"В архиве отсутствует файл: {source.relative_to(ROOT)}")
        if dest.exists():
            raise RuntimeError(f"Файл уже существует: {dest.relative_to(ROOT)}. Установка остановлена без изменений.")

    cli, cli_bom, cli_nl = read_utf8(cli_path)
    discovery, disc_bom, disc_nl = read_utf8(discovery_path)
    cli_new = cli
    cli_new = replace_once(
        cli_new,
        "from .discovery_analysis import analyze_discovery\n",
        "from .discovery_analysis import analyze_discovery\nfrom .discovery_editor import build_review_page\n",
        "импорт Stage 4",
    )
    cli_new = replace_once(
        cli_new,
        "\ndef cmd_discover_cleanup(args: argparse.Namespace) -> int:\n",
        '''\ndef cmd_discover_editor(args: argparse.Namespace) -> int:\n    source = _path(args.input)\n    out = _path(args.out)\n    page = out / "editor.html"\n    result = build_review_page(source, page)\n    print(f"Редакторская панель: {page}")\n    print(f"Уникальных видео загружено: {result['candidate_count']}")\n    print(f"Данные обновить или удалить до: {result['source_refresh_or_delete_by_utc']}")\n    print("Откройте editor.html в браузере. Страница работает локально и не вызывает YouTube API.")\n    return 0\n\n\ndef cmd_discover_cleanup(args: argparse.Namespace) -> int:\n''',
        "CLI-функция Stage 4",
    )
    cli_new = replace_once(
        cli_new,
        "    da.set_defaults(func=cmd_discover_analyze)\n",
        '''    da.set_defaults(func=cmd_discover_analyze)\n    de = sub.add_parser("discover-editor", help="Создать локальную HTML-панель ручного отбора всех видео Discovery")\n    de.add_argument("--input", default="output/discovery/discovery.json", help="Исходный discovery.json schema 2.0")\n    de.add_argument("--out", default="output/discovery", help="Каталог для editor.html")\n    de.set_defaults(func=cmd_discover_editor)\n''',
        "CLI-команда discover-editor",
    )
    disc_new = replace_once(
        discovery,
        '    (out_dir / "topics.md").unlink(missing_ok=True)\n    return True\n',
        '    (out_dir / "topics.md").unlink(missing_ok=True)\n'
        '    # Stage 4 page embeds raw YouTube API fields; editor export contains video IDs.\n'
        '    (out_dir / "editor.html").unlink(missing_ok=True)\n'
        '    (out_dir / "editorial.json").unlink(missing_ok=True)\n'
        '    return True\n',
        "30-дневная очистка Stage 4",
    )

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = ROOT / "backups" / f"discovery-stage4-{stamp}"
    changed = [cli_path, discovery_path]
    created: list[Path] = []
    backup_dir.mkdir(parents=True, exist_ok=False)
    try:
        for path in changed:
            shutil.copy2(path, backup_dir / path.name)
        write_utf8(cli_path, cli_new, cli_bom, cli_nl)
        write_utf8(discovery_path, disc_new, disc_bom, disc_nl)
        for source, dest in payload_files:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            created.append(dest)

        print("[Stage 4] Проверка синтаксиса Python...", flush=True)
        run([str(PYTHON), "-m", "py_compile", "src/kadr_farm/discovery_editor.py", "src/kadr_farm/cli.py", "src/kadr_farm/discovery.py"])
        print("[Stage 4] Тесты Stage 4 и Stage 3...", flush=True)
        run([str(PYTHON), "-m", "pytest", "-q", "tests/test_discovery_editor.py", "tests/test_discovery_analysis.py"])
        print("[Stage 4] Проверка CLI...", flush=True)
        run([str(PYTHON), "-m", "kadr_farm.cli", "discover-editor", "--help"])
        print("[Stage 4] Установка завершена успешно.", flush=True)
        print(f"Резервная копия: {backup_dir}", flush=True)
        print("Запуск: .\\.venv\\Scripts\\python.exe -m kadr_farm.cli discover-editor", flush=True)
        return 0
    except Exception:
        print("[Stage 4] Ошибка. Выполняется откат...", file=sys.stderr, flush=True)
        for path in changed:
            backup = backup_dir / path.name
            if backup.exists():
                shutil.copy2(backup, path)
        for path in created:
            path.unlink(missing_ok=True)
        print("[Stage 4] Откат выполнен.", file=sys.stderr, flush=True)
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Установка Stage 4 не завершена: {exc}", file=sys.stderr)
        raise SystemExit(1)
