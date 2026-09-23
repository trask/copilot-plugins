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
import subprocess
import time
import uuid
from types import ModuleType
from typing import Any, Callable


COMMON_MODULE_NAME = "pr_pipeline_common"
COMMON_PATH = Path(__file__).resolve().parent / "pipeline_common.py"
COMMON_SHA256 = "6ac20fb9203fd3dc761fc24618c4e45280ea9bcb2faf2b75af6ead1f2f2310b7"


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
STAGE_HEARTBEAT_SECONDS = 60.0
STEP_DEADLINE_SECONDS = 90.0
CI_SNAPSHOT_CHANGED_REASONS = {
    "clearance_verification": "ci_snapshot_changed",
    "warning_verification": "ci_warning_snapshot_changed",
}
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
PROGRESS_EVENT = common.PROGRESS_EVENT
TERMINAL_RESULT_MAX_BYTES = 8192
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


def run_result_path(target: dict[str, Any], run_id: str) -> Path:
    return run_directory_for(target, run_id) / "result.json"


def run_state_path(target: dict[str, Any], run_id: str) -> Path:
    return run_directory_for(target, run_id) / "state.json"


def require_session_id() -> str:
    session_id = os.environ.get("COPILOT_AGENT_SESSION_ID")
    if not session_id or re.fullmatch(r"[A-Za-z0-9_-]+", session_id) is None:
        raise WorkflowError("bounded PR Pipeline requires a Copilot agent session")
    return session_id


def execution_identity() -> dict[str, str] | None:
    execution = common._EXECUTION
    if execution is None:
        return None
    return {"root": str(execution.root), "run_id": execution.run_id}


def require_finished_step(state: dict[str, Any]) -> None:
    identity = state.get("previous_execution")
    if common._EXECUTION is None:
        return
    if (
        not isinstance(identity, dict)
        or not isinstance(identity.get("root"), str)
        or not isinstance(identity.get("run_id"), str)
    ):
        raise WorkflowError("previous pipeline step has no execution identity")
    runtime = sys.modules.get("_trask_foreground_execution")
    if runtime is None:
        raise WorkflowError("previous pipeline step cannot be verified")
    try:
        prior = runtime.status(Path(identity["root"]))
    except (OSError, ValueError, RuntimeError) as error:
        raise WorkflowError(
            f"previous pipeline step could not be verified: {error}"
        ) from error
    workflow = prior.get("workflow_result")
    if (
        prior.get("run_id") != identity["run_id"]
        or prior.get("terminal") is not True
        or prior.get("exit_code") != 0
        or prior.get("local_status") != "finished"
        or prior.get("local_children_drained") is not True
        or not isinstance(workflow, dict)
        or workflow.get("result") not in {"continue", "waiting"}
    ):
        raise WorkflowError("previous pipeline step did not finish safely")


def save_run_state(path: Path, state: dict[str, Any]) -> None:
    if common._EXECUTION is not None:
        common._EXECUTION.record_state(path, state)
    common.write_json_atomically(path, state)


def load_run_state(path: Path, *, session_id: str) -> dict[str, Any]:
    state = common.read_json(path)
    if (
        not isinstance(state, dict)
        or state.get("schema") != 1
        or state.get("session_id") != session_id
        or state.get("status") not in {"active", "waiting", "blocked", "complete"}
        or not isinstance(state.get("cursor"), dict)
    ):
        raise WorkflowError("bounded PR Pipeline state is missing or belongs to another session")
    return state


def serialized_size(payload: Any) -> int:
    return common.serialized_size(payload)


