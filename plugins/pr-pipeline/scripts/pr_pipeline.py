#!/usr/bin/env python3
"""Run the PR pipeline as two bounded foreground sweeps."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid
from typing import Any, Callable


COMMON_MODULE_NAME = "pr_pipeline_common"
COMMON_PATH = Path(__file__).resolve().parent / "pipeline_common.py"
COMMON_SHA256 = "e06214907cb138784c00c1cef30f678f61abe095ca9ccc3ef2e796c681c2462b"


def load_common() -> Any:
    """Load the shared pipeline module that sits beside this script.

    The helper runs from an installed plugin directory that is not on
    ``sys.path``, so the shared module is loaded from its own file location and
    cached under a stable name.
    """
    source = COMMON_PATH.read_bytes()
    if hashlib.sha256(source).hexdigest() != COMMON_SHA256:
        raise RuntimeError("shared pipeline module integrity changed")
    cached = sys.modules.get(COMMON_MODULE_NAME)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(COMMON_MODULE_NAME, COMMON_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {COMMON_PATH}")
    code = compile(source, str(COMMON_PATH), "exec", dont_inherit=True)
    module = importlib.util.module_from_spec(spec)
    sys.modules[COMMON_MODULE_NAME] = module
    try:
        exec(code, module.__dict__)
    except BaseException:
        sys.modules.pop(COMMON_MODULE_NAME, None)
        raise
    return module


common = load_common()

WorkflowError = common.WorkflowError

MAX_SWEEPS = 2
DEFAULT_STAGE_MODEL = common.DEFAULT_STAGE_MODEL
DEFAULT_EFFORT = common.DEFAULT_EFFORT
IS_WINDOWS = common.IS_WINDOWS

STAGE_CONFLICT = common.STAGE_CONFLICT
STAGE_SELF_REVIEW = common.STAGE_SELF_REVIEW
STAGE_COPILOT_REVIEW = common.STAGE_COPILOT_REVIEW
STAGE_CI = common.STAGE_CI
STAGE_DESCRIPTION = common.STAGE_DESCRIPTION

STAGES = common.STAGES
STAGE_NAMES = common.STAGE_NAMES
STAGE_BY_NAME = common.STAGE_BY_NAME

STAGE_PERMISSION_FLAGS = common.STAGE_PERMISSION_FLAGS
STAGE_AUTOPILOT_FLAGS = common.STAGE_AUTOPILOT_FLAGS
PIPELINE_RUN_FLAG = common.PIPELINE_RUN_FLAG
PIPELINE_ITERATION_FLAG = common.PIPELINE_ITERATION_FLAG
PIPELINE_MAX_ITERATIONS_FLAG = common.PIPELINE_MAX_ITERATIONS_FLAG
CLEARING_OUTCOMES = common.CLEARING_OUTCOMES
STAGE_STATUS_FIELDS = common.STAGE_STATUS_FIELDS

run = common.run
git = common.git
git_or_none = common.git_or_none
git_succeeds = common.git_succeeds
emit = common.emit
report_event = common.report_safely
utc_now = common.utc_now
normalize_cli_path = common.normalize_cli_path
copilot_home = common.copilot_home
require_tools = common.require_tools
path_image = common.path_image
resolve_launch_program = common.resolve_launch_program
build_target = common.build_target
parse_target = common.parse_target
resolve_repo_root = common.resolve_repo_root
github_repo_from_remote = common.github_repo_from_remote
repo_name_for = common.repo_name_for
commit_url = common.commit_url
commits_added = common.commits_added
local_commits_between = common.local_commits_between
target_remote = common.target_remote
worktree_dirt = common.worktree_dirt
unreachable_commit_count = common.unreachable_commit_count
stage_script_path = common.stage_script_path
stage_state_path = common.stage_state_path
string_at = common.string_at
stage_status_summary = common.stage_status_summary
stage_models = common.stage_models
stage_prompt = common.stage_prompt

RUN_KIND = "pr-pipeline"
MONITOR_SCHEMA = "github.copilot.pr-pipeline-monitor"
MONITOR_VERSION = 1
PROGRESS_EVENT = common.PROGRESS_EVENT
PROGRESS_UPDATE_EVENT = common.PROGRESS_UPDATE_EVENT
PROGRESS_HEARTBEAT_INTERVAL = common.PROGRESS_HEARTBEAT_INTERVAL
TERMINAL_RESULT_MAX_BYTES = 8192
WATCH_MAX_BYTES = 8192
TERMINAL_COLLECTION_LIMIT = 12
TERMINAL_TEXT_LIMIT = 512
STAGE_LABELS = {
    STAGE_CONFLICT: "conflict resolution",
    STAGE_COPILOT_REVIEW: "Copilot review",
    STAGE_SELF_REVIEW: "self review",
    STAGE_CI: "CI remediation",
    STAGE_DESCRIPTION: "description validation",
}
ACTIVE_TASK_STATES = common.ACTIVE_TASK_STATES
RECOVERY_TASK_STATES = common.RECOVERY_TASK_STATES
UNAVAILABLE_STATUS_REASONS = common.UNAVAILABLE_STATUS_REASONS


def run_slug(target: dict[str, Any]) -> str:
    owner = str(target["owner"])
    repo = str(target["repo"])
    for name, value in (("owner", owner), ("repository", repo)):
        if (
            re.fullmatch(r"[A-Za-z0-9_.-]+", value) is None
            or value in {".", ".."}
        ):
            raise WorkflowError(f"invalid {name} path identity")
    return f"{owner}--{repo}--pr-{target['number']}"


def run_root() -> Path:
    return copilot_home() / "run" / RUN_KIND


def run_directory_for(target: dict[str, Any], run_id: str) -> Path:
    return run_root() / run_slug(target) / run_id


def progress_log_path(target: dict[str, Any], run_id: str) -> Path:
    return run_directory_for(target, run_id) / "progress.jsonl"


def launch_state_path(target: dict[str, Any], run_id: str) -> Path:
    return run_directory_for(target, run_id) / "launch.json"


def observer_state_path(target: dict[str, Any], run_id: str) -> Path:
    return run_directory_for(target, run_id) / "observer.json"


def scheduler_log_path(target: dict[str, Any], run_id: str) -> Path:
    return run_directory_for(target, run_id) / "scheduler.log"


def run_result_path(target: dict[str, Any], run_id: str) -> Path:
    return run_directory_for(target, run_id) / "result.json"


def serialized_size(payload: Any) -> int:
    return len((json.dumps(payload, sort_keys=True) + os.linesep).encode("utf-8"))


def bounded_value(value: Any, *, depth: int = 0) -> tuple[Any, bool]:
    """Bound diagnostic previews; the artifact retains their exact values."""
    if isinstance(value, str):
        return (
            value[:TERMINAL_TEXT_LIMIT] + "..."
            if len(value) > TERMINAL_TEXT_LIMIT else value,
            len(value) > TERMINAL_TEXT_LIMIT,
        )
    if not isinstance(value, (dict, list)):
        return value, False
    if depth >= 5:
        return None, bool(value)
    items = list(value.items()) if isinstance(value, dict) else list(enumerate(value))
    limit = 32 if isinstance(value, dict) else TERMINAL_COLLECTION_LIMIT
    truncated = len(items) > limit
    result: Any = {} if isinstance(value, dict) else []
    for key, item in items[:limit]:
        preview, shortened = bounded_value(item, depth=depth + 1)
        truncated |= shortened
        if isinstance(result, dict):
            if len(key) > TERMINAL_TEXT_LIMIT:
                truncated = True
                continue
            result[key] = preview
        else:
            result.append(preview)
    return result, truncated


def compact_stage(stage: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value for key, value in stage.items()
        if key not in {"status", "status_state", "log_path"}
    }
    if stage.get("clear") is not True:
        result["status"] = stage.get("status", {})
        if stage.get("log_path"):
            result["log_path"] = stage["log_path"]
    elif stage.get("status"):
        result["diagnostics_omitted"] = True
    return result


def clean_ci_evidence(payload: dict[str, Any]) -> bool:
    if payload.get("ci_warnings") or payload.get("ci_warning_revalidation_error"):
        return False
    pr = payload.get("pr") or {}
    head, base = pr.get("head_sha"), pr.get("base_sha")
    if not head or not base or payload.get("head_sha") != head:
        return False
    ci = next(
        (stage for stage in payload.get("stages", []) if stage.get("stage") == STAGE_CI),
        {},
    )
    status = ci.get("status") or {}
    run_status = status.get("run") or {}
    coordinator = status.get("coordinator") or {}
    return (
        ci.get("clear") is True
        and ci.get("clearance_kind") == "stage_result"
        and ci.get("clear_at_head_sha") == head
        and ci.get("clear_at_base_sha") == base
        and common.current_ci_clearance_verification(status.get("clearance_verification"))
        and status.get("outcome") == "green"
        and run_status.get("head_sha") == head
        and run_status.get("decision") == "green"
        and run_status.get("reason") == "all_checks_passed"
        and coordinator.get("status") == "ready"
        and coordinator.get("head_sha") == head
        and coordinator.get("base_sha") == base
    )


def compact_terminal_result(
    payload: dict[str, Any], *, result_path: Path, result_sha256: str,
    max_bytes: int = TERMINAL_RESULT_MAX_BYTES,
) -> dict[str, Any]:
    compact = {
        key: payload[key]
        for key in (
            "event", "result", "run_id", "number", "head_sha", "local_head_sha", "sweeps",
        )
        if key in payload
    }
    compact["artifacts"] = {
        "result": str(result_path.resolve()),
        "result_sha256": result_sha256,
    }
    compact["summary_version"] = 1
    pr = payload.get("pr") or {}
    if pr.get("base_sha"):
        compact["base_sha"] = pr["base_sha"]
    if payload.get("all_ci_passed") is False or payload.get("ci_warnings"):
        compact["all_ci_passed"] = False
    elif clean_ci_evidence(payload):
        compact["all_ci_passed"] = True

    sections = {
        key: payload[key] for key in (
            "pr", "stage", "reason", "detail", "error", "ci_warnings",
            "ci_warning_revalidation_error",
        ) if key in payload
    }
    sections["stages"] = [compact_stage(stage) for stage in payload.get("stages", [])]
    if isinstance(payload.get("stage_result"), dict):
        sections["stage_result"] = compact_stage(payload["stage_result"])
    if payload.get("result") != "complete":
        failure = common.stage_failure_summary(payload.get("stage_result"))
        if failure:
            sections["stage_failure"] = failure
    runs = payload.get("runs", [])
    sections["runs"] = [
        {
            key: record[key] for key in (
                "stage", "sweep", "action", "outcome", "clear", "stage_reason",
                "started_head_sha", "ended_head_sha", "history_rewritten",
            ) if key in record
        }
        for record in runs
    ]
    for key in ("published_commits", "retained_commits", "commit_tracking_errors"):
        values = list(payload.get(key) or [])
        for record in runs:
            for value in record.get(key) or []:
                if value not in values:
                    values.append(value)
        sections[key] = values
    if payload.get("history_rewritten") or any(run.get("history_rewritten") for run in runs):
        compact["history_rewritten"] = True
    ci = next(
        (stage for stage in payload.get("stages", []) if stage.get("stage") == STAGE_CI),
        None,
    )
    if ci and ci.get("status"):
        sections["ci_status"] = ci["status"]

    for key, value in sections.items():
        compact[key], truncated = bounded_value(value)
        if truncated:
            compact[f"{key}_details_truncated"] = True
        if isinstance(value, list) and len(value) > len(compact[key]):
            compact[f"{key}_omitted"] = len(value) - len(compact[key])
    compact["diagnostics_omitted"] = bool(runs or payload.get("stages"))

    # Drop previews, not identity or outcome. Counts and flags require artifact retrieval.
    for key in (
        "ci_status", "runs", "stages", "stage_result", "ci_warnings",
        "published_commits", "retained_commits", "commit_tracking_errors", "pr",
        "ci_warning_revalidation_error", "stage_failure", "detail", "error", "reason", "stage",
    ):
        if serialized_size(compact) <= max_bytes:
            break
        if key not in compact:
            continue
        compact.pop(key)
        compact.pop(f"{key}_details_truncated", None)
        value = sections[key]
        compact[f"{key}_omitted"] = len(value) if isinstance(value, list) else True
    if serialized_size(compact) > max_bytes:
        raise WorkflowError("terminal result identity exceeds the output byte limit")
    return compact


def persist_terminal_result(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    path = path.resolve()
    if path.exists():
        if common.read_json(path) != payload:
            raise WorkflowError(f"terminal result already exists with different content: {path}")
    else:
        common.write_json_atomically(path, payload)
    return compact_terminal_result(
        payload, result_path=path, result_sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )


def bounded_watch_result(
    payload: dict[str, Any], *, target: dict[str, Any], run_id: str
) -> dict[str, Any]:
    result = dict(payload)
    updates = payload.get("updates", [])
    if payload.get("finished") and not payload.get("monitor_failure"):
        terminal = next(
            (update.get("final_event") for update in reversed(updates)
             if isinstance(update.get("final_event"), dict)),
            None,
        )
        if terminal is None:
            records = common.read_progress_log(progress_log_path(target, run_id))
            terminal = next(
                (record.get("final_event") for record in reversed(records)
                 if record.get("terminal")), None,
            )
        if not isinstance(terminal, dict):
            raise WorkflowError("terminal progress record has no final_event")
        path = run_result_path(target, run_id).resolve()
        if terminal.get("summary_version") != 1:
            terminal = persist_terminal_result(terminal, path)
        artifacts = terminal.get("artifacts") or {}
        if not paths_match(artifacts.get("result"), path):
            raise WorkflowError("terminal result artifact path does not match the run")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != artifacts.get("result_sha256"):
            raise WorkflowError("terminal result artifact hash does not match the summary")
        original = json.loads(raw)
        if original.get("run_id") != run_id:
            raise WorkflowError("terminal result artifact identity does not match the run")
        envelope = {
            **result, "updates": [], "updates_omitted": len(updates),
            "artifacts": {"progress": str(progress_log_path(target, run_id).resolve())},
        }
        terminal = compact_terminal_result(
            original, result_path=path, result_sha256=artifacts["result_sha256"],
            max_bytes=WATCH_MAX_BYTES - serialized_size(envelope) - 256,
        )
        result["final_event"] = terminal
    result["updates"] = []
    for update in updates[-TERMINAL_COLLECTION_LIMIT:]:
        preview, truncated = bounded_value(
            {key: value for key, value in update.items() if key != "final_event"}
        )
        if truncated:
            preview["details_truncated"] = True
        result["updates"].append(preview)
    result["artifacts"] = {"progress": str(progress_log_path(target, run_id).resolve())}
    # Keep the legacy terminal location when both copies fit the watch budget.
    if result.get("final_event") and result["updates"] and updates[-1].get("terminal"):
        result["updates"][-1]["final_event"] = result["final_event"]
        if serialized_size(result) > WATCH_MAX_BYTES:
            result["updates"][-1].pop("final_event")
    while result["updates"] and serialized_size(result) > WATCH_MAX_BYTES - 64:
        result["updates"].pop(0)
    omitted = len(updates) - len(result["updates"])
    if omitted:
        result["updates_omitted"] = omitted
    if serialized_size(result) > WATCH_MAX_BYTES:
        raise WorkflowError("watch result identity exceeds the output byte limit")
    return result


def monitor_locator_path(run_id: str) -> Path:
    return run_root() / "monitors" / f"{run_id}.json"


def target_identity(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "owner": target["owner"],
        "repo": target["repo"],
        "number": target["number"],
    }


def targets_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        str(left.get("owner") or "").casefold()
        == str(right.get("owner") or "").casefold()
        and str(left.get("repo") or "").casefold()
        == str(right.get("repo") or "").casefold()
        and left.get("number") == right.get("number")
    )


def paths_match(left: Any, right: Path) -> bool:
    if not isinstance(left, str) or not left:
        return False
    try:
        return Path(left).resolve() == right.resolve()
    except OSError:
        return False


def monitor_locator(target: dict[str, Any], run_id: str) -> dict[str, Any]:
    return {
        "schema": MONITOR_SCHEMA,
        "version": MONITOR_VERSION,
        "run_id": run_id,
        "target": target_identity(target),
        "launch_path": str(launch_state_path(target, run_id)),
        "event_log": str(progress_log_path(target, run_id)),
    }


def load_monitor_target(run_id: str) -> dict[str, Any]:
    path = monitor_locator_path(run_id)
    if not path.is_file() or path.is_symlink():
        raise WorkflowError(f"monitor handle does not exist for run {run_id}")
    locator = common.read_json(path)
    if (
        not isinstance(locator, dict)
        or set(locator)
        != {
            "schema",
            "version",
            "run_id",
            "target",
            "launch_path",
            "event_log",
        }
        or locator.get("schema") != MONITOR_SCHEMA
        or locator.get("version") != MONITOR_VERSION
        or locator.get("run_id") != run_id
    ):
        raise WorkflowError(f"monitor handle is malformed for run {run_id}")
    identity = locator.get("target")
    if (
        not isinstance(identity, dict)
        or set(identity) != {"owner", "repo", "number"}
        or not isinstance(identity.get("owner"), str)
        or not identity["owner"]
        or not isinstance(identity.get("repo"), str)
        or not identity["repo"]
        or isinstance(identity.get("number"), bool)
        or not isinstance(identity.get("number"), int)
        or identity["number"] < 1
    ):
        raise WorkflowError(f"monitor handle has invalid target identity for run {run_id}")
    target = build_target(identity["owner"], identity["repo"], identity["number"])
    try:
        run_directory_for(target, run_id).resolve().relative_to(run_root().resolve())
    except ValueError as error:
        raise WorkflowError(
            f"monitor handle target escapes the run directory for run {run_id}"
        ) from error
    if (
        not paths_match(locator.get("launch_path"), launch_state_path(target, run_id))
        or not paths_match(locator.get("event_log"), progress_log_path(target, run_id))
    ):
        raise WorkflowError(f"monitor handle paths are invalid for run {run_id}")
    return target


def validate_launch_record(target: dict[str, Any], run_id: str) -> None:
    path = launch_state_path(target, run_id)
    if not path.is_file() or path.is_symlink():
        raise WorkflowError(f"launch record does not exist for run {run_id}")
    launch = common.read_json(path)
    if (
        not isinstance(launch, dict)
        or launch.get("kind") != RUN_KIND
        or launch.get("run_id") != run_id
        or not isinstance(launch.get("target"), dict)
        or not targets_match(launch["target"], target)
        or not paths_match(launch.get("event_log"), progress_log_path(target, run_id))
    ):
        raise WorkflowError(f"launch record identity is invalid for run {run_id}")


def watch_arguments(
    run_id: str,
    cursor: int,
    *,
    target: dict[str, Any] | None = None,
) -> list[str]:
    arguments = ["watch"]
    if target is not None:
        arguments.append(
            f"{target['owner']}/{target['repo']}#{target['number']}"
        )
    arguments.extend(
        [
            "--run-id",
            run_id,
            "--cursor",
            str(cursor),
            "--wait-seconds",
            str(int(PROGRESS_HEARTBEAT_INTERVAL)),
        ]
    )
    return arguments


def bind_next_watch(
    payload: dict[str, Any],
    *,
    target: dict[str, Any],
    run_id: str,
    legacy_target: bool,
) -> dict[str, Any]:
    bound = {
        **payload,
        "run_id": run_id,
        "target": f"{target['owner']}/{target['repo']}#{target['number']}",
    }
    if not payload.get("finished"):
        bound["next_watch"] = {
            "arguments": watch_arguments(
                run_id,
                int(payload.get("cursor", 0)),
                target=target if legacy_target else None,
            )
        }
    return bound


def progress_transition(payload: dict[str, Any]) -> dict[str, Any] | None:
    event = payload.get("event")
    sweep = payload.get("sweep")
    stage = payload.get("stage")
    label = STAGE_LABELS.get(stage, str(stage or "pipeline"))
    prefix = f"Sweep {sweep}/{MAX_SWEEPS}: " if isinstance(sweep, int) else ""
    target_url = payload.get("target")
    number = payload.get("number")
    scope = f" for #{number}" if isinstance(number, int) else ""
    has_ci_warnings = bool(payload.get("ci_warnings") or payload.get("ci_warnings_omitted"))

    update: dict[str, Any]
    if event == "pipeline_started":
        update = {
            "message": f"PR pipeline starting for {target_url}.",
            "next_action": "Read the live pull request and synchronize its worktree.",
            "waiting": True,
            "wait_reason": "reading pull request state from GitHub",
        }
    elif event == "sweep_started":
        update = {
            "message": f"{prefix}started.",
            "next_action": f"Inspect and run {STAGE_LABELS[STAGE_CONFLICT]}.",
            "waiting": True,
            "wait_reason": "checking the current pull request head and stage markers",
        }
    elif event == "stage_started":
        update = {
            "message": f"{prefix}{label} running{scope}.",
            "next_action": "Wait for the stage agent result.",
            "waiting": True,
            "wait_reason": f"waiting for {label}",
        }
    elif event == "stage_progress":
        phase = payload.get("phase")
        action_checks = payload.get("action_checks") or []
        pending_checks = payload.get("pending_checks") or []
        if phase == "diagnosing":
            message = f"{prefix}{label} diagnosing {len(action_checks)} known failure(s){scope}."
            next_action = "Attribute the known failure from its logs and the pinned diff."
        elif phase == "fixing":
            message = f"{prefix}{label} fixing {len(action_checks)} attributed failure(s){scope}."
            next_action = "Validate, commit, and publish the fix."
        elif phase == "rerunning":
            message = f"{prefix}{label} retrying {len(action_checks)} suspected flake(s){scope}."
            next_action = "Request one safe retry, then inspect its result."
        else:
            message = f"{prefix}{label} monitoring {len(pending_checks)} pending check(s){scope}."
            next_action = "Inspect the next concrete failure as soon as it completes."
        update = {
            "message": message,
            "next_action": next_action,
            "waiting": True,
            "wait_reason": (
                f"{phase} a known CI failure"
                if phase != "waiting"
                else "waiting for remaining CI checks"
            ),
        }
    elif event == "stage_finished":
        clear = payload.get("clear")
        action = payload.get("action")
        if payload.get("returncode") not in (None, 0):
            outcome = f"failed with exit code {payload['returncode']}"
        elif clear and payload.get("clearance_kind") == "ci_warning":
            outcome = "completed WITH CI WARNINGS"
        elif action == "already_clear":
            outcome = "already clear"
        elif clear:
            outcome = "complete"
        else:
            reason = payload.get("stage_reason") or payload.get("outcome")
            outcome = f"not clear: {reason}" if reason else "not clear"
            if reason == "clearance_is_for_an_older_head":
                recorded = payload.get("clear_at_head_sha")
                live = payload.get("ended_head_sha")
                if recorded and live:
                    outcome += f" (recorded {recorded[:8]}, live {live[:8]})"
            elif reason == "clearance_is_for_an_older_base":
                recorded = payload.get("clear_at_base_sha")
                live = payload.get("inspected_base_sha")
                if recorded and live:
                    outcome += f" (recorded {recorded[:8]}, live {live[:8]})"
        update = {
            "message": f"{prefix}{label} {outcome}{scope}.",
            "next_action": "Inspect and run the next stage.",
            "waiting": True,
            "wait_reason": "checking GitHub state before the next stage",
        }
    elif event == "sweep_finished":
        uncleared = payload.get("uncleared_stages") or []
        update = {
            "message": (
                f"{prefix}{'completed' if payload.get('ci_warnings') else 'complete'}"
                + (
                    f"; still uncleared: {', '.join(uncleared)}."
                    if uncleared
                    else " WITH CI WARNINGS; not all CI passed."
                    if payload.get("ci_warnings")
                    else "; all stages are clear."
                )
            ),
            "next_action": (
                "Finish the run."
                if not uncleared
                else "Start another sweep only if the head or base changed."
            ),
            "waiting": False,
        }
    elif event == "pipeline_finished":
        result = payload.get("result", "unknown")
        reported_result = (
            "completed" if result == "complete" and has_ci_warnings else result
        )
        update = {
            "message": (
                f"PR pipeline {reported_result}"
                + (
                    " WITH CI WARNINGS; not all CI passed"
                    if has_ci_warnings
                    else ""
                )
                + (f": {payload.get('reason')}." if payload.get("reason") else ".")
            ),
            "next_action": "Report the final pipeline result.",
            "waiting": False,
            "terminal": True,
            "result": result,
            "final_event": payload,
        }
    else:
        return None

    if payload.get("ci_warnings"):
        update["ci_warnings"] = payload["ci_warnings"]
        update["all_ci_passed"] = False
        if "WITH CI WARNINGS" not in update["message"]:
            update["message"] += " WITH CI WARNINGS; not all CI passed."
    if payload.get("ci_warning_revalidation_error"):
        update["message"] += (
            " CI warning status could not be revalidated: "
            f"{payload['ci_warning_revalidation_error']}."
        )
    update.update(
        {
            "event": PROGRESS_EVENT,
            "kind": "transition",
            "source_event": event,
            "sweep": sweep,
            "iteration": sweep,
            "stage": stage,
            "pull_requests": (
                [payload["number"]] if isinstance(payload.get("number"), int) else []
            ),
        }
    )
    return {key: value for key, value in update.items() if value is not None}


class ProgressReporter(common.ConversationProgressReporter):
    def __init__(
        self,
        *,
        target: dict[str, Any] | None = None,
        result_path: Path | None = None,
        event_log: Path | None = None,
        output: Callable[[dict[str, Any]], None] = emit,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        self.result_path = result_path
        self.target = target

        def transition(payload: dict[str, Any]) -> dict[str, Any] | None:
            if target is not None:
                payload = {**payload, "number": target["number"]}
            return progress_transition(payload)

        super().__init__(
            transition=transition,
            event_log=event_log,
            output=output,
            wall_time=wall_time,
        )

    def __call__(self, payload: dict[str, Any]) -> None:
        if payload.get("event") == "pipeline_finished" and self.result_path is not None:
            if self.target is not None:
                payload = {**payload, "number": self.target["number"]}
            payload = persist_terminal_result(payload, self.result_path)
        super().__call__(payload)


def gh_json(arguments: list[str]) -> Any:
    return common.gh_json(arguments)


def resolve_target(value: str | None, repo_root: Path) -> dict[str, Any]:
    return common.resolve_target(value, repo_root, api=gh_json)


def base_ref_tip(repo_name: str, base_branch: str) -> str:
    return common.base_ref_tip(repo_name, base_branch, api=gh_json)


def read_pull_request(target: dict[str, Any]) -> dict[str, Any]:
    return common.read_pull_request(target, api=gh_json, base_tip=base_ref_tip)


def read_pr_commits(target: dict[str, Any]) -> list[dict[str, Any]]:
    return common.read_pr_commits(target, api=gh_json)


def snapshot_pr_commits(target: dict[str, Any]) -> dict[str, Any]:
    return common.snapshot_pr_commits(target, read=read_pr_commits)


def fetch_pr_head(repo_root: Path, target: dict[str, Any]) -> dict[str, Any]:
    return common.fetch_pr_head(repo_root, target, remote_for=target_remote)


def checkout_fetched_head(repo_root: Path, head_sha: str) -> dict[str, Any]:
    return common.checkout_fetched_head(repo_root, head_sha)


def sync_worktree(
    repo_root: Path,
    target: dict[str, Any],
    pr: dict[str, Any],
    *,
    known_safe_head: str | None,
) -> dict[str, Any]:
    return common.sync_worktree(
        repo_root,
        target,
        pr,
        known_safe_head=known_safe_head,
        fetch=fetch_pr_head,
        checkout=checkout_fetched_head,
    )


def settle_after_stage(
    repo_root: Path,
    target: dict[str, Any],
    *,
    started_head_sha: str,
) -> dict[str, Any]:
    return common.settle_after_stage(
        repo_root,
        target,
        started_head_sha=started_head_sha,
        fetch=fetch_pr_head,
        checkout=checkout_fetched_head,
    )


def read_stage_status(
    entry: dict[str, Any], target: dict[str, Any], run_id: str | None = None
) -> dict[str, Any]:
    return common.read_stage_status(
        entry,
        target,
        state_for=lambda current, selected: stage_state_path(
            current, selected, run_id
        ),
    )


def inspect_stage(
    entry: dict[str, Any],
    target: dict[str, Any],
    head_sha: str,
    base_sha: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    return common.inspect_stage(
        entry,
        target,
        head_sha,
        base_sha,
        pipeline_run=run_id,
        read_status=lambda current, selected: read_stage_status(
            current, selected, run_id
        ),
    )


def inspect_stages(
    target: dict[str, Any], head_sha: str, base_sha: str, run_id: str | None = None
) -> list[dict[str, Any]]:
    return common.inspect_stages(
        target,
        head_sha,
        base_sha,
        inspect=lambda entry, selected, head, base: inspect_stage(
            entry, selected, head, base, run_id
        ),
    )


def inspect_stage_for_run(
    entry: dict[str, Any],
    target: dict[str, Any],
    head_sha: str,
    base_sha: str,
    run_id: str,
) -> dict[str, Any]:
    return inspect_stage(entry, target, head_sha, base_sha, run_id)


def stage_accepts_pipeline_position(entry: dict[str, Any]) -> bool:
    return common.stage_accepts_pipeline_position(entry)


def inspect_stages_for_run(
    target: dict[str, Any], head_sha: str, base_sha: str, run_id: str
) -> list[dict[str, Any]]:
    return inspect_stages(target, head_sha, base_sha, run_id)


def pipeline_arguments(entry: dict[str, Any], run_id: str, sweep: int) -> list[str]:
    return common.pipeline_arguments(
        entry,
        run_id,
        sweep,
        MAX_SWEEPS,
        accepts=stage_accepts_pipeline_position,
    )


def stage_command(
    entry: dict[str, Any],
    target: dict[str, Any],
    *,
    model: str,
    effort: str,
    run_id: str,
    sweep: int,
    conflict_strategy: str = "auto",
    repo_root: Path | None = None,
) -> list[str]:
    arguments = pipeline_arguments(entry, run_id, sweep)
    arguments.extend(
        [
            "--state",
            str(stage_state_path(entry, target, run_id)),
        ]
    )
    if entry["stage"] == STAGE_CONFLICT:
        arguments.extend(
            [
                "--strategy",
                conflict_strategy,
            ]
        )
    return common.stage_command(
        entry,
        target,
        model=model,
        effort=effort,
        arguments=arguments,
        repo_root=repo_root,
    )


def stage_log_path(
    target: dict[str, Any], run_id: str, sweep: int, entry: dict[str, Any]
) -> Path:
    directory = (
        copilot_home()
        / "run"
        / "pr-pipeline"
        / f"{target['owner']}--{target['repo']}--{target['number']}"
        / run_id
    )
    return directory / f"{sweep}-{entry['stage']}.log"


def run_stage(
    entry: dict[str, Any],
    target: dict[str, Any],
    repo_root: Path,
    *,
    model: str,
    effort: str,
    run_id: str,
    sweep: int,
    conflict_strategy: str = "auto",
    report: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    command = stage_command(
        entry,
        target,
        model=model,
        effort=effort,
        run_id=run_id,
        sweep=sweep,
        conflict_strategy=conflict_strategy,
        repo_root=repo_root,
    )
    log_path = stage_log_path(target, run_id, sweep, entry)
    if entry["stage"] != STAGE_CI:
        return common.run_foreground(command, cwd=repo_root, log_path=log_path)

    last_signature: str | None = None

    def progress() -> None:
        nonlocal last_signature
        current = common.stage_live_progress(
            entry,
            target,
            state_for=lambda selected, current_target: stage_state_path(
                selected, current_target, run_id
            ),
        )
        if current is None:
            return
        signature = json.dumps(current, sort_keys=True)
        if signature == last_signature:
            return
        last_signature = signature
        report_event(
            report,
            "stage_progress",
            run_id=run_id,
            stage=entry["stage"],
            sweep=sweep,
            number=target["number"],
            **current,
        )

    return common.run_monitored(
        command,
        cwd=repo_root,
        log_path=log_path,
        progress=progress,
    )


def blocked_result(
    *,
    pr: dict[str, Any],
    run_id: str,
    sweeps: int,
    runs: list[dict[str, Any]],
    reason: str,
    detail: str,
    stage: str | None = None,
    stage_result: dict[str, Any] | None = None,
    local_head_sha: str | None = None,
    retained_commits: list[dict[str, str]] | None = None,
    stages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = {
        "result": "blocked",
        "run_id": run_id,
        "pr": pr,
        "head_sha": pr.get("head_sha"),
        "sweeps": sweeps,
        "runs": runs,
        "stage": stage,
        "reason": reason,
        "detail": detail,
    }
    if stage_result is not None:
        payload["stage_result"] = stage_result
    if local_head_sha is not None:
        payload["local_head_sha"] = local_head_sha
    if retained_commits:
        payload["retained_commits"] = retained_commits
    if stages is not None:
        payload["stages"] = stages
    current_stages = stages
    if current_stages is None:
        last_ci = next(
            (record for record in reversed(runs) if record["stage"] == STAGE_CI),
            None,
        )
        current_stages = (
            [last_ci]
            if last_ci
            and last_ci.get("clear_at_head_sha") == pr.get("head_sha")
            and last_ci.get("clear_at_base_sha") == pr.get("base_sha")
            else []
        )
    if common.ci_warning_fields(current_stages):
        try:
            current_ci = inspect_stage_for_run(
                STAGE_BY_NAME[STAGE_CI],
                common.target_for(pr["repo_name"], pr["number"]),
                pr["head_sha"],
                pr["base_sha"],
                run_id,
            )
            payload.update(common.ci_warning_fields([current_ci]))
            if current_ci.get("reason") in UNAVAILABLE_STATUS_REASONS | {
                "no_state", "ci_warning_not_verified",
            }:
                payload["ci_warning_revalidation_error"] = current_ci["reason"]
        except (WorkflowError, json.JSONDecodeError, OSError) as error:
            payload["ci_warning_revalidation_error"] = str(error)
    return payload


stage_blocker = common.stage_blocker


def run_pipeline(
    target: dict[str, Any],
    repo_root: Path,
    *,
    models: dict[str, str],
    effort: str,
    run_id: str | None = None,
    conflict_strategy: str = "auto",
    report: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    run_id = run_id or uuid.uuid4().hex
    runs: list[dict[str, Any]] = []
    known_safe_head: str | None = None
    completed_sweeps = 0
    completed_conflict_resolution = False
    report_event(report, "pipeline_started", run_id=run_id, target=target["pr_url"])

    for sweep in range(1, MAX_SWEEPS + 1):
        pr = read_pull_request(target)
        if pr["state"] != "OPEN":
            return blocked_result(
                pr=pr,
                run_id=run_id,
                sweeps=completed_sweeps,
                runs=runs,
                reason="pr_not_open",
                detail=f"the pull request is {pr['state']}",
            )
        synced = sync_worktree(
            repo_root,
            target,
            pr,
            known_safe_head=known_safe_head,
        )
        if synced["result"] != "ready":
            return blocked_result(
                pr=pr,
                run_id=run_id,
                sweeps=completed_sweeps,
                runs=runs,
                reason=synced["reason"],
                detail=synced["detail"],
            )
        known_safe_head = synced["head_sha"]
        sweep_started_head = known_safe_head
        sweep_started_base = pr["base_sha"]
        head_changed = False
        base_changed = False
        report_event(
            report,
            "sweep_started",
            run_id=run_id,
            sweep=sweep,
            head_sha=sweep_started_head,
        )

        for entry in STAGES:
            pr = read_pull_request(target)
            if pr["state"] != "OPEN":
                return blocked_result(
                    pr=pr,
                    run_id=run_id,
                    sweeps=completed_sweeps,
                    runs=runs,
                    stage=entry["stage"],
                    reason="pr_not_open",
                    detail=f"the pull request is {pr['state']}",
                )
            synced = sync_worktree(
                repo_root,
                target,
                pr,
                known_safe_head=known_safe_head,
            )
            if synced["result"] != "ready":
                return blocked_result(
                    pr=pr,
                    run_id=run_id,
                    sweeps=completed_sweeps,
                    runs=runs,
                    stage=entry["stage"],
                    reason=synced["reason"],
                    detail=synced["detail"],
                )
            current_head = synced["head_sha"]
            head_changed = head_changed or current_head != known_safe_head
            base_changed = base_changed or pr["base_sha"] != sweep_started_base
            known_safe_head = current_head

            before = inspect_stage_for_run(
                entry, target, current_head, pr["base_sha"], run_id
            )
            before_attempt_id = (
                ((before.get("status") or {}).get("attempt") or {}).get("id")
                if entry["stage"] == STAGE_CONFLICT
                else None
            )
            if before["clear"]:
                record = {
                    "stage": entry["stage"],
                    "sweep": sweep,
                    "action": "already_clear",
                    "started_head_sha": current_head,
                    "ended_head_sha": current_head,
                    "outcome": before["outcome"],
                    "clear": True,
                    "stage_reason": before["reason"],
                    "status": before["status"],
                    "clearance_kind": before.get("clearance_kind"),
                    "clear_at_head_sha": before.get("clear_at_head_sha"),
                    "clear_at_base_sha": before.get("clear_at_base_sha"),
                    **common.ci_warning_fields([before]),
                    "published_commits": [],
                }
                runs.append(record)
                report_event(report, "stage_finished", run_id=run_id, **record)
                continue
            blocker = stage_blocker(
                before,
                after_launch=False,
                conflict_strategy=conflict_strategy,
            )
            if blocker is not None:
                reason, detail = blocker
                return blocked_result(
                    pr=pr,
                    run_id=run_id,
                    sweeps=completed_sweeps,
                    runs=runs,
                    stage=entry["stage"],
                    reason=reason,
                    detail=detail,
                    stage_result=before,
                )
            if entry["stage"] == STAGE_CONFLICT and completed_conflict_resolution:
                record = {
                    "stage": entry["stage"],
                    "sweep": sweep,
                    "action": "completed_this_run",
                    "started_head_sha": current_head,
                    "ended_head_sha": current_head,
                    "outcome": before["outcome"],
                    "clear": False,
                    "stage_reason": before["reason"],
                    "status": before["status"],
                    "published_commits": [],
                }
                runs.append(record)
                report_event(report, "stage_finished", run_id=run_id, **record)
                continue
            if not before["installed"]:
                record = {
                    "stage": entry["stage"],
                    "sweep": sweep,
                    "action": "plugin_not_installed",
                    "started_head_sha": current_head,
                    "ended_head_sha": current_head,
                    "outcome": None,
                    "clear": False,
                    "stage_reason": before["reason"],
                    "status": before["status"],
                    "published_commits": [],
                }
                runs.append(record)
                report_event(report, "stage_finished", run_id=run_id, **record)
                continue

            report_event(
                report,
                "stage_started",
                run_id=run_id,
                stage=entry["stage"],
                sweep=sweep,
                head_sha=current_head,
                started_at=utc_now(),
            )
            commits_before = snapshot_pr_commits(target)
            launched = run_stage(
                entry,
                target,
                repo_root,
                model=models[entry["stage"]],
                effort=effort,
                run_id=run_id,
                sweep=sweep,
                conflict_strategy=conflict_strategy,
                report=report,
            )
            settled = settle_after_stage(
                repo_root,
                target,
                started_head_sha=current_head,
            )
            commits_after = snapshot_pr_commits(target)
            published_commits, commit_tracking_errors, history_rewritten = commits_added(
                commits_before, commits_after
            )
            record = {
                "stage": entry["stage"],
                "sweep": sweep,
                "action": "launched",
                "model": models[entry["stage"]],
                "started_head_sha": current_head,
                "published_commits": published_commits,
                **launched,
            }
            if commit_tracking_errors:
                record["commit_tracking_errors"] = commit_tracking_errors
            if history_rewritten:
                record["history_rewritten"] = True
            if settled["result"] != "ready":
                local_head = settled.get("local_head_sha") or git_or_none(
                    repo_root, "rev-parse", "HEAD"
                )
                pr_head = settled.get("pr_head_sha") or current_head
                current_pr = read_pull_request(target)
                stages = inspect_stages_for_run(
                    target, pr_head, current_pr["base_sha"], run_id
                )
                stage_result = next(
                    result for result in stages if result["stage"] == entry["stage"]
                )
                published_shas = {commit["sha"] for commit in published_commits}
                retained_commits = [
                    commit
                    for commit in local_commits_between(
                        repo_root, current_head, local_head
                    )
                    if commit["sha"] not in published_shas
                ]
                record.update(
                    {
                        "ended_head_sha": local_head,
                        "outcome": stage_result["outcome"],
                        "clear": stage_result["clear"],
                        "stage_reason": stage_result["reason"],
                        "clear_at_head_sha": stage_result.get("clear_at_head_sha"),
                        "clear_at_base_sha": stage_result.get("clear_at_base_sha"),
                        "inspected_base_sha": current_pr["base_sha"],
                        "status": stage_result["status"],
                        "clearance_kind": stage_result.get("clearance_kind"),
                        **common.ci_warning_fields([stage_result]),
                        "retained_commits": retained_commits,
                    }
                )
                runs.append(record)
                report_event(report, "stage_finished", run_id=run_id, **record)
                return blocked_result(
                    pr=read_pull_request(target),
                    run_id=run_id,
                    sweeps=completed_sweeps,
                    runs=runs,
                    stage=entry["stage"],
                    reason=settled["reason"],
                    detail=settled["detail"],
                    stage_result=stage_result,
                    local_head_sha=local_head,
                    retained_commits=retained_commits,
                    stages=stages,
                )

            ended_head = settled["head_sha"]
            known_safe_head = ended_head
            head_changed = head_changed or ended_head != current_head
            current_pr = read_pull_request(target)
            after = inspect_stage_for_run(
                entry,
                target,
                ended_head,
                current_pr["base_sha"],
                run_id,
            )
            record.update(
                {
                    "ended_head_sha": ended_head,
                    "outcome": after["outcome"],
                    "clear": after["clear"],
                    "stage_reason": after["reason"],
                    "clear_at_head_sha": after.get("clear_at_head_sha"),
                    "clear_at_base_sha": after.get("clear_at_base_sha"),
                    "inspected_base_sha": current_pr["base_sha"],
                    "status": after["status"],
                    "clearance_kind": after.get("clearance_kind"),
                    **common.ci_warning_fields([after]),
                }
            )
            runs.append(record)
            report_event(report, "stage_finished", run_id=run_id, **record)
            if launched.get("returncode") != 0:
                return blocked_result(
                    pr=current_pr,
                    run_id=run_id,
                    sweeps=completed_sweeps,
                    runs=runs,
                    stage=entry["stage"],
                    reason="stage_execution_failed",
                    detail=(
                        f"{entry['stage']} exited with code "
                        f"{launched.get('returncode')}; see {launched.get('log_path')}"
                    ),
                    stage_result=after,
                )
            blocker = stage_blocker(
                after,
                after_launch=True,
                conflict_strategy=conflict_strategy,
            )
            if blocker is not None:
                reason, detail = blocker
                return blocked_result(
                    pr=current_pr,
                    run_id=run_id,
                    sweeps=completed_sweeps,
                    runs=runs,
                    stage=entry["stage"],
                    reason=reason,
                    detail=detail,
                    stage_result=after,
                )
            if entry["stage"] == STAGE_CONFLICT and after["outcome"] == "completed":
                after_attempt_id = (
                    ((after.get("status") or {}).get("attempt") or {}).get("id")
                )
                completed_conflict_resolution = (
                    bool(after_attempt_id) and after_attempt_id != before_attempt_id
                )

        completed_sweeps = sweep
        pr = read_pull_request(target)
        synced = sync_worktree(
            repo_root,
            target,
            pr,
            known_safe_head=known_safe_head,
        )
        if synced["result"] != "ready":
            return blocked_result(
                pr=pr,
                run_id=run_id,
                sweeps=completed_sweeps,
                runs=runs,
                reason=synced["reason"],
                detail=synced["detail"],
            )
        final_head = synced["head_sha"]
        known_safe_head = final_head
        head_changed = head_changed or final_head != sweep_started_head
        base_changed = base_changed or pr["base_sha"] != sweep_started_base
        stages = inspect_stages_for_run(target, final_head, pr["base_sha"], run_id)
        report_event(
            report,
            "sweep_finished",
            run_id=run_id,
            sweep=sweep,
            head_sha=final_head,
            head_changed=head_changed,
            base_sha=pr["base_sha"],
            base_changed=base_changed,
            uncleared_stages=[
                stage["stage"] for stage in stages if not stage["clear"]
            ],
            **common.ci_warning_fields(stages),
        )
        if all(stage["clear"] for stage in stages):
            return {
                "result": "complete",
                "run_id": run_id,
                "pr": pr,
                "head_sha": final_head,
                "sweeps": completed_sweeps,
                "stages": stages,
                "runs": runs,
                **common.ci_warning_fields(stages),
            }
        if sweep == MAX_SWEEPS:
            return {
                "result": "incomplete",
                "reason": "two_sweeps_finished",
                "run_id": run_id,
                "pr": pr,
                "head_sha": final_head,
                "sweeps": completed_sweeps,
                "stages": stages,
                "runs": runs,
                **common.ci_warning_fields(stages),
            }
        if not head_changed and not base_changed:
            return {
                "result": "incomplete",
                "reason": "stages_not_clear",
                "run_id": run_id,
                "pr": pr,
                "head_sha": final_head,
                "sweeps": completed_sweeps,
                "stages": stages,
                "runs": runs,
                **common.ci_warning_fields(stages),
            }

    raise WorkflowError("the pipeline ended without a result")


def scheduler_command(
    args: argparse.Namespace,
    target: dict[str, Any],
    run_id: str,
    event_log: Path,
) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
        f"{target['owner']}/{target['repo']}#{target['number']}",
        "--run-id",
        run_id,
        "--event-log",
        str(event_log),
        "--effort",
        args.effort,
        "--conflict-strategy",
        args.conflict_strategy,
        "--github-mutation-policy",
        args.github_mutation_policy,
    ]
    for override in args.stage_model or []:
        command.extend(["--stage-model", override])
    return command


def command_start(args: argparse.Namespace) -> None:
    stage_models(args.stage_model, args.effort)
    repo_root = resolve_repo_root()
    target = resolve_target(args.target, repo_root)
    run_id = uuid.uuid4().hex
    event_log = progress_log_path(target, run_id)
    launch_path = launch_state_path(target, run_id)
    locator_path = monitor_locator_path(run_id)
    if launch_path.exists() or locator_path.exists():
        raise WorkflowError(f"run identity already exists: {run_id}")
    started_at_epoch = time.time()
    started_at = utc_now()
    launch = {
        "kind": RUN_KIND,
        "run_id": run_id,
        "target": target,
        "pid": None,
        "event_log": str(event_log),
        "started_at": started_at,
        "started_at_epoch": started_at_epoch,
        "conflict_strategy": args.conflict_strategy,
        "github_mutation_policy": args.github_mutation_policy,
    }
    common.write_json_atomically(launch_path, launch)
    try:
        process = common.start_detached(
            scheduler_command(args, target, run_id, event_log),
            cwd=repo_root,
            log_path=scheduler_log_path(target, run_id),
        )
    except common.LaunchError as error:
        common.write_json_atomically(
            launch_path, {**launch, **error.launch_receipt, "error": str(error), "status": "launch_failed"}
        )
        raise
    try:
        common.write_json_atomically(
            launch_path, {**launch, "pid": process.pid, **process.launch_receipt}
        )
        common.write_json_atomically(locator_path, monitor_locator(target, run_id))
    except OSError:
        process.terminate()
        raise
    emit(
        {
            "event": "pipeline_launched",
            "run_id": run_id,
            "target": f"{target['owner']}/{target['repo']}#{target['number']}",
            "pid": process.pid,
            **process.launch_receipt,
            "cursor": 0,
            "next_watch": {
                "arguments": watch_arguments(
                    run_id,
                    0,
                )
            },
            "conflict_strategy": args.conflict_strategy,
            "github_mutation_policy": args.github_mutation_policy,
        }
    )


def command_watch(args: argparse.Namespace) -> None:
    if args.run_id is None:
        raise WorkflowError("watch requires --run-id")
    run_id = common.validate_run_id(args.run_id)
    locator_path = monitor_locator_path(run_id)
    legacy_target = not locator_path.exists()
    if locator_path.exists():
        target = load_monitor_target(run_id)
        if args.target is not None and not targets_match(
            parse_target(args.target), target
        ):
            raise WorkflowError(
                f"watch target does not match monitor handle for run {run_id}"
            )
    elif args.target is not None:
        target = parse_target(args.target)
    else:
        raise WorkflowError(f"monitor handle does not exist for run {run_id}")
    validate_launch_record(target, run_id)
    payload = common.watch_progress(
        event_log=progress_log_path(target, run_id),
        launch_path=launch_state_path(target, run_id),
        observer_path=observer_state_path(target, run_id),
        cursor=args.cursor,
        wait_seconds=args.wait_seconds,
    )
    bound = bind_next_watch(
        payload, target=target, run_id=run_id, legacy_target=legacy_target,
    )
    emit(bounded_watch_result(bound, target=target, run_id=run_id))


def command_run(args: argparse.Namespace) -> None:
    common.ACTIVE_GITHUB_MUTATION_POLICY = args.github_mutation_policy
    args.run_id = common.validate_run_id(args.run_id) if args.run_id else uuid.uuid4().hex
    require_tools()
    repo_root = resolve_repo_root()
    target = resolve_target(args.target, repo_root)
    event_log = Path(args.event_log).resolve() if args.event_log else None
    args.result_path = run_result_path(target, args.run_id)
    reporter = ProgressReporter(
        target=target, event_log=event_log, result_path=args.result_path
    )
    options = {
        "models": stage_models(args.stage_model, args.effort),
        "effort": args.effort,
        "conflict_strategy": args.conflict_strategy,
        "report": reporter,
        "run_id": args.run_id,
    }
    result = run_pipeline(target, repo_root, **options)
    reporter({"event": "pipeline_finished", **result})


def report_run_error(args: argparse.Namespace, error: str) -> None:
    run_id = getattr(args, "run_id", None)
    if not isinstance(run_id, str) or not common.RUN_ID_PATTERN.fullmatch(run_id):
        run_id = uuid.uuid4().hex
    event = {
        "event": "pipeline_finished", "result": "error", "error": error, "run_id": run_id,
    }
    event_log = Path(args.event_log).resolve() if args.event_log else None
    result_path = getattr(args, "result_path", None) or (
        event_log.with_name("result.json") if event_log
        else run_root() / "errors" / run_id / "result.json"
    )
    try:
        ProgressReporter(event_log=event_log, result_path=result_path)(event)
    except (WorkflowError, OSError, ValueError) as reporting_error:
        emit({
            "event": "pipeline_reporting_failed", "run_id": run_id,
            "error": str(reporting_error), "pipeline_error": error,
        })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_command = subparsers.add_parser(
        "run", help="run up to two foreground sweeps over the five stages"
    )
    run_command.add_argument(
        "target",
        nargs="?",
        help=(
            "PR URL, owner/repo#number, or a bare number when the repository is "
            "known; omit only from a branch attached to the pull request"
        ),
    )
    run_command.add_argument(
        "--stage-model",
        action="append",
        help="pin one stage's model as <stage>=<model>; repeatable",
    )
    run_command.add_argument("--effort", default=DEFAULT_EFFORT)
    run_command.add_argument(
        "--conflict-strategy",
        choices=common.CONFLICT_STRATEGIES,
        default="auto",
    )
    run_command.add_argument(
        "--github-mutation-policy",
        choices=("allow", "source-only"),
        default="allow",
    )
    run_command.add_argument("--run-id", help=argparse.SUPPRESS)
    run_command.add_argument("--event-log", help=argparse.SUPPRESS)
    run_command.set_defaults(function=command_run)

    start = subparsers.add_parser(
        "start", help="launch the scheduler and return a durable monitor handle"
    )
    start.add_argument(
        "target",
        nargs="?",
        help=(
            "PR URL, owner/repo#number, or a bare number when the repository is "
            "known; omit only from a branch attached to the pull request"
        ),
    )
    start.add_argument(
        "--stage-model",
        action="append",
        help="pin one stage's model as <stage>=<model>; repeatable",
    )
    start.add_argument("--effort", default=DEFAULT_EFFORT)
    start.add_argument(
        "--conflict-strategy",
        choices=common.CONFLICT_STRATEGIES,
        default="auto",
    )
    start.add_argument(
        "--github-mutation-policy",
        choices=("allow", "source-only"),
        default="allow",
    )
    start.set_defaults(function=command_start)

    watch = subparsers.add_parser(
        "watch", help="wait for progress or one five-minute heartbeat"
    )
    watch.add_argument(
        "target",
        nargs="?",
        help="legacy exact owner/repo#number for runs created before monitor handles",
    )
    watch.add_argument("--run-id")
    watch.add_argument("--cursor", type=int, default=0)
    watch.add_argument(
        "--wait-seconds",
        type=float,
        default=PROGRESS_HEARTBEAT_INTERVAL,
    )
    watch.set_defaults(function=command_watch)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.function(args)
        return 0
    except (WorkflowError, json.JSONDecodeError, OSError) as error:
        if args.command == "watch":
            emit(
                {
                    "event": PROGRESS_UPDATE_EVENT,
                    "updates": [],
                    "finished": True,
                    "monitor_failure": str(error),
                    "run_id": getattr(args, "run_id", None),
                    "cursor": getattr(args, "cursor", 0),
                }
            )
            return 1
        if args.command == "start":
            emit({"event": "pipeline_launch_failed", "error": str(error)})
            return 1
        report_run_error(args, str(error))
        return 1
    except KeyboardInterrupt:
        if args.command == "watch":
            emit(
                {
                    "event": PROGRESS_UPDATE_EVENT,
                    "updates": [],
                    "finished": True,
                    "monitor_failure": "interrupted",
                    "run_id": getattr(args, "run_id", None),
                    "cursor": getattr(args, "cursor", 0),
                }
            )
            return 130
        if args.command == "start":
            emit({"event": "pipeline_launch_failed", "error": "interrupted"})
            return 130
        report_run_error(args, "interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
