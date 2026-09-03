#!/usr/bin/env python3
"""Run the PR pipeline as two bounded foreground sweeps."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
import time
import uuid
from typing import Any, Callable


COMMON_MODULE_NAME = "pr_pipeline_common"
COMMON_PATH = Path(__file__).resolve().parent / "pipeline_common.py"


def load_common() -> Any:
    """Load the shared pipeline module that sits beside this script.

    The helper runs from an installed plugin directory that is not on
    ``sys.path``, so the shared module is loaded from its own file location and
    cached under a stable name.
    """
    cached = sys.modules.get(COMMON_MODULE_NAME)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(COMMON_MODULE_NAME, COMMON_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {COMMON_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[COMMON_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


common = load_common()

WorkflowError = common.WorkflowError

MAX_SWEEPS = 2
DEFAULT_STAGE_MODEL = common.DEFAULT_STAGE_MODEL
DEFAULT_EFFORT = common.DEFAULT_EFFORT
CLAUDE_FAMILY = common.CLAUDE_FAMILY
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
PROGRESS_UPDATE_EVENT = common.PROGRESS_UPDATE_EVENT
PROGRESS_HEARTBEAT_INTERVAL = common.PROGRESS_HEARTBEAT_INTERVAL
STAGE_LABELS = {
    STAGE_CONFLICT: "conflict resolution",
    STAGE_COPILOT_REVIEW: "Copilot review",
    STAGE_SELF_REVIEW: "self review",
    STAGE_CI: "CI remediation",
    STAGE_DESCRIPTION: "description validation",
}


def run_slug(target: dict[str, Any]) -> str:
    return f"{target['owner']}--{target['repo']}--pr-{target['number']}"


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


def progress_transition(payload: dict[str, Any]) -> dict[str, Any] | None:
    event = payload.get("event")
    sweep = payload.get("sweep")
    stage = payload.get("stage")
    label = STAGE_LABELS.get(stage, str(stage or "pipeline"))
    prefix = f"Sweep {sweep}/{MAX_SWEEPS}: " if isinstance(sweep, int) else ""
    target_url = payload.get("target")
    number = payload.get("number")
    scope = f" for #{number}" if isinstance(number, int) else ""

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
        if action == "already_clear":
            outcome = "already clear"
        elif clear:
            outcome = "complete"
        else:
            reason = payload.get("stage_reason") or payload.get("outcome")
            outcome = f"not clear: {reason}" if reason else "not clear"
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
                f"{prefix}complete"
                + (
                    f"; still uncleared: {', '.join(uncleared)}."
                    if uncleared
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
        update = {
            "message": (
                f"PR pipeline {result}"
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
        event_log: Path | None = None,
        output: Callable[[dict[str, Any]], None] = emit,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
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
    entry: dict[str, Any], target: dict[str, Any]
) -> dict[str, Any]:
    return common.read_stage_status(entry, target)


def inspect_stage(
    entry: dict[str, Any],
    target: dict[str, Any],
    head_sha: str,
    base_sha: str | None = None,
) -> dict[str, Any]:
    return common.inspect_stage(
        entry, target, head_sha, base_sha, read_status=read_stage_status
    )


def inspect_stages(
    target: dict[str, Any], head_sha: str, base_sha: str
) -> list[dict[str, Any]]:
    return common.inspect_stages(target, head_sha, base_sha, inspect=inspect_stage)


def stage_accepts_pipeline_position(entry: dict[str, Any]) -> bool:
    return common.stage_accepts_pipeline_position(entry)


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
) -> list[str]:
    return common.stage_command(
        entry,
        target,
        model=model,
        effort=effort,
        arguments=pipeline_arguments(entry, run_id, sweep),
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
    report: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    command = stage_command(
        entry,
        target,
        model=model,
        effort=effort,
        run_id=run_id,
        sweep=sweep,
    )
    log_path = stage_log_path(target, run_id, sweep, entry)
    if entry["stage"] != STAGE_CI:
        return common.run_foreground(command, cwd=repo_root, log_path=log_path)

    last_signature: str | None = None

    def progress() -> None:
        nonlocal last_signature
        current = common.stage_live_progress(entry, target)
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
    return payload


def run_pipeline(
    target: dict[str, Any],
    repo_root: Path,
    *,
    models: dict[str, str],
    effort: str,
    run_id: str | None = None,
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

            before = inspect_stage(entry, target, current_head, pr["base_sha"])
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
                    "published_commits": [],
                }
                runs.append(record)
                report_event(report, "stage_finished", run_id=run_id, **record)
                continue
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
                stages = inspect_stages(target, pr_head, current_pr["base_sha"])
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
                        "status": stage_result["status"],
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
            after = inspect_stage(
                entry,
                target,
                ended_head,
                current_pr["base_sha"],
            )
            record.update(
                {
                    "ended_head_sha": ended_head,
                    "outcome": after["outcome"],
                    "clear": after["clear"],
                    "stage_reason": after["reason"],
                    "status": after["status"],
                }
            )
            runs.append(record)
            report_event(report, "stage_finished", run_id=run_id, **record)
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
        stages = inspect_stages(target, final_head, pr["base_sha"])
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
    ]
    for override in args.stage_model or []:
        command.extend(["--stage-model", override])
    return command


def command_start(args: argparse.Namespace) -> None:
    repo_root = resolve_repo_root()
    target = resolve_target(args.target, repo_root)
    run_id = uuid.uuid4().hex
    event_log = progress_log_path(target, run_id)
    launch_path = launch_state_path(target, run_id)
    started_at_epoch = time.time()
    common.write_json_atomically(
        launch_path,
        {
            "kind": RUN_KIND,
            "run_id": run_id,
            "target": target,
            "pid": None,
            "event_log": str(event_log),
            "started_at": utc_now(),
            "started_at_epoch": started_at_epoch,
        },
    )
    process = common.start_detached(
        scheduler_command(args, target, run_id, event_log),
        cwd=repo_root,
        log_path=scheduler_log_path(target, run_id),
    )
    try:
        common.write_json_atomically(
            launch_path,
            {
                "kind": RUN_KIND,
                "run_id": run_id,
                "target": target,
                "pid": process.pid,
                "event_log": str(event_log),
                "started_at": utc_now(),
                "started_at_epoch": started_at_epoch,
            },
        )
    except OSError:
        process.terminate()
        raise
    emit(
        {
            "event": "pipeline_launched",
            "run_id": run_id,
            "target": f"{target['owner']}/{target['repo']}#{target['number']}",
            "pid": process.pid,
            "cursor": 0,
        }
    )


def command_watch(args: argparse.Namespace) -> None:
    target = parse_target(args.target)
    run_id = common.validate_run_id(args.run_id)
    emit(
        common.watch_progress(
            event_log=progress_log_path(target, run_id),
            launch_path=launch_state_path(target, run_id),
            observer_path=observer_state_path(target, run_id),
            cursor=args.cursor,
            wait_seconds=args.wait_seconds,
        )
    )


def command_run(args: argparse.Namespace) -> None:
    require_tools()
    repo_root = resolve_repo_root()
    target = resolve_target(args.target, repo_root)
    event_log = Path(args.event_log).resolve() if args.event_log else None
    reporter = ProgressReporter(target=target, event_log=event_log)
    options = {
        "models": stage_models(args.stage_model),
        "effort": args.effort,
        "report": reporter,
    }
    if args.run_id:
        options["run_id"] = common.validate_run_id(args.run_id)
    result = run_pipeline(target, repo_root, **options)
    reporter({"event": "pipeline_finished", **result})


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
    start.set_defaults(function=command_start)

    watch = subparsers.add_parser(
        "watch", help="wait for progress or one five-minute heartbeat"
    )
    watch.add_argument("target", help="the canonical owner/repo#number from start")
    watch.add_argument("--run-id", required=True)
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
                    "finished": False,
                    "monitor_failure": str(error),
                }
            )
            return 1
        if args.command == "start":
            emit({"event": "pipeline_launch_failed", "error": str(error)})
            return 1
        event = {"event": "pipeline_finished", "result": "error", "error": str(error)}
        event_log = getattr(args, "event_log", None)
        ProgressReporter(
            event_log=Path(event_log).resolve() if event_log else None
        )(event)
        return 1
    except KeyboardInterrupt:
        if args.command == "watch":
            emit(
                {
                    "event": PROGRESS_UPDATE_EVENT,
                    "updates": [],
                    "finished": False,
                    "monitor_failure": "interrupted",
                }
            )
            return 130
        if args.command == "start":
            emit({"event": "pipeline_launch_failed", "error": "interrupted"})
            return 130
        event = {
            "event": "pipeline_finished",
            "result": "error",
            "error": "interrupted",
        }
        event_log = getattr(args, "event_log", None)
        ProgressReporter(
            event_log=Path(event_log).resolve() if event_log else None
        )(event)
        return 130


if __name__ == "__main__":
    sys.exit(main())