def bounded_value(value: Any, *, depth: int = 0) -> tuple[Any, bool]:
    return common.bounded_value(
        value,
        text_limit=TERMINAL_TEXT_LIMIT,
        collection_limit=TERMINAL_COLLECTION_LIMIT,
        depth=depth,
    )


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
            "event", "result", "run_id", "number", "head_sha", "local_head_sha",
            "sweeps", "session_title",
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
                "source_drift",
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
    canonical, result_sha256 = common.canonical_terminal_payload(payload, path)
    return compact_terminal_result(
        canonical, result_path=path, result_sha256=result_sha256
    )


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
    elif event in {"stage_progress", "stage_heartbeat"}:
        phase = payload.get("phase")
        action_checks = payload.get("action_checks") or []
        pending_checks = payload.get("pending_checks") or []
        if phase == "hosted_task":
            task_state = str(
                payload.get("hosted_task_state") or "active"
            ).replace("_", " ")
            message = (
                f"{prefix}{label} hosted task {task_state}{scope}."
            )
            next_action = "Wait for the hosted task state to advance."
        elif stage == STAGE_CI and phase == "diagnosing":
            message = (
                f"{prefix}{label} diagnosing {len(action_checks)} "
                f"known failure(s){scope}."
            )
            next_action = (
                "Attribute the known failure from its logs and the pinned diff."
            )
        elif stage == STAGE_CI and phase == "fixing":
            message = (
                f"{prefix}{label} fixing {len(action_checks)} "
                f"attributed failure(s){scope}."
            )
            next_action = "Validate, commit, and publish the fix."
        elif stage == STAGE_CI and phase == "rerunning":
            message = (
                f"{prefix}{label} retrying {len(action_checks)} "
                f"suspected flake(s){scope}."
            )
            next_action = "Request one safe retry, then inspect its result."
        elif stage == STAGE_CI:
            message = (
                f"{prefix}{label} monitoring {len(pending_checks)} "
                f"pending check(s){scope}."
            )
            next_action = (
                "Inspect the next concrete failure as soon as it completes."
            )
        else:
            phase_label = (
                str(phase).replace("_", " ")
                if isinstance(phase, str) and phase
                else "running"
            )
            message = f"{prefix}{label} {phase_label}{scope}."
            next_action = "Wait for the stage agent result."
        elapsed = payload.get("elapsed_seconds")
        if event == "stage_heartbeat" and isinstance(elapsed, int):
            minutes, seconds = divmod(max(0, elapsed), 60)
            duration = (
                f"{minutes}m {seconds}s"
                if minutes
                else f"{seconds}s"
            )
            message = f"{message[:-1]} ({duration} elapsed)."
        update = {
            "message": message,
            "next_action": next_action,
            "waiting": True,
            "wait_reason": (
                (
                    f"{phase} a known CI failure"
                    if phase != "waiting"
                    else "waiting for remaining CI checks"
                )
                if stage == STAGE_CI
                else f"waiting for {label}"
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
                else (
                    "Start another sweep only after a revision change or final "
                    "CI snapshot drift."
                )
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


class ProgressReporter(common.ForegroundProgressReporter):
    def __init__(
        self,
        *,
        target: dict[str, Any] | None = None,
        result_path: Path | None = None,
        output: Callable[[dict[str, Any]], None] = emit,
    ) -> None:
        self.result_path = result_path
        self.target = target
        super().__init__(output=output)

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
    bounded: bool = False,
) -> list[str]:
    arguments = pipeline_arguments(entry, run_id, sweep)
    if bounded:
        arguments.append("--bounded-step")
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
    bounded: bool = False,
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
        bounded=bounded,
    )
    log_path = stage_log_path(target, run_id, sweep, entry)
    last_signature: str | None = None
    last_reported_at = time.monotonic()
    started_at = last_reported_at
    started_wall = time.time()

    def progress() -> None:
        nonlocal last_reported_at, last_signature
        now = time.monotonic()
        if bounded and now - started_at >= STEP_DEADLINE_SECONDS:
            raise WorkflowError(
                f"{entry['stage']} exceeded the bounded step deadline"
            )
        current = common.stage_live_progress(
            entry,
            target,
            state_for=lambda selected, current_target: stage_state_path(
                selected, current_target, run_id
            ),
        )
        hosted = common.hosted_task_progress(observed_after=started_wall)
        if hosted is not None:
            current = hosted
        signature = (
            json.dumps(current, sort_keys=True)
            if current is not None
            else None
        )
        heartbeat = now - last_reported_at >= STAGE_HEARTBEAT_SECONDS
        if signature == last_signature and not heartbeat:
            return
        if signature != last_signature:
            event = "stage_progress"
            last_signature = signature
        else:
            event = "stage_heartbeat"
        last_reported_at = now
        report_event(
            report,
            event,
            run_id=run_id,
            stage=entry["stage"],
            sweep=sweep,
            number=target["number"],
            elapsed_seconds=int(now - started_at),
            **{
                key: value for key, value in (current or {"phase": "running"}).items()
                if key != "elapsed_seconds"
            },
        )

    result = common.run_monitored(
        command,
        cwd=repo_root,
        log_path=log_path,
        progress=progress,
    )
    if bounded and result.get("returncode") == 0:
        terminal = result.get("child_terminal_result")
        workflow = terminal.get("workflow_result") if isinstance(terminal, dict) else None
        if not isinstance(workflow, dict):
            raise WorkflowError("bounded stage did not seal a structured result")
        if workflow.get("result") == "waiting":
            result["waiting"] = True
            result["wait_seconds"] = workflow.get("wait_seconds", 30)
        elif not isinstance(workflow.get("result"), str) or not workflow["result"]:
            raise WorkflowError("bounded stage returned no structured result")
    return result


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


