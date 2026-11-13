from __future__ import annotations

"""Persistent, conservative accounting for YouTube search.list calls.

The ledger is local application bookkeeping, not an authoritative view of the
Google Cloud project quota. Calls made outside this installation must be
reconciled explicitly with ``discover-quota --external-search-calls N``.
"""

import datetime as dt
import json
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PACIFIC_ZONE = "America/Los_Angeles"
DEFAULT_DAILY_CEILING = 90


class QuotaLedgerError(RuntimeError):
    """Raised when local quota accounting cannot safely authorize a call."""


class DailyQuotaExceeded(QuotaLedgerError):
    """Raised when the configured daily search.list ceiling is exhausted."""


def _pacific_zone() -> ZoneInfo:
    try:
        return ZoneInfo(PACIFIC_ZONE)
    except ZoneInfoNotFoundError as exc:
        raise QuotaLedgerError(
            "Не найдена база часовых поясов IANA для America/Los_Angeles. "
            "Установите зависимость: .\\.venv\\Scripts\\python.exe -m pip install tzdata"
        ) from exc


def _utc_iso(value: dt.datetime | None = None) -> str:
    current = value or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Время должно содержать часовой пояс")
    return current.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# Windows msvcrt byte-range locks are process-scoped in a way that can make
# concurrent threads in the same process fail with errno 36 (EDEADLK), rather
# than waiting for one another. Serialize threads first, then use the OS lock
# for coordination with other processes.
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(lock_path: Path) -> threading.Lock:
    key = os.path.normcase(str(lock_path.resolve()))
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    """Cross-thread and cross-process lock using stdlib primitives only."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _thread_lock_for(lock_path)
    with thread_lock:
        handle = open(lock_path, "a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class YouTubeSearchQuotaLedger:
    """Conservatively track all local search.list attempts by Pacific date."""

    def __init__(self, path: Path, daily_ceiling: int = DEFAULT_DAILY_CEILING):
        if not 1 <= int(daily_ceiling) <= 100:
            raise ValueError("daily_ceiling должен быть в диапазоне 1–100")
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.daily_ceiling = int(daily_ceiling)

    def _today(self, now: dt.datetime | None = None) -> tuple[str, str]:
        current = now or dt.datetime.now(dt.timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now должен содержать часовой пояс")
        pacific_date = current.astimezone(_pacific_zone()).date().isoformat()
        return pacific_date, _utc_iso(current)

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {
                "schema_version": "1.0",
                "timezone": PACIFIC_ZONE,
                "daily_ceiling": self.daily_ceiling,
                "days": {},
            }
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QuotaLedgerError(
                f"Не удалось прочитать журнал квоты {self.path}: {exc}. "
                "Автоматический сброс журнала запрещён, чтобы не превысить квоту."
            ) from exc
        if not isinstance(data, dict) or data.get("schema_version") != "1.0" or not isinstance(data.get("days"), dict):
            raise QuotaLedgerError(
                f"Журнал квоты {self.path} имеет неизвестную структуру. "
                "Не удаляйте его автоматически; сначала сохраните копию и проверьте файл."
            )
        data["timezone"] = PACIFIC_ZONE
        data["daily_ceiling"] = self.daily_ceiling
        return data

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, self.path)

    def _day_unlocked(self, data: dict[str, Any], day: str) -> dict[str, Any]:
        days = data.setdefault("days", {})
        row = days.setdefault(day, {"application_attempts": 0, "external_calls": 0, "attempts": []})
        if not isinstance(row, dict):
            raise QuotaLedgerError(f"Некорректная запись за {day} в журнале квоты.")
        row.setdefault("application_attempts", 0)
        row.setdefault("external_calls", 0)
        row.setdefault("attempts", [])
        try:
            app_calls = int(row["application_attempts"])
            external_calls = int(row["external_calls"])
        except (TypeError, ValueError) as exc:
            raise QuotaLedgerError(f"Некорректные счётчики квоты за {day}; журнал не будет сброшен.") from exc
        if app_calls < 0 or external_calls < 0 or not isinstance(row["attempts"], list):
            raise QuotaLedgerError(f"Некорректные значения квоты за {day}; журнал не будет сброшен.")
        row["application_attempts"] = app_calls
        row["external_calls"] = external_calls
        return row

    def reserve_attempt(self, *, run_id: str, context: dict[str, Any],
                        now: dt.datetime | None = None) -> str:
        """Persist the attempt BEFORE HTTP; even failed calls consume one slot."""
        day, timestamp = self._today(now)
        call_id = uuid.uuid4().hex
        with _file_lock(self.lock_path):
            data = self._read_unlocked()
            row = self._day_unlocked(data, day)
            used = int(row["application_attempts"]) + int(row["external_calls"])
            if used >= self.daily_ceiling:
                raise DailyQuotaExceeded(
                    f"Локальный лимит YouTube search.list исчерпан: {used}/{self.daily_ceiling} "
                    f"в Pacific-день {day}. Новый запрос не отправлен."
                )
            row["application_attempts"] = int(row["application_attempts"]) + 1
            row["attempts"].append({
                "call_id": call_id,
                "run_id": run_id,
                "timestamp_utc": timestamp,
                "context": context,
                "outcome": "reserved_before_http",
            })
            self._write_unlocked(data)
        return call_id

    def finish_attempt(self, call_id: str, outcome: str, *, error_reason: str | None = None,
                       now: dt.datetime | None = None) -> None:
        day, _ = self._today(now)
        with _file_lock(self.lock_path):
            data = self._read_unlocked()
            row = self._day_unlocked(data, day)
            match = next((x for x in reversed(row["attempts"]) if x.get("call_id") == call_id), None)
            if match is None:
                # A Pacific midnight boundary may have passed during the HTTP request.
                for old_row in data.get("days", {}).values():
                    match = next((x for x in reversed(old_row.get("attempts", [])) if x.get("call_id") == call_id), None)
                    if match is not None:
                        break
            if match is not None:
                match["outcome"] = outcome
                if error_reason:
                    match["error_reason"] = str(error_reason)[:100]
                match["finished_at_utc"] = _utc_iso(now)
            self._write_unlocked(data)

    def set_external_usage(self, calls: int, *, now: dt.datetime | None = None) -> None:
        if not 0 <= int(calls) <= 100:
            raise ValueError("external_search_calls должен быть в диапазоне 0–100")
        day, timestamp = self._today(now)
        with _file_lock(self.lock_path):
            data = self._read_unlocked()
            row = self._day_unlocked(data, day)
            row["external_calls"] = int(calls)
            row["external_reconciled_at_utc"] = timestamp
            self._write_unlocked(data)

    def status(self, *, now: dt.datetime | None = None) -> dict[str, Any]:
        day, timestamp = self._today(now)
        with _file_lock(self.lock_path):
            data = self._read_unlocked()
            row = self._day_unlocked(data, day)
            app = int(row["application_attempts"])
            external = int(row["external_calls"])
            used = app + external
            return {
                "pacific_date": day,
                "checked_at_utc": timestamp,
                "timezone": PACIFIC_ZONE,
                "daily_ceiling": self.daily_ceiling,
                "application_attempts": app,
                "external_calls": external,
                "total_accounted_calls": used,
                "remaining": max(0, self.daily_ceiling - used),
                "over_limit": used > self.daily_ceiling,
                "external_reconciled_at_utc": row.get("external_reconciled_at_utc"),
                "ledger_path": str(self.path),
            }
