from __future__ import annotations

import argparse
import datetime as dt
import shutil
import subprocess
import sys
from pathlib import Path


def run(args: list[str], root: Path, label: str) -> None:
    print(f"[Stage 5.2] {label}...", flush=True)
    result = subprocess.run(args, cwd=root)
    if result.returncode:
        raise RuntimeError(f"Команда завершилась с кодом {result.returncode}: {' '.join(args)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Установщик Stage 5.2 для video-farm")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--package-root", required=True)
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    package = Path(args.package_root).resolve()
    if not (root / "pyproject.toml").is_file() or not (root / "src" / "kadr_farm" / "discovery_editor.py").is_file():
        raise RuntimeError("Ожидался проект с установленным Stage 4: src/kadr_farm/discovery_editor.py")
    profile_path = root / "config" / "production_profiles.json"
    if not profile_path.is_file():
        raise RuntimeError("Не найден config/production_profiles.json. Сначала установите Stage 5.1 v1.0.1.")
    python = root / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        raise RuntimeError(f"Не найден Python проекта: {python}")
    version = subprocess.check_output([str(python), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"], text=True).strip()
    if version not in {"3.12", "3.13"}:
        raise RuntimeError(f"Ожидался Python 3.12 или 3.13, найден {version}")

    stage = package / "stage5_2"
    source_map = {
        "src/kadr_farm/discovery_editor.py": stage / "src/kadr_farm/discovery_editor.py",
        "tests/test_discovery_editor.py": stage / "tests/test_discovery_editor.py",
        "docs/TREND_DISCOVERY_STAGE5_2.md": stage / "docs/TREND_DISCOVERY_STAGE5_2.md",
    }
    for rel, path in source_map.items():
        if not path.is_file():
            raise RuntimeError(f"В пакете отсутствует {path}")

    # Verify the required catalogue before making changes.
    check = subprocess.run([str(python), "-c", "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8')); assert d.get('schema_version')=='1.0' and isinstance(d.get('profiles'),list)", str(profile_path)], cwd=root)
    if check.returncode:
        raise RuntimeError("production_profiles.json не прошёл проверку схемы; файлы проекта не изменены.")

    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = root / "backups" / f"stage5.2-{timestamp}"
    targets = list(source_map)
    backup_index: dict[str, bool] = {}
    for rel in targets:
        target = root / rel
        backup_index[rel] = target.is_file()
        if target.is_file():
            backup = backup_root / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
    backup_root.mkdir(parents=True, exist_ok=True)

    changed: list[str] = []
    try:
        for rel, source in source_map.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            changed.append(rel)
        run([str(python), "-m", "py_compile", "src/kadr_farm/discovery_editor.py"], root, "Проверка синтаксиса")
        run([str(python), "-m", "pytest", "-q", "tests/test_discovery_editor.py", "tests/test_discovery.py", "tests/test_discovery_quota.py", "tests/test_discovery_analysis.py"], root, "Регрессионные тесты Stage 3–5.2")
        run([str(python), "-m", "kadr_farm.cli", "discover-editor", "--help"], root, "Проверка CLI")
        print("[Stage 5.2] Установка завершена успешно.")
        print(f"Резервная копия: {backup_root}")
        print(r"Запуск: .\.venv\Scripts\python.exe -m kadr_farm.cli discover-editor")
        return 0
    except Exception:
        print("[Stage 5.2] Ошибка; выполняется откат...", file=sys.stderr, flush=True)
        for rel in reversed(changed):
            target = root / rel
            if backup_index.get(rel):
                backup = backup_root / rel
                if backup.exists(): shutil.copy2(backup, target)
            elif target.exists():
                target.unlink()
        print("[Stage 5.2] Откат завершён.", file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Stage 5.2 не установлен: {exc}", file=sys.stderr)
        raise SystemExit(1)