def requires_ci_revalidation_sweep(stages: list[dict[str, Any]]) -> bool:
    uncleared = [stage for stage in stages if stage.get("clear") is not True]
    if len(uncleared) != 1 or uncleared[0].get("stage") != STAGE_CI:
        return False
    ci_stage = uncleared[0]
    if ci_stage.get("installed") is not True or stage_blocker(
        ci_stage, after_launch=False
    ) is not None:
        return False
    status = ci_stage.get("status")
    if not isinstance(status, dict):
        return False
    verifications = {
        "clearance_verification": status.get("clearance_verification"),
        "warning_verification": ci_stage.get("warning_verification"),
    }
    for field, reason in CI_SNAPSHOT_CHANGED_REASONS.items():
        verification = verifications[field]
        if not isinstance(verification, dict):
            continue
        expected = verification.get("expected_snapshot_sha256")
        observed = verification.get("observed_snapshot_sha256")
        if (
            verification.get("result") == "stale"
            and verification.get("reason") == reason
            and isinstance(expected, str)
            and re.fullmatch(r"[0-9a-f]{64}", expected) is not None
            and isinstance(observed, str)
            and re.fullmatch(r"[0-9a-f]{64}", observed) is not None
            and observed != expected
        ):
            return True
    return False


def run_pipeline(
    target: dict[str, Any],
    repo_root: Path,
    *,
    models: dict[str, str],
    effort: str,
    run_id: str | None = None,
    conflict_strategy: str = "auto",
    report: Callable[[dict[str, Any]], None] | None = None,
    cursor: dict[str, Any] | None = None,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    run_id = run_id or uuid.uuid4().hex
    bounded = checkpoint is not None
    runs: list[dict[str, Any]] = list(cursor.get("runs", [])) if cursor else []
    known_safe_head: str | None = cursor.get("known_safe_head") if cursor else None
    completed_sweeps = cursor.get("completed_sweeps", 0) if cursor else 0
    completed_conflict_resolution = (
        cursor.get("completed_conflict_resolution", False) if cursor else False
    )
    start_sweep = cursor.get("sweep", 1) if cursor else 1
    start_stage_index = cursor.get("stage_index", 0) if cursor else 0
    pending_stage = cursor.get("pending_stage") if cursor else None

    def save_cursor(
        *, sweep: int, stage_index: int, initialized: bool,
        sweep_started_head: str | None = None,
        sweep_started_base: str | None = None,
        head_changed: bool = False, base_changed: bool = False,
        pending: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = {
            "sweep": sweep, "stage_index": stage_index, "initialized": initialized,
            "sweep_started_head": sweep_started_head,
            "sweep_started_base": sweep_started_base,
            "head_changed": head_changed, "base_changed": base_changed,
            "known_safe_head": known_safe_head,
            "completed_sweeps": completed_sweeps,
            "completed_conflict_resolution": completed_conflict_resolution,
            "pending_stage": pending,
            "runs": runs,
        }
        if checkpoint is not None:
            checkpoint(current)
        return current

    def more_work(
        *, sweep: int, stage_index: int, initialized: bool,
        sweep_started_head: str, sweep_started_base: str,
        head_changed: bool, base_changed: bool,
        pending: dict[str, Any] | None = None,
        wait_seconds: int | float | None = None,
    ) -> dict[str, Any]:
        current = save_cursor(
            sweep=sweep, stage_index=stage_index, initialized=initialized,
            sweep_started_head=sweep_started_head,
            sweep_started_base=sweep_started_base,
            head_changed=head_changed, base_changed=base_changed,
            pending=pending,
        )
        outcome = {
            "result": "waiting" if pending is not None else "continue",
            "run_id": run_id, "sweep": sweep, "stage": STAGES[stage_index]["stage"]
            if stage_index < len(STAGES) else None,
            "head_sha": known_safe_head, "cursor": current,
        }
        if pending is not None:
            outcome["wait_seconds"] = (
                min(60, max(1, int(wait_seconds)))
                if isinstance(wait_seconds, (int, float)) and wait_seconds > 0
                else 30
            )
        return outcome

    if cursor is None:
        report_event(report, "pipeline_started", run_id=run_id, target=target["pr_url"])

    for sweep in range(start_sweep, MAX_SWEEPS + 1):
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
        previous_safe_head = known_safe_head
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
        continuing_sweep = bool(
            bounded and cursor is not None and sweep == start_sweep
            and cursor.get("initialized")
        )
        if continuing_sweep:
            sweep_started_head = cursor["sweep_started_head"]
            sweep_started_base = cursor["sweep_started_base"]
            head_changed = (
                cursor["head_changed"]
                or previous_safe_head is not None
                and known_safe_head != previous_safe_head
            )
            base_changed = cursor["base_changed"]
        else:
            sweep_started_head = known_safe_head
            sweep_started_base = pr["base_sha"]
            head_changed = False
            base_changed = False
            report_event(
                report, "sweep_started", run_id=run_id,
                sweep=sweep, head_sha=sweep_started_head,
            )

        for stage_index, entry in enumerate(STAGES):
            if continuing_sweep and stage_index < start_stage_index:
                continue
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
            resuming_stage = (
                bounded and pending_stage is not None
                and pending_stage.get("sweep") == sweep
                and pending_stage.get("stage_index") == stage_index
            )
            if pending_stage is not None and not resuming_stage:
                raise WorkflowError("bounded pipeline cursor does not match the pending stage")
            if before["clear"] and not resuming_stage:
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
                if bounded:
                    return more_work(
                        sweep=sweep, stage_index=stage_index + 1,
                        initialized=True, sweep_started_head=sweep_started_head,
                        sweep_started_base=sweep_started_base,
                        head_changed=head_changed, base_changed=base_changed,
                    )
                continue
            blocker = (
                None if resuming_stage else stage_blocker(
                    before,
                    after_launch=False,
                    conflict_strategy=conflict_strategy,
                )
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
            if (
                entry["stage"] == STAGE_CONFLICT
                and completed_conflict_resolution and not resuming_stage
            ):
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
                if bounded:
                    return more_work(
                        sweep=sweep, stage_index=stage_index + 1,
                        initialized=True, sweep_started_head=sweep_started_head,
                        sweep_started_base=sweep_started_base,
                        head_changed=head_changed, base_changed=base_changed,
                    )
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
                if bounded:
                    return more_work(
                        sweep=sweep, stage_index=stage_index + 1,
                        initialized=True, sweep_started_head=sweep_started_head,
                        sweep_started_base=sweep_started_base,
                        head_changed=head_changed, base_changed=base_changed,
                    )
                continue

            if resuming_stage:
                commits_before = pending_stage["commits_before"]
                started_head = pending_stage["started_head_sha"]
                before_attempt_id = pending_stage.get("before_attempt_id")
            else:
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
                started_head = current_head
            if bounded:
                pending_stage = {
                    "sweep": sweep, "stage_index": stage_index,
                    "started_head_sha": started_head,
                    "commits_before": commits_before,
                    "before_attempt_id": before_attempt_id,
                    "phase": "executing",
                }
                save_cursor(
                    sweep=sweep, stage_index=stage_index, initialized=True,
                    sweep_started_head=sweep_started_head,
                    sweep_started_base=sweep_started_base,
                    head_changed=head_changed, base_changed=base_changed,
                    pending=pending_stage,
                )
            stage_options: dict[str, Any] = {"bounded": True} if bounded else {}
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
                **stage_options,
            )
            if bounded and launched.get("waiting") is True:
                if launched.get("returncode") != 0:
                    raise WorkflowError("bounded stage reported waiting with a failed execution")
                pending_stage["phase"] = "waiting"
                return more_work(
                    sweep=sweep, stage_index=stage_index, initialized=True,
                    sweep_started_head=sweep_started_head,
                    sweep_started_base=sweep_started_base,
                    head_changed=head_changed, base_changed=base_changed,
                    pending=pending_stage,
                    wait_seconds=launched.get("wait_seconds"),
                )
            pending_stage = None
            settled = settle_after_stage(
                repo_root,
                target,
                started_head_sha=started_head,
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
                "started_head_sha": started_head,
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
                pr_head = settled.get("pr_head_sha") or started_head
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
                        repo_root, started_head, local_head
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
                        **(
                            {"source_drift": stage_result["source_drift"]}
                            if stage_result.get("source_drift") is not None else {}
                        ),
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
            head_changed = head_changed or ended_head != started_head
            current_pr = read_pull_request(target)
            after = inspect_stage_for_run(
                entry,
                target,
                ended_head,
                current_pr["base_sha"],
                run_id,
            )
            child_terminal = launched.get("child_terminal_result")
            if (
                after.get("reason") == "no_state"
                and isinstance(child_terminal, dict)
            ):
                after["sealed_terminal"] = child_terminal
                after["missing_state_diagnostic"] = {
                    "reason": "no_state",
                    "expected_path": after.get("status_state"),
                }
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
                    **(
                        {"source_drift": after["source_drift"]}
                        if after.get("source_drift") is not None else {}
                    ),
                    **common.ci_warning_fields([after]),
                }
            )
            runs.append(record)
            report_event(report, "stage_finished", run_id=run_id, **record)
            if launched.get("returncode") != 0:
                failure = common.stage_failure_summary(after)
                blocker = stage_blocker(
                    after,
                    after_launch=True,
                    conflict_strategy=conflict_strategy,
                )
                execution_detail = (
                    failure.get("error")
                    or (blocker[1] if blocker is not None else None)
                    or launched.get("error")
                )
                return blocked_result(
                    pr=current_pr,
                    run_id=run_id,
                    sweeps=completed_sweeps,
                    runs=runs,
                    stage=entry["stage"],
                    reason="stage_execution_failed",
                    detail=(
                        execution_detail
                        if isinstance(execution_detail, str) and execution_detail
                        else (
                            f"{entry['stage']} exited with code "
                            f"{launched.get('returncode')}; "
                            f"see {launched.get('log_path')}"
                        )
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
            if bounded:
                return more_work(
                    sweep=sweep, stage_index=stage_index + 1,
                    initialized=True, sweep_started_head=sweep_started_head,
                    sweep_started_base=sweep_started_base,
                    head_changed=head_changed, base_changed=base_changed,
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
        if (
            not head_changed
            and not base_changed
            and not requires_ci_revalidation_sweep(stages)
        ):
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
        if bounded:
            return more_work(
                sweep=sweep + 1, stage_index=0,
                initialized=False, sweep_started_head=final_head,
                sweep_started_base=pr["base_sha"],
                head_changed=False, base_changed=False,
            )

    raise WorkflowError("the pipeline ended without a result")


def command_run(args: argparse.Namespace) -> None:
    common.ACTIVE_GITHUB_MUTATION_POLICY = args.github_mutation_policy
    args.run_id = (
        common._EXECUTION.run_id if common._EXECUTION is not None else uuid.uuid4().hex
    )
    require_tools()
    repo_root = resolve_repo_root()
    target = resolve_target(args.target, repo_root)
    args.result_path = run_result_path(target, args.run_id)
    reporter = ProgressReporter(target=target, result_path=args.result_path)
    options = {
        "models": stage_models(args.stage_model, args.effort),
        "effort": args.effort,
        "conflict_strategy": args.conflict_strategy,
        "report": reporter,
        "run_id": args.run_id,
    }
    result = run_pipeline(target, repo_root, **options)
    pr = result.get("pr")
    if (
        isinstance(pr, dict)
        and type(pr.get("number")) is int
        and isinstance(pr.get("title"), str)
        and pr["title"]
    ):
        result["session_title"] = (
            f"PR Pipeline: #{pr['number']} - {pr['title']}"
        )
    reporter({"event": "pipeline_finished", **result})


def command_start(args: argparse.Namespace) -> None:
    session_id = require_session_id()
    require_tools()
    repo_root = resolve_repo_root()
    target = resolve_target(args.target, repo_root)
    run_id = uuid.uuid4().hex
    state_path = run_state_path(target, run_id)
    if state_path.exists():
        raise WorkflowError("new pipeline run state already exists")
    state = {
        "schema": 1, "run_id": run_id, "session_id": session_id,
        "target": target, "repo_root": str(repo_root),
        "models": stage_models(args.stage_model, args.effort),
        "effort": args.effort, "conflict_strategy": args.conflict_strategy,
        "github_mutation_policy": args.github_mutation_policy,
        "status": "active", "cursor": {}, "created_at": utc_now(),
        "previous_execution": execution_identity(),
    }
    save_run_state(state_path, state)
    emit({
        "result": "continue", "run_id": run_id, "target": target["pr_url"],
        "state": str(state_path),
    })


def command_advance(args: argparse.Namespace) -> None:
    session_id = require_session_id()
    if common.RUN_ID_PATTERN.fullmatch(args.run_id) is None:
        raise WorkflowError("invalid pipeline run ID")
    require_tools()
    repo_root = resolve_repo_root()
    target = (
        parse_target(args.target)
        if "/" in args.target else resolve_target(args.target, repo_root)
    )
    state_path = run_state_path(target, args.run_id)
    lock_path = state_path.with_name("step.lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise WorkflowError("a pipeline step is already active or was interrupted") from None
    try:
        os.close(descriptor)
        state = load_run_state(state_path, session_id=session_id)
        if (
            state["run_id"] != args.run_id
            or state["target"] != target
            or state["repo_root"] != str(repo_root)
        ):
            raise WorkflowError("pipeline run identity or checkout changed")
        if state["status"] not in {"active", "waiting"}:
            raise WorkflowError(f"pipeline run is {state['status']}; it cannot advance")
        try:
            require_finished_step(state)
        except WorkflowError as error:
            state["status"] = "blocked"
            state["error"] = str(error)
            save_run_state(state_path, state)
            raise
        previous = state["cursor"]
        if isinstance(previous.get("pending_stage"), dict) and (
            previous["pending_stage"].get("phase") != "waiting"
        ):
            raise WorkflowError("previous pipeline step did not finish safely")
        reporter = ProgressReporter(
            target=target, result_path=run_result_path(target, args.run_id)
        )
        common.ACTIVE_GITHUB_MUTATION_POLICY = state["github_mutation_policy"]

        def checkpoint(cursor: dict[str, Any]) -> None:
            state["cursor"] = cursor
            state["previous_execution"] = execution_identity()
            state["status"] = (
                "waiting"
                if isinstance(cursor.get("pending_stage"), dict)
                and cursor["pending_stage"].get("phase") == "waiting"
                else "active"
            )
            save_run_state(state_path, state)

        try:
            outcome = run_pipeline(
                target, repo_root, models=state["models"],
                effort=state["effort"], run_id=args.run_id,
                conflict_strategy=state["conflict_strategy"], report=reporter,
                cursor=previous if previous else None, checkpoint=checkpoint,
            )
        except BaseException as error:
            state["status"] = "blocked"
            state["error"] = str(error)
            save_run_state(state_path, state)
            raise
        if outcome["result"] in {"continue", "waiting"}:
            emit({
                key: value for key, value in outcome.items() if key != "cursor"
            })
        else:
            state["status"] = (
                "complete" if outcome["result"] == "complete" else "blocked"
            )
            save_run_state(state_path, state)
            reporter({"event": "pipeline_finished", **outcome})
    finally:
        lock_path.unlink(missing_ok=True)


def report_run_error(args: argparse.Namespace, error: str) -> None:
    run_id = getattr(args, "run_id", None)
    if not isinstance(run_id, str) or not common.RUN_ID_PATTERN.fullmatch(run_id):
        run_id = uuid.uuid4().hex
    event = {
        "event": "pipeline_finished", "result": "error", "error": error, "run_id": run_id,
    }
    result_path = getattr(args, "result_path", None) or (
        run_root() / "errors" / run_id / "result.json"
    )
    try:
        ProgressReporter(result_path=result_path)(event)
    except (WorkflowError, OSError, ValueError) as reporting_error:
        emit({
            "event": "pipeline_reporting_failed", "run_id": run_id,
            "error": str(reporting_error), "pipeline_error": error,
        })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "start"):
        run_command = subparsers.add_parser(
            name, help="run the five stages" if name == "run"
            else "start one agent-driven bounded pipeline run"
        )
        run_command.add_argument(
            "target", nargs="?",
            help="PR URL, owner/repo#number, or PR number in the current repository",
        )
        run_command.add_argument(
            "--stage-model", action="append",
            help="pin one stage's model as <stage>=<model>; repeatable",
        )
        run_command.add_argument("--effort", default=DEFAULT_EFFORT)
        run_command.add_argument(
            "--conflict-strategy", choices=common.CONFLICT_STRATEGIES,
            default="auto",
        )
        run_command.add_argument(
            "--github-mutation-policy", choices=("allow", "source-only"),
            default="allow",
        )
        run_command.set_defaults(
            function=command_run if name == "run" else command_start
        )
    advance = subparsers.add_parser(
        "advance", help="advance one bounded step of the current session's run"
    )
    advance.add_argument("target")
    advance.add_argument("--run-id", required=True)
    advance.set_defaults(function=command_advance)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.function(args)
        return 0
    except (json.JSONDecodeError, OSError, RuntimeError, TypeError) as error:
        report_run_error(args, str(error))
        return 1
    except KeyboardInterrupt:
        report_run_error(args, "interrupted")
        return 130


_EXECUTION = None
EXECUTION_TERMINAL_RESULTS = frozenset({
    "complete",
    "incomplete",
    "continue",
    "waiting",
})
EXECUTION_SHA256 = "737375138585724c2ff1eb5a3e3dc84f432839e6b494a165f12ecb478617b458"
EXECUTION_RELATIVE_PATH = Path('scripts', 'execution.py')


def load_execution_runtime(source_path: Path) -> ModuleType:
    if (
        not source_path.is_absolute()
        or not source_path.is_file()
        or source_path.is_symlink()
        or source_path.parent.is_symlink()
    ):
        raise RuntimeError("execution Runtime source path is invalid")
    source_path = source_path.resolve()
    source = source_path.read_bytes()
    if hashlib.sha256(source).hexdigest() != EXECUTION_SHA256:
        raise RuntimeError("execution Runtime source digest changed")
    module = ModuleType("_trask_foreground_execution")
    module.__file__ = str(source_path)
    sys.modules[module.__name__] = module
    try:
        exec(compile(source, str(source_path), "exec", dont_inherit=True), module.__dict__)
    except BaseException:
        sys.modules.pop(module.__name__, None)
        raise
    return module


def _load_execution():
    """Load only the pinned shared foreground execution source."""
    inventory = subprocess.run(
        ["copilot", "skill", "list", "--json"], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        **({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
           if os.name == "nt" else {}),
    )
    matches = [
        entry for entry in json.loads(inventory.stdout)
        if entry.get("name") == "agent-tasks-runtime" and entry.get("source") == "plugin"
        and entry.get("enabled") is True
    ]
    if len(matches) != 1:
        raise RuntimeError("shared execution Runtime is not uniquely installed and enabled")
    root = Path(matches[0]["path"])
    return load_execution_runtime(root / EXECUTION_RELATIVE_PATH)


def execution_main():
    commands = ('run', 'start', 'advance')
    arguments = sys.argv[1:]
    selected = (
        os.environ.get("TRASK_EXECUTION_PARENT")
        or arguments
        and arguments[0] in {*commands, "execution-status", "execution-cancel"}
    )
    enabled = (
        "--execution-handle" in arguments or os.environ.get("TRASK_EXECUTION_PARENT")
        or os.environ.get("COPILOT_AGENT_SESSION_ID")
        or arguments and arguments[0] in {"execution-status", "execution-cancel"}
    )
    if not selected or not enabled:
        return main()
    return _load_execution().entrypoint(main, globals(), commands=commands)


if __name__ == "__main__":
    sys.exit(execution_main())
