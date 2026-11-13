from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def fail(message: str) -> None:
    raise RuntimeError(message)


def patch_cli(text: str) -> str:
    import_line = "from .discovery_analysis import analyze_discovery"
    if import_line not in text:
        pattern = re.compile(
            r"(?m)^from \.discovery import \(discover_youtube, purge_expired_discovery_output,[ \t]*\r?\n"
            r"[ \t]*DEFAULT_MAX_RESULTS, DEFAULT_MAX_PAGES, DEFAULT_SEARCH_CALL_BUDGET\)[ \t]*$"
        )
        match = pattern.search(text)
        if match is None:
            # Be tolerant of a slightly different wrapping of the same import.
            fallback = re.compile(
                r"(?ms)^from \.discovery import \([^\n]*\n(?:(?!\)).)*"
                r"DEFAULT_MAX_RESULTS.*?DEFAULT_SEARCH_CALL_BUDGET[^\n]*\)"
            )
            match = fallback.search(text)
        if match is None:
            fail("Cannot locate the Stage 2 import block in cli.py. No project files were changed.")
        text = text[:match.end()] + "\n" + import_line + text[match.end():]

    if "def cmd_discover_analyze(" not in text:
        marker = "def cmd_discover_cleanup(args: argparse.Namespace) -> int:"
        if marker not in text:
            fail("Cannot locate cmd_discover_cleanup() in cli.py. No project files were changed.")
        handler = """def cmd_discover_analyze(args: argparse.Namespace) -> int:
    source = _path(args.input)
    out = _path(args.out)
    report = analyze_discovery(source, out, limit=args.limit)
    print(f\"Редакторские подборки: {out / 'topics.json'} и {out / 'topics.md'}\")
    print(f\"Обработано уникальных видео: {report['candidate_count']}\")
    print(\"Автоматическая семантическая классификация и трендовые метрики не выполняются.\")
    return 0


"""
        text = text.replace(marker, handler + marker, 1)

    if 'sub.add_parser("discover-analyze"' not in text:
        marker = "    disc.set_defaults(func=cmd_discover)"
        if marker not in text:
            fail("Cannot locate the discover parser registration. No project files were changed.")
        parser_block = """    da = sub.add_parser("discover-analyze", help="Create editorial lists from a saved discovery.json")
    da.add_argument("--input", default="output/discovery/discovery.json", help="Source discovery.json, schema 2.0")
    da.add_argument("--out", default="output/discovery", help="Output directory for topics.json and topics.md")
    da.add_argument("--limit", type=int, default=20, help="Maximum videos in each list (1–100)")
    da.set_defaults(func=cmd_discover_analyze)
"""
        text = text.replace(marker, marker + "\n" + parser_block.rstrip(), 1)
    return text


def patch_discovery(text: str) -> str:
    if '(out_dir / "topics.json").unlink(missing_ok=True)' in text:
        return text
    pattern = re.compile(
        r'(?m)^(?P<indent>[ \t]*)json_path\.unlink\(missing_ok=True\)[ \t]*\r?\n'
        r'(?P=indent)\(out_dir / "discovery\.md"\)\.unlink\(missing_ok=True\)[ \t]*\r?\n'
        r'(?P=indent)return True[ \t]*$'
    )
    match = pattern.search(text)
    if match is None:
        fail("Cannot locate the 30-day cleanup block in discovery.py. No project files were changed.")
    indent = match.group("indent")
    replacement = "\n".join((
        indent + "json_path.unlink(missing_ok=True)",
        indent + '(out_dir / "discovery.md").unlink(missing_ok=True)',
        indent + "# Stage 3 reports contain copied YouTube API fields and share the same deadline.",
        indent + '(out_dir / "topics.json").unlink(missing_ok=True)',
        indent + '(out_dir / "topics.md").unlink(missing_ok=True)',
        indent + "return True",
    ))
    return text[:match.start()] + replacement + text[match.end():]


def run_checked(args: list[str], root: Path, env: dict[str, str], label: str) -> None:
    print(f"[Stage 3] {label}...", flush=True)
    result = subprocess.run([sys.executable, *args], cwd=root, env=env)
    if result.returncode:
        fail(f"{label} failed (exit code {result.returncode}).")


def install(root: Path) -> None:
    cli = root / "src" / "kadr_farm" / "cli.py"
    discovery = root / "src" / "kadr_farm" / "discovery.py"
    payload = root / "stage3"
    required = [
        payload / "src" / "kadr_farm" / "discovery_analysis.py",
        payload / "tests" / "test_discovery_analysis.py",
        payload / "docs" / "TREND_DISCOVERY_STAGE3.md",
    ]
    if not cli.is_file() or not discovery.is_file():
        fail("Project root not recognized. Expected src/kadr_farm/cli.py and discovery.py.")
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        fail("Archive is incomplete; missing: " + ", ".join(missing))

    # Compute all text edits before creating backups or changing any project file.
    cli_text = cli.read_text(encoding="utf-8-sig")
    discovery_text = discovery.read_text(encoding="utf-8-sig")
    new_cli = patch_cli(cli_text)
    new_discovery = patch_discovery(discovery_text)

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = root / "backups" / f"discovery-stage3-{stamp}"
    changes: list[tuple[Path, Path | None]] = []
    targets = [
        (cli, new_cli.encode("utf-8")),
        (discovery, new_discovery.encode("utf-8")),
        (root / "src" / "kadr_farm" / "discovery_analysis.py", required[0].read_bytes()),
        (root / "tests" / "test_discovery_analysis.py", required[1].read_bytes()),
        (root / "docs" / "TREND_DISCOVERY_STAGE3.md", required[2].read_bytes()),
    ]

    try:
        for target, data in targets:
            if target.exists():
                backup = backup_root / target.relative_to(root)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                changes.append((target, backup))
            else:
                changes.append((target, None))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        env = os.environ.copy()
        src_path = str(root / "src")
        env["PYTHONPATH"] = src_path + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        run_checked(["-m", "py_compile", str(root / "src" / "kadr_farm" / "discovery_analysis.py"), str(cli), str(discovery)], root, env, "Python syntax check")
        run_checked(["-m", "pytest", "tests/test_discovery_analysis.py", "-q"], root, env, "Stage 3 tests")
        run_checked(["-m", "kadr_farm.cli", "discover-analyze", "--help"], root, env, "CLI command check")
    except Exception:
        print("Installation failed; restoring original files...", flush=True)
        for target, backup in reversed(changes):
            if backup is not None and backup.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
            elif target.exists():
                target.unlink()
        print("Rollback complete.", flush=True)
        raise

    print("\nStage 3 installed successfully.")
    print("The original discovery.json was not modified. No YouTube API calls were made.")
    print(f"Backups: {backup_root}")
    print(r"Run: .\.venv\Scripts\python.exe -m kadr_farm.cli discover-analyze")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    try:
        install(Path(args.root).resolve())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
