"""Durable checkpoints, task journal and raw LLM call artifacts for production runs.

Runtime data is written below output/<run>/recovery and is intentionally not
stored in Git. Successful LLM results are reusable only when their complete
request fingerprint matches the current task.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import read_json, write_json


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def fingerprint(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _slug(value: str, limit: int = 56) -> str:
    result = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._-")
    return (result[:limit] or "task")


def classify_error(error: BaseException) -> str:
    name = type(error).__name__.lower()
    message = str(error).lower()
    if "timeout" in name or "timed out" in message or "timeout" in message:
        return "provider_timeout"
    if any(token in name for token in ("connection", "network", "api", "ratelimit")):
        return "provider_unavailable"
    if "json" in name or "json" in message or "validation" in name:
        return "invalid_model_output"
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return "local_validation_or_data_error"
    return "production_error"


class ProductionJournal:
    """Append-only diagnostics plus fingerprinted successful-result checkpoints."""

    VERSION = 1

    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir).resolve()
        self.root = self.run_dir / "recovery"
        self.calls_dir = self.root / "model_calls"
        self.results_dir = self.root / "llm_results"
        self.state_path = self.root / "production_state.json"
        self.calls_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

        if self.state_path.is_file():
            try:
                loaded_state = read_json(self.state_path)
                if not isinstance(loaded_state, dict):
                    raise ValueError("Production state must be a JSON object")
                self.state = loaded_state
            except Exception:
                broken = self.state_path.with_name(
                    f"production_state.corrupt_{time.time_ns()}.json"
                )
                self.state_path.replace(broken)
                self.state = self._new_state()
        else:
            self.state = self._new_state()

        previous_stage = self.state.get("current_stage")
        if previous_stage and previous_stage.get("status") == "running":
            interrupted_at = utc_now()
            reason = (
                "Процесс завершился до фиксации результата этапа. "
                "При повторном запуске переиспользуются только проверенные контрольные точки."
            )
            previous_stage["status"] = "interrupted"
            previous_stage["interrupted_at"] = interrupted_at
            previous_stage["interruption_reason"] = reason
            stage_id = previous_stage.get("stage")
            if stage_id:
                saved_stage = self.state.setdefault("stages", {}).setdefault(stage_id, {})
                saved_stage.update({
                    "status": "interrupted",
                    "interrupted_at": interrupted_at,
                    "interruption_reason": reason,
                })
            self.state["last_interruption"] = {
                "stage": stage_id,
                "at": interrupted_at,
                "reason": reason,
            }
        self.state["run_id"] = uuid.uuid4().hex
        self.state["run_started_at"] = utc_now()
        self.state["run_status"] = "running"
        self._save()

    def _new_state(self) -> dict[str, Any]:
        return {
            "schema_version": self.VERSION,
            "run_status": "created",
            "run_id": None,
            "run_started_at": None,
            "run_completed_at": None,
            "current_stage": None,
            "last_interruption": None,
            "run_context": {},
            "stages": {},
            "tasks": {},
            "model_call_count": 0,
            "policy": {
                "automatic_provider_fallback": False,
                "resume_rule": "reuse only completed artifacts with matching fingerprints",
            },
        }

    def _save(self) -> None:
        write_json(self.state_path, self.state)

    def set_run_context(self, context: dict[str, Any]) -> None:
        with self._lock:
            old = self.state.get("run_context") or {}
            if not isinstance(old, dict):
                old = {}

            previous_provider = old.get("provider_identity")
            requested_provider = context.get("provider_identity")
            has_previous_llm_tasks = (
                bool(self.state.get("tasks"))
                or int(self.state.get("model_call_count", 0)) > 0
            )

            if (
                previous_provider is not None
                and requested_provider != previous_provider
            ):
                raise ValueError(
                    "Cannot resume this production directory with a different "
                    "LLM provider, model or endpoint. Keep the original "
                    "configuration or use a new --out directory."
                )

            if previous_provider is None and has_previous_llm_tasks:
                raise ValueError(
                    "Cannot safely resume this production directory: existing "
                    "LLM tasks have no saved provider identity. Use a new "
                    "--out directory."
                )

            new_fp = fingerprint(context)
            old_fp = self.state.get("run_context_fingerprint")
            if old_fp and old_fp != new_fp:
                self.state.setdefault("context_history", []).append({
                    "fingerprint": old_fp,
                    "context": old,
                    "superseded_at": utc_now(),
                })
            self.state["run_context"] = context
            self.state["run_context_fingerprint"] = new_fp
            self._save()

    def start_stage(self, stage: str, *, input_fingerprint: str | None = None,
                    details: dict[str, Any] | None = None) -> None:
        with self._lock:
            previous = self.state.get("current_stage")
            if previous and previous.get("status") == "running":
                previous["status"] = "interrupted"
                previous["interrupted_at"] = utc_now()
            item = self.state.setdefault("stages", {}).get(stage, {})
            item.update({
                "stage": stage,
                "status": "running",
                "attempts": int(item.get("attempts", 0)) + 1,
                "started_at": utc_now(),
                "input_fingerprint": input_fingerprint,
                "details": details or {},
            })
            self.state["stages"][stage] = item
            self.state["current_stage"] = item
            self.state["run_status"] = "running"
            self._save()

    def complete_stage(self, stage: str, *, artifact: str | None = None,
                       details: dict[str, Any] | None = None) -> None:
        with self._lock:
            item = self.state.setdefault("stages", {}).setdefault(stage, {"stage": stage})
            item.update({"status": "completed", "completed_at": utc_now(), "artifact": artifact})
            if details:
                item.setdefault("details", {}).update(details)
            self.state["current_stage"] = item
            self._save()

    def fail_stage(self, stage: str, error: BaseException) -> None:
        with self._lock:
            item = self.state.setdefault("stages", {}).setdefault(stage, {"stage": stage})
            item.update({
                "status": "failed", "failed_at": utc_now(),
                "error_type": type(error).__name__, "error": str(error)[:4000],
                "error_class": classify_error(error),
            })
            self.state["current_stage"] = item
            self.state["run_status"] = "paused"
            self._save()

    def cached_result(self, task_id: str, input_fingerprint: str) -> dict[str, Any] | None:
        with self._lock:
            item = (self.state.get("tasks") or {}).get(task_id) or {}
            if (item.get("status") != "completed"
                    or item.get("input_fingerprint") != input_fingerprint
                    or not item.get("result_file")):
                return None
            path = self.run_dir / item["result_file"]
            if not path.is_file():
                return None
            try:
                payload = read_json(path)
            except Exception:
                return None
            if not isinstance(payload, dict):
                return None
            if (payload.get("task_id") != task_id
                    or payload.get("input_fingerprint") != input_fingerprint):
                return None
            result = payload.get("result")
            return result if isinstance(result, dict) else None

    def start_task(self, task_id: str, *, stage: str, input_fingerprint: str,
                   provider: str, model: str) -> None:
        with self._lock:
            tasks = self.state.setdefault("tasks", {})
            item = tasks.get(task_id, {})
            item.update({
                "task_id": task_id, "stage": stage, "status": "running",
                "input_fingerprint": input_fingerprint,
                "attempts": int(item.get("attempts", 0)) + 1,
                "started_at": utc_now(), "provider": provider, "model": model,
                "last_error": None,
            })
            tasks[task_id] = item
            self._save()

    def record_model_call(self, *, task_id: str, stage: str, provider: str,
                          model: str, request: dict[str, Any],
                          raw_response: str | None = None,
                          response_metadata: dict[str, Any] | None = None,
                          error: BaseException | None = None,
                          call_kind: str = "initial") -> str:
        with self._lock:
            call_id = f"{time.time_ns()}_{uuid.uuid4().hex[:8]}"
            path = self.calls_dir / f"{_slug(task_id)}_{call_kind}_{call_id}.json"
            payload = {
                "call_id": call_id, "task_id": task_id, "stage": stage,
                "call_kind": call_kind, "created_at": utc_now(),
                "provider": provider, "model": model, "request": request,
                "raw_response": raw_response,
                "response_metadata": response_metadata or {},
                "error": None if error is None else {
                    "type": type(error).__name__, "message": str(error)[:4000],
                    "classification": classify_error(error),
                },
                "validation": {"status": "not_checked"},
            }
            write_json(path, payload)
            item = self.state.setdefault("tasks", {}).setdefault(task_id, {
                "task_id": task_id, "stage": stage, "status": "running",
            })
            item.setdefault("model_calls", []).append(str(path.relative_to(self.run_dir)))
            self.state["model_call_count"] = int(self.state.get("model_call_count", 0)) + 1
            self._save()
            return str(path.relative_to(self.run_dir))

    def update_model_call(self, path: str | Path, validation: dict[str, Any]) -> None:
        with self._lock:
            target = Path(path)
            if not target.is_absolute():
                target = self.run_dir / target
            if target.is_file():
                payload = read_json(target)
                payload["validation"] = validation
                write_json(target, payload)

    def complete_task(self, task_id: str, *, stage: str, input_fingerprint: str,
                      result: dict[str, Any], schema_name: str,
                      provider: str, model: str, raw_artifact: str | None) -> None:
        with self._lock:
            cache_name = f"{_slug(task_id)}_{input_fingerprint[:20]}.json"
            cache_path = self.results_dir / cache_name
            write_json(cache_path, {
                "task_id": task_id, "stage": stage,
                "input_fingerprint": input_fingerprint, "schema": schema_name,
                "provider": provider, "model": model, "completed_at": utc_now(),
                "raw_artifact": raw_artifact, "result": result,
            })
            item = self.state.setdefault("tasks", {}).setdefault(task_id, {})
            item.update({
                "task_id": task_id, "stage": stage, "status": "completed",
                "input_fingerprint": input_fingerprint, "completed_at": utc_now(),
                "result_file": str(cache_path.relative_to(self.run_dir)),
                "schema": schema_name, "provider": provider, "model": model,
                "raw_artifact": raw_artifact, "last_error": None,
            })
            self._save()

    def reject_task(self, task_id: str, reason: str) -> None:
        """Keep raw/model-call artifacts but prevent a semantically rejected result being reused."""
        with self._lock:
            item = self.state.setdefault("tasks", {}).setdefault(task_id, {})
            item.update({
                "task_id": task_id,
                "status": "rejected",
                "rejected_at": utc_now(),
                "business_validation_error": str(reason)[:4000],
            })
            self._save()

    def fail_task(self, task_id: str, *, stage: str, input_fingerprint: str,
                  provider: str, model: str, error: BaseException) -> None:
        with self._lock:
            item = self.state.setdefault("tasks", {}).setdefault(task_id, {})
            item.update({
                "task_id": task_id, "stage": stage, "status": "failed",
                "input_fingerprint": input_fingerprint, "failed_at": utc_now(),
                "provider": provider, "model": model,
                "last_error": {
                    "type": type(error).__name__, "message": str(error)[:4000],
                    "classification": classify_error(error),
                },
            })
            self._save()

    def finish_run(self, *, status: str = "complete", report_path: str | None = None) -> None:
        with self._lock:
            self.state["run_status"] = status
            self.state["run_completed_at"] = utc_now()
            self.state["report_path"] = report_path
            self.state["current_stage"] = None
            self._save()

    def state_file(self) -> Path:
        return self.state_path
