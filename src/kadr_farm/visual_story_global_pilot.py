"""Safe, standalone Pass-1 pilot for KadrFarm's VisualStoryPlan.

Default mode is dry-run. Use --execute for one explicitly pinned provider.
This module does not import or modify production_director and never performs
per-scene composition, TTS, media retrieval, rendering, or video assembly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from copy import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

from .common import read_json, write_json
from .config import load_settings
from .llm_structured import StructuredLLMRouter
from .production_recovery import ProductionJournal, fingerprint
from .visual_story import VisualStoryPlan
from .visual_story_prompts import (
    DEFAULT_GLOBAL_MAX_TOKENS,
    DEFAULT_GLOBAL_TEMPERATURE,
    GLOBAL_PROMPT_VERSION,
    build_global_director_request,
    execute_prompt,
    storyboard_fingerprint,
    validate_global_plan_coverage,
)


log = logging.getLogger("kadr_farm.visual_story_global_pilot")


def pin_one_provider(settings: Any, provider_name: str, timeout_seconds: float) -> Any:
    """Return a shallow settings copy with exactly one explicitly named provider."""
    name = str(provider_name or "").strip().lower()
    if name not in {"primary", "secondary"}:
        raise ValueError("provider_name must be 'primary' or 'secondary'")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise TypeError("timeout_seconds must be numeric")
    timeout = float(timeout_seconds)
    if not 10.0 <= timeout <= 3600.0:
        raise ValueError("timeout_seconds must be between 10 and 3600")

    configured = list(getattr(settings, "providers", []) or [])
    selected = [item for item in configured if str(getattr(item, "name", "")).lower() == name]
    if len(selected) != 1:
        available = [str(getattr(item, "name", "unknown")) for item in configured]
        raise ValueError(
            f"Exactly one provider named {name!r} must be configured; available={available}"
        )

    pinned = copy(settings)
    pinned.providers = [replace(selected[0], timeout_seconds=timeout)]
    if len(pinned.providers) != 1:
        raise AssertionError("provider pinning invariant failed")
    return pinned


def provider_identity(settings: Any) -> dict[str, str]:
    providers = list(getattr(settings, "providers", []) or [])
    if len(providers) != 1:
        raise ValueError("global pilot requires exactly one pinned provider")
    item = providers[0]
    return {
        "name": str(getattr(item, "name", "")).strip().lower(),
        "model": str(getattr(item, "model", "")).strip(),
        "base_url": str(getattr(item, "base_url", "")).strip(),
    }


def preflight_existing_journal(output: Path, identity: dict[str, str]) -> None:
    """Reject unsafe resume attempts before ProductionJournal can mutate state."""
    state_path = output / "recovery" / "production_state.json"
    if not state_path.is_file():
        return

    state = read_json(state_path)
    if not isinstance(state, dict):
        raise ValueError("Existing pilot recovery state must be a JSON object")

    old_context = state.get("run_context") or {}
    if not isinstance(old_context, dict):
        raise ValueError("Existing pilot run context is invalid")

    previous_provider = old_context.get("provider_identity")
    has_previous_tasks = (
        bool(state.get("tasks"))
        or int(state.get("model_call_count", 0)) > 0
    )

    if previous_provider is not None and previous_provider != identity:
        raise ValueError(
            "Cannot resume this pilot directory with a different "
            "LLM provider, model or endpoint. Use the original configuration "
            "or a new --out directory."
        )

    if previous_provider is None and has_previous_tasks:
        raise ValueError(
            "Cannot safely resume this pilot directory: existing LLM tasks "
            "have no saved provider identity. Use a new --out directory."
        )

    previous_pilot = old_context.get("pilot")
    has_prior_state = (
        bool(old_context)
        or bool(state.get("stages"))
        or state.get("current_stage") is not None
        or has_previous_tasks
    )
    if has_prior_state and previous_pilot != "visual_story_global":
        raise ValueError(
            "Cannot reuse this recovery directory: it does not belong "
            "to the Global Director pilot. Use a new --out directory."
        )



def _request_preview(request: Any, identity: dict[str, str], timeout_seconds: float,
                     storyboard_sha256: str) -> dict[str, Any]:
    schema = request.schema_model.model_json_schema()
    schema_text = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "mode": "dry_run",
        "prompt_version": request.prompt_version,
        "task_id": request.task_id,
        "stage": request.stage,
        "provider_identity": identity,
        "timeout_seconds": timeout_seconds,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "storyboard_file_sha256": storyboard_sha256,
        "schema_sha256": hashlib.sha256(schema_text.encode("utf-8")).hexdigest(),
        "system_prompt_chars": len(request.system_prompt),
        "user_prompt_chars": len(request.user_prompt),
        "user_prompt_utf8_bytes": len(request.user_prompt.encode("utf-8")),
        "system_prompt": request.system_prompt,
        "user_prompt": request.user_prompt,
        "json_schema": schema,
    }


def run_global_pilot(
    *,
    storyboard_path: Path,
    out_dir: Path,
    provider_name: str = "primary",
    timeout_seconds: float = 300.0,
    temperature: float = DEFAULT_GLOBAL_TEMPERATURE,
    max_tokens: int = DEFAULT_GLOBAL_MAX_TOKENS,
    execute: bool = False,
) -> dict[str, Any]:
    source = Path(storyboard_path).expanduser().resolve()
    output = Path(out_dir).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"storyboard not found: {source}")
    if source == output or source.is_relative_to(output):
        raise ValueError("pilot output directory must not contain or replace the source storyboard")

    settings = load_settings()
    pinned_settings = pin_one_provider(settings, provider_name, timeout_seconds)
    identity = provider_identity(pinned_settings)
    storyboard = read_json(source)
    if not isinstance(storyboard, dict):
        raise ValueError("storyboard root must be a JSON object")

    request = build_global_director_request(
        storyboard, temperature=temperature, max_tokens=max_tokens
    )
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    preview = _request_preview(request, identity, float(timeout_seconds), source_sha)

    if not execute:
        output.mkdir(parents=True, exist_ok=True)
        write_json(output / "global_request_preview.json", preview)
        return {
            "mode": "dry_run",
            "storyboard": str(source),
            "output": str(output),
            "scene_count": len(storyboard.get("scenes", [])),
            "provider": identity,
            "timeout_seconds": float(timeout_seconds),
            "prompt_version": request.prompt_version,
            "task_id": request.task_id,
            "system_prompt_chars": len(request.system_prompt),
            "user_prompt_chars": len(request.user_prompt),
            "user_prompt_utf8_bytes": len(request.user_prompt.encode("utf-8")),
            "preview_path": str((output / "global_request_preview.json").resolve()),
            "llm_called": False,
        }

    output.mkdir(parents=True, exist_ok=True)

    # Check identity before ProductionJournal.__init__ can alter recovery state.
    preflight_existing_journal(output, identity)

    journal = ProductionJournal(output)
    stage_started = False
    run_context = {
        "pilot": "visual_story_global",
        "prompt_version": GLOBAL_PROMPT_VERSION,
        "storyboard_path": str(source),
        "storyboard_file_sha256": source_sha,
        "storyboard_content_fingerprint": storyboard_fingerprint(storyboard),
        "provider": identity["name"],
        "provider_identity": identity,
        "automatic_provider_fallback": False,
        "timeout_seconds": float(timeout_seconds),
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
    }
    try:
        journal.set_run_context(run_context)
        stage_fp = fingerprint({
            "run_context": run_context,
            "system_prompt": request.system_prompt,
            "user_prompt": request.user_prompt,
            "schema": request.schema_model.model_json_schema(),
        })
        journal.start_stage(request.stage, input_fingerprint=stage_fp,
                            details={"scene_count": len(storyboard.get("scenes", []))})
        stage_started = True

        # pin_one_provider() makes this router's client list length exactly one;
        # StructuredLLMRouter therefore cannot silently fall through to secondary.
        router = StructuredLLMRouter(pinned_settings, journal=journal)
        result = execute_prompt(router, request)
        if not isinstance(result, VisualStoryPlan):
            raise TypeError(f"unexpected global plan type: {type(result).__name__}")

        try:
            validated = validate_global_plan_coverage(
                storyboard, result,
                minimum_developing_share=0.5,
                require_recurring_motif=True,
            )
        except Exception as exc:
            journal.reject_task(request.task_id, f"Global Plan semantic validation failed: {exc}")
            raise

        plan_path = output / "visual_story_plan.json"
        write_json(plan_path, validated.model_dump(mode="json"))
        report = {
            "status": "complete",
            "human_review_required": True,
            "scene_count": len(validated.scenes),
            "motif_count": len(validated.motifs),
            "reused_motif_count": sum(bool(motif.returns_in) for motif in validated.motifs),
            "developing_scene_count": sum(
                scene.develops_scene_id is not None for scene in validated.scenes
            ),
            "visual_thesis": validated.visual_thesis,
            "arc_summary": validated.arc_summary,
            "provider_identity": identity,
            "prompt_version": request.prompt_version,
            "task_id": request.task_id,
            "storyboard_file_sha256": source_sha,
            "storyboard_content_fingerprint": storyboard_fingerprint(storyboard),
            "plan_path": str(plan_path.resolve()),
            "automatic_provider_fallback": False,
            "note": (
                "Pydantic and global structural/editorial-threshold validation passed. "
                "This is not human approval of the creative direction or factual claims."
            ),
        }
        report_path = output / "visual_story_global_report.json"
        write_json(report_path, report)
        journal.complete_stage(request.stage, artifact=str(plan_path.resolve()),
                               details={"scene_count": len(validated.scenes)})
        journal.finish_run(status="complete", report_path=str(report_path.resolve()))
        return report
    except Exception as exc:
        if stage_started:
            journal.fail_stage(request.stage, exc)
        else:
            journal.fail_stage("startup", exc)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kadr_farm.visual_story_global_pilot",
        description="Standalone, resumable Global Director pilot; dry-run is the default.",
    )
    parser.add_argument("--storyboard", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--provider", choices=("primary", "secondary"), default="primary")
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--temperature", type=float, default=DEFAULT_GLOBAL_TEMPERATURE)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_GLOBAL_MAX_TOKENS)
    parser.add_argument("--execute", action="store_true",
                        help="Actually call the explicitly selected provider. Without this flag, only dry-run is performed.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    try:
        result = run_global_pilot(
            storyboard_path=args.storyboard,
            out_dir=args.out,
            provider_name=args.provider,
            timeout_seconds=args.timeout_seconds,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            execute=args.execute,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        log.exception("Global Director pilot failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
