#!/usr/bin/env python3
"""Drive one native GitHub stack through the five pipeline stages.

This helper is orchestration only. Every unit of work is delegated to the
plugin-qualified agent that already owns that stage, and no stage policy is
reimplemented here: the helper decides who runs, where, in which order, and
when a result counts, while each stage decides what to do.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from types import ModuleType
from typing import Any, Callable


COMMON_MODULE_NAME = "pr_pipeline_common"
COMMON_PATH = Path(__file__).resolve().parent / "pipeline_common.py"
COMMON_SHA256 = "0b0ad38c9264aae92bc8c4932ad0dfb940e7efe386e1c3361fa32013bf30c3ad"


def load_common() -> Any:
    """Load the shared pipeline module that sits beside this script."""
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


class PipelineCancelled(RuntimeError):
    pass


STATE_VERSION = 1
MAX_PASSES = 2
DEFAULT_EFFORT = common.DEFAULT_EFFORT
READINESS_TIMEOUT = 300.0
READINESS_POLL_INTERVAL = 2.0
MONITOR_POLL_INTERVAL = 15.0
STACK_ENTRIES_PAGE = 50
RUN_KIND = "pr-stack-pipeline"
OWNERSHIP_SUFFIX = ".worktree.json"
WINDOWS_WORKTREE_DIRECTORY = "cpw"
WINDOWS_WORKTREE_PATH_BUDGET = 120
WINDOWS_PR_NUMBER_RESERVE = 10
CONFLICT_PROPAGATE_COMMAND = "descendant-propagate"
PROGRESS_EVENT = common.PROGRESS_EVENT
TERMINAL_RESULT_MAX_BYTES = 8192
TERMINAL_RESULT_MAX_PULL_REQUESTS = 12
TERMINAL_RESULT_MAX_PHASES = 10
TERMINAL_TEXT_MAX_CHARS = 512

STAGE_CONFLICT = common.STAGE_CONFLICT
STAGE_COPILOT_REVIEW = common.STAGE_COPILOT_REVIEW
STAGE_SELF_REVIEW = common.STAGE_SELF_REVIEW
STAGE_CI = common.STAGE_CI
STAGE_DESCRIPTION = common.STAGE_DESCRIPTION
STAGES = common.STAGES
STAGE_NAMES = common.STAGE_NAMES
STAGE_BY_NAME = common.STAGE_BY_NAME

PHASE_STACK_DISPATCH = "stack-dispatch"
PHASE_PARALLEL = "parallel"
PHASE_BOTTOM_UP = "bottom-up"

PHASES: tuple[dict[str, str], ...] = (
    {"phase": STAGE_CONFLICT, "stage": STAGE_CONFLICT, "mode": PHASE_STACK_DISPATCH},
    {
        "phase": STAGE_COPILOT_REVIEW,
        "stage": STAGE_COPILOT_REVIEW,
        "mode": PHASE_PARALLEL,
    },
    {"phase": STAGE_SELF_REVIEW, "stage": STAGE_SELF_REVIEW, "mode": PHASE_PARALLEL},
    {"phase": STAGE_CI, "stage": STAGE_CI, "mode": PHASE_BOTTOM_UP},
    {"phase": STAGE_DESCRIPTION, "stage": STAGE_DESCRIPTION, "mode": PHASE_PARALLEL},
)
PHASE_NAMES = tuple(phase["phase"] for phase in PHASES)
PHASE_AGENTS = {
    phase["phase"]: STAGE_BY_NAME[phase["stage"]]["agent"] for phase in PHASES
}
STAGE_LABELS = {
    STAGE_CONFLICT: "conflict resolution",
    STAGE_COPILOT_REVIEW: "Copilot review",
    STAGE_SELF_REVIEW: "self review",
    STAGE_CI: "CI remediation",
    STAGE_DESCRIPTION: "description validation",
}


report_safely = common.report_safely


# Thin wrappers keep every shared call a seam a test can replace by name.


def stage_script_path(entry: dict[str, Any]) -> Path:
    return common.stage_script_path(entry)


def stage_state_path(
    entry: dict[str, Any], target: dict[str, Any], run_id: str | None = None
) -> Path:
    return common.stage_state_path(entry, target, run_id)


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


def stage_result_summary(stage_result: dict[str, Any]) -> dict[str, Any]:
    status = stage_result.get("status")
    task = status.get("agent_task") if isinstance(status, dict) else None
    return {
        key: value
        for key, value in {
            "stage": stage_result.get("stage"),
            "clear": stage_result.get("clear"),
            "clear_at_head_sha": stage_result.get("clear_at_head_sha"),
            "clear_at_base_sha": stage_result.get("clear_at_base_sha"),
            "clearance_kind": stage_result.get("clearance_kind"),
            "warning_verification": stage_result.get("warning_verification"),
            "clearance_verification": (status or {}).get("clearance_verification"),
            "run_id": (status or {}).get("run_id"),
            "pipeline_run": (status or {}).get("pipeline_run"),
            "native_stack_clearance": (status or {}).get("native_stack_clearance"),
            "source_drift": stage_result.get("source_drift"),
            **common.ci_warning_fields([stage_result]),
            "outcome": stage_result.get("outcome"),
            "reason": stage_result.get("reason"),
            "status_state": stage_result.get("status_state"),
            "agent_task": task,
            "detail": stage_result.get("detail"),
            "inspected_head_sha": stage_result.get("inspected_head_sha"),
            "inspected_base_sha": stage_result.get("inspected_base_sha"),
        }.items()
        if value is not None
    }


def stack_ci_warning_fields(pull_requests: list[dict[str, Any]]) -> dict[str, Any]:
    warnings = [
        {
            **warning,
            "number": pull_request["number"],
            "head_sha": pull_request["head_sha"],
            "base_sha": pull_request["base_sha"],
        }
        for pull_request in pull_requests
        for warning in pull_request.get("ci_warnings", [])
    ]
    return {"ci_warnings": warnings, "all_ci_passed": False} if warnings else {}


def gh_json(arguments: list[str]) -> Any:
    return common.gh_json(arguments)


def base_ref_tip(repo_name: str, base_branch: str) -> str:
    return common.base_ref_tip(repo_name, base_branch, api=gh_json)


def commit_contains(repository: str, ancestor: str, descendant: str) -> bool:
    payload = gh_json(
        ["api", f"repos/{repository}/compare/{ancestor}...{descendant}"]
    )
    return isinstance(payload, dict) and payload.get("status") in {
        "ahead",
        "identical",
    }


def utc_now() -> str:
    return common.utc_now()


def session_title(kickoff: dict[str, Any], pull_request_title: str) -> str:
    return (
        f"PR Stack Pipeline: #{kickoff['startPullRequest']} - "
        f"{pull_request_title}"
    )


def run_slug(kickoff: dict[str, Any]) -> str:
    owner, _, repo = kickoff["repository"].partition("/")
    return (
        f"{owner}--{repo}--stack-{kickoff['stackNumber']}"
        f"--from-{kickoff['startPullRequest']}"
    )


def run_root() -> Path:
    return common.copilot_home() / "run" / RUN_KIND


def run_directory_for(kickoff: dict[str, Any], run_id: str) -> Path:
    return run_root() / run_slug(kickoff) / run_id


def state_path_for(kickoff: dict[str, Any], run_id: str) -> Path:
    return run_directory_for(kickoff, run_id) / "state.json"


def validate_worktree_root(root: Path, *, platform_name: str | None = None) -> Path:
    platform_name = platform_name or os.name
    if platform_name != "nt":
        return root
    longest_path = root / ("9" * WINDOWS_PR_NUMBER_RESERVE)
    if len(str(longest_path)) > WINDOWS_WORKTREE_PATH_BUDGET:
        raise WorkflowError(
            "the Windows worker worktree root exceeds the "
            f"{WINDOWS_WORKTREE_PATH_BUDGET}-character path budget: {root}"
        )
    return root


def worktree_root_for(
    run_directory: Path,
    run_id: str,
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    platform_name = platform_name or os.name
    if platform_name != "nt":
        return run_directory / "worktrees"
    environment = os.environ if environ is None else environ
    local_app_data = environment.get("LOCALAPPDATA")
    if not local_app_data:
        raise WorkflowError("LOCALAPPDATA is required for Windows worker worktrees")
    root = (Path(local_app_data) / WINDOWS_WORKTREE_DIRECTORY / run_id).resolve()
    return validate_worktree_root(root, platform_name=platform_name)


def run_result_path(kickoff: dict[str, Any], run_id: str) -> Path:
    return run_directory_for(kickoff, run_id) / "result.json"


def paths_match(left: Any, right: Path) -> bool:
    if not isinstance(left, str) or not left:
        return False
    try:
        return Path(left).resolve() == right.resolve()
    except OSError:
        return False


def format_pull_requests(numbers: list[int]) -> str:
    return ", ".join(f"#{number}" for number in numbers)


def progress_transition(payload: dict[str, Any]) -> dict[str, Any] | None:
    event = payload.get("event")
    pass_number = payload.get("pull_request_pass")
    stage = payload.get("phase") or payload.get("stage")
    label = STAGE_LABELS.get(stage, str(stage or "pipeline"))
    number = payload.get("number")
    numbers = payload.get("numbers")
    if not isinstance(numbers, list):
        numbers = [number] if isinstance(number, int) else []
    prefix = (
        f"Pass {pass_number}/{MAX_PASSES}: "
        if isinstance(pass_number, int)
        else ""
    )

    update: dict[str, Any]
    if event == "stack_pipeline_started":
        selected = payload.get("selected") or []
        numbers = selected
        update = {
            "message": (
                f"Stack #{payload.get('stack_number')} pipeline starting for "
                f"{format_pull_requests(selected)}."
            ),
            "next_action": "Validate the live native stack topology.",
            "waiting": True,
            "wait_reason": "validating the stack with GitHub",
        }
    elif event == "topology_validated":
        selected = payload.get("selected") or []
        numbers = selected
        update = {
            "message": f"Stack topology validated for {format_pull_requests(selected)}.",
            "next_action": "Start pass 1.",
            "waiting": False,
        }
    elif event == "pass_started":
        update = {
            "message": f"{prefix}started.",
            "next_action": f"Start {STAGE_LABELS[STAGE_CONFLICT]}.",
            "waiting": False,
        }
    elif event == "phase_started":
        update = {
            "message": (
                f"{prefix}{label} starting for {format_pull_requests(numbers)}."
            ),
            "next_action": "Create, verify, and start the required worker processes.",
            "waiting": True,
            "wait_reason": "starting workers and checking GitHub state",
        }
        if payload.get("phase") == STAGE_DESCRIPTION and "reused" in payload:
            dispatch_numbers = payload.get("dispatch_numbers") or []
            update = {
                "message": (
                    f"{prefix}{label}: reusing clearance for "
                    f"{format_pull_requests(payload['reused'])}; starting workers for "
                    f"{format_pull_requests(dispatch_numbers)}."
                ),
                "next_action": (
                    "Create, verify, and start the required worker processes."
                    if dispatch_numbers else "Collect the phase outcome without new workers."
                ),
                "waiting": bool(dispatch_numbers),
                "wait_reason": "starting Description workers" if dispatch_numbers else None,
            }
    elif event == "worker_starting":
        update = {
            "message": f"{prefix}{label} worker starting for #{number}.",
            "next_action": "Verify the worktree and wait for durable worker readiness.",
            "waiting": True,
            "wait_reason": f"starting the {label} worker for #{number}",
        }
    elif event == "worker_active":
        update = {
            "message": f"{prefix}{label} running for #{number}.",
            "next_action": "Wait for the worker result.",
            "waiting": True,
            "wait_reason": f"waiting for the {label} worker on #{number}",
        }
    elif event == "worker_wait_started":
        update = {
            "message": f"{prefix}{label} still running for #{number}.",
            "next_action": "Collect the worker result when it exits.",
            "waiting": True,
            "wait_reason": f"waiting for the {label} worker on #{number}",
        }
    elif event == "worker_progress":
        phase = payload.get("phase")
        action_checks = payload.get("action_checks") or []
        pending_checks = payload.get("pending_checks") or []
        if phase == "waiting_for_review":
            message = f"{prefix}#{number} is waiting for Copilot's review."
            next_action = "Collect the review, then address any comments it contains."
        elif phase == "addressing_comments":
            message = f"{prefix}#{number} is addressing Copilot review comments."
            next_action = "Finish the current comment batch and validate its changes."
        elif phase == "validating":
            message = f"{prefix}#{number} is validating Copilot review fixes."
            next_action = "Fix any validation failure, then publish the reviewed changes."
        elif phase == "diagnosing":
            message = f"{prefix}{label} diagnosing {len(action_checks)} known failure(s) for #{number}."
            next_action = "Attribute the known failure from its logs and the pinned diff."
        elif phase == "fixing":
            message = f"{prefix}{label} fixing {len(action_checks)} attributed failure(s) for #{number}."
            next_action = "Validate, commit, and publish the fix."
        elif phase == "rerunning":
            message = f"{prefix}{label} retrying {len(action_checks)} suspected flake(s) for #{number}."
            next_action = "Request one safe retry, then inspect its result."
        else:
            message = f"{prefix}{label} monitoring {len(pending_checks)} pending check(s) for #{number}."
            next_action = "Inspect the next concrete failure as soon as it completes."
        wait_reason = {
            "waiting_for_review": f"waiting for Copilot's review on #{number}",
            "addressing_comments": f"addressing Copilot review comments on #{number}",
            "validating": f"validating Copilot review fixes on #{number}",
        }.get(phase)
        update = {
            "message": message,
            "next_action": next_action,
            "waiting": True,
            "wait_reason": wait_reason or (
                f"{phase} a known CI failure for #{number}"
                if phase != "waiting"
                else f"waiting for remaining CI checks on #{number}"
            ),
        }
    elif event == "worker_finished":
        returncode = payload.get("returncode")
        accepted = payload.get("accepted")
        clear = payload.get("clear")
        reason = payload.get("reason")
        blocked = payload.get("blocking_reason")
        if returncode not in {None, 0}:
            outcome = f"failed for #{number} with exit code {returncode}"
            next_action = "Finish the stage and preserve the failure in the pipeline result."
        elif not accepted:
            outcome = f"finished for #{number}, but its stale result was ignored"
            next_action = "Continue with evidence for the current pull request head."
        elif blocked:
            outcome = f"blocked for #{number}: {blocked}"
            next_action = "Stop without launching a replacement worker."
        elif clear and payload.get("clearance_kind") == "ci_warning":
            outcome = f"completed WITH CI WARNINGS for #{number}"
            next_action = "Collect any remaining worker results; not all CI passed."
        elif clear:
            outcome = f"completed for #{number}"
            next_action = "Collect any remaining worker results."
        else:
            outcome = f"result collected for #{number}, clearance is not current"
            if reason:
                outcome += f": {reason}"
            if reason == "clearance_is_for_an_older_head":
                recorded = payload.get("clear_at_head_sha")
                live = payload.get("current_head_sha")
                if recorded and live:
                    outcome += f" (recorded {recorded[:8]}, live {live[:8]})"
            elif reason == "clearance_is_for_an_older_base":
                recorded = payload.get("clear_at_base_sha")
                live = payload.get("current_base_sha")
                if recorded and live:
                    outcome += f" (recorded {recorded[:8]}, live {live[:8]})"
            next_action = "Preserve the reason and continue the bounded pass."
        update = {
            "message": f"{prefix}{label} {outcome}.",
            "next_action": next_action,
            "waiting": True,
            "wait_reason": "finishing the current stage",
        }
    elif event == "worker_launch_stopped":
        update = {
            "message": (
                f"{prefix}{label} failed to launch for #{number}: "
                f"{payload.get('reason', payload.get('step', 'unknown reason'))}."
            ),
            "next_action": "Stop the run without retrying or duplicating the worker.",
            "waiting": False,
        }
    elif event == "worker_cancelled":
        update = {
            "message": f"{prefix}{label} worker cancelled for #{number}.",
            "next_action": "Reap the remaining owned workers and finalize cancellation.",
            "waiting": True,
            "wait_reason": "cancelling the stack pipeline",
        }
    elif event == "stack_pipeline_cancelling":
        update = {
            "message": "Stack pipeline cancellation requested.",
            "next_action": "Stop and reap every active worker owned by this run.",
            "waiting": True,
            "wait_reason": "cancelling the stack pipeline",
        }
    elif event == "push_propagated":
        trigger = payload.get("trigger")
        if trigger == "obsolete_checkpoint":
            message = (
                f"{prefix}obsolete CI push from #{number} was superseded by its "
                "current head."
            )
        elif trigger == "checkpoint_revalidation":
            message = (
                f"{prefix}CI push from #{number} could not be revalidated against "
                "its current head."
            )
        elif trigger == "predecessor_alignment":
            message = (
                f"{prefix}live head from #{number} descendant alignment "
                f"{payload.get('result')}."
            )
        else:
            message = (
                f"{prefix}accepted CI push from #{number} propagation "
                f"{payload.get('result')}."
            )
        update = {
            "message": message,
            "next_action": "Continue bottom-up CI remediation.",
            "waiting": True,
            "wait_reason": "waiting for the current CI worker or descendant propagation",
        }
    elif event == "phase_finished":
        stopped = payload.get("stopped")
        blocked = payload.get("blocked")
        action = payload.get("action")
        clear = payload.get("clear")
        reasons = payload.get("reasons") or []
        if action == "completed_this_run":
            outcome = "not run again because current clearance was already verified"
            next_action = "Continue with the next stage."
        elif stopped:
            outcome = "failed"
            next_action = (
                "Stop the pipeline and report the stage failure."
                if stopped.get("step") == "stage_status"
                else "Stop the pipeline and report the launch failure."
            )
        elif blocked:
            outcome = f"blocked: {blocked.get('reason', 'unknown reason')}"
            next_action = "Continue to the snapshot or next bounded pass."
        elif clear and payload.get("ci_warnings"):
            affected = sorted({warning["number"] for warning in payload["ci_warnings"]})
            outcome = f"completed WITH CI WARNINGS for {format_pull_requests(affected)}"
            next_action = "Continue with the next stage; not all CI passed."
        elif clear:
            outcome = "complete"
            if payload.get("reused"):
                outcome += (
                    f"; reused current Description clearance for "
                    f"{format_pull_requests(payload['reused'])}"
                )
            next_action = "Revalidate the stack, then start the next stage."
        else:
            outcome = "results collected; current clearance was not verified"
            if reasons:
                outcome += f": {', '.join(reasons)}"
            next_action = "Continue the bounded pass with the recorded stage reasons."
        update = {
            "message": f"{prefix}{label} {outcome}.",
            "next_action": next_action,
            "waiting": not bool(stopped),
            "wait_reason": (
                "revalidating the stack before the next stage"
                if not stopped
                else None
            ),
        }
    elif event == "snapshot_taken":
        update = {
            "message": (
                f"{prefix}snapshot {payload.get('result')}"
                + (
                    " WITH CI WARNINGS; not all CI passed"
                    if payload.get("ci_warnings")
                    else ""
                )
                + (
                    f": {payload.get('reason')}."
                    if payload.get("reason")
                    else "."
                )
            ),
            "next_action": (
                "Finish the run."
                if payload.get("result") == "complete"
                else "Start the next pass if the two-pass budget allows."
            ),
            "waiting": True,
            "wait_reason": "cleaning up worker worktrees and finalizing the result",
        }
    elif event == "stack_pipeline_finished":
        result = payload.get("result", "unknown")
        reported_result = (
            "completed"
            if result == "complete" and payload.get("all_ci_passed") is False
            else result
        )
        update = {
            "message": (
                f"Stack pipeline {reported_result}"
                + (
                    " WITH CI WARNINGS; not all CI passed"
                    if payload.get("all_ci_passed") is False
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
            "pull_request_pass": pass_number,
            "stage": stage,
            "pull_requests": numbers,
        }
    )
    return {key: value for key, value in update.items() if value is not None}


class ProgressReporter(common.ForegroundProgressReporter):
    def __init__(
        self,
        *,
        output: Callable[[dict[str, Any]], None] = common.emit,
    ) -> None:
        super().__init__(output=output)


STACK_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!, $first: Int!) {"
    "  repository(owner: $owner, name: $name) {"
    "    pullRequest(number: $number) {"
    "      stack {"
    "        id number size baseRefName"
    "        entries(first: $first) {"
    "          nodes {"
    "            position"
    "            pullRequest {"
    "              number title headRefName baseRefName headRefOid mergeable isDraft state"
    "              baseRef { target { oid } }"
    "            }"
    "          }"
    "        }"
    "      }"
    "    }"
    "  }"
    "}"
)


def parse_stack(raw: Any) -> dict[str, Any] | None:
    """Turn one GraphQL stack into an ordered member snapshot.

    Draft and non-draft members are kept, because a stack is reviewed and
    repaired as a unit and dropping drafts would silently shorten it.
    """
    if not isinstance(raw, dict):
        return None
    stack_id = raw.get("id")
    stack_number = raw.get("number")
    if not isinstance(stack_id, str) or not stack_id:
        raise WorkflowError("the native stack has no stable identity")
    if (
        not isinstance(stack_number, int)
        or isinstance(stack_number, bool)
        or stack_number <= 0
    ):
        raise WorkflowError("the native stack has no valid number")
    trunk = raw.get("baseRefName")
    if not isinstance(trunk, str) or not trunk:
        raise WorkflowError("the native stack has no trunk branch")
    entries = raw.get("entries")
    nodes = entries.get("nodes") if isinstance(entries, dict) else None
    if not isinstance(nodes, list):
        raise WorkflowError("the native stack has no ordered member list")
    members: list[dict[str, Any]] = []
    for node in nodes:
        member = node.get("pullRequest") if isinstance(node, dict) else None
        if not isinstance(member, dict):
            raise WorkflowError("the native stack has an unreadable member")
        position = node.get("position")
        number = member.get("number")
        title = member.get("title")
        head_branch = member.get("headRefName")
        base_branch = member.get("baseRefName")
        head_sha = member.get("headRefOid")
        base_ref = member.get("baseRef")
        base_target = base_ref.get("target") if isinstance(base_ref, dict) else None
        if (
            not isinstance(position, int)
            or isinstance(position, bool)
            or position < 0
            or not isinstance(number, int)
            or isinstance(number, bool)
            or number <= 0
            or not isinstance(title, str)
            or not title
            or not isinstance(head_branch, str)
            or not head_branch
            or not isinstance(base_branch, str)
            or not base_branch
            or not isinstance(head_sha, str)
            or not head_sha
        ):
            raise WorkflowError(
                f"native stack member {number!r} is missing a required field"
            )
        members.append(
            {
                "position": position,
                "number": number,
                "title": title,
                "head_branch": head_branch,
                "base_branch": base_branch,
                "head_sha": head_sha,
                "base_sha": base_target.get("oid") if isinstance(base_target, dict) else None,
                "mergeable": member.get("mergeable"),
                "is_draft": bool(member.get("isDraft")),
                "state": member.get("state"),
            }
        )
    members.sort(
        key=lambda item: (item["position"] is None, item["position"], item["number"])
    )
    size = raw.get("size")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
        or size != len(members)
    ):
        raise WorkflowError(
            f"the native stack reports {size!r} members but exposes {len(members)}"
        )
    if [member["position"] for member in members] != list(range(size)):
        raise WorkflowError("the native stack member positions are malformed")
    if len({member["number"] for member in members}) != size:
        raise WorkflowError("the native stack repeats a pull request")
    if len({member["head_branch"] for member in members}) != size:
        raise WorkflowError("the native stack repeats a head branch")
    expected_bases = [trunk, *(member["head_branch"] for member in members[:-1])]
    if [member["base_branch"] for member in members] != expected_bases:
        raise WorkflowError("the native stack branch topology is malformed")
    return {
        "id": stack_id,
        "number": stack_number,
        "size": size,
        "trunk": trunk,
        "members": members,
    }


def read_native_stack(
    repository: str,
    number: int,
    *,
    api: Callable[[list[str]], Any] = gh_json,
) -> dict[str, Any] | None:
    owner, _, repo = repository.partition("/")
    payload = common.graphql(
        STACK_QUERY,
        {
            "owner": owner,
            "name": repo,
            "number": number,
            "first": STACK_ENTRIES_PAGE,
        },
        api=api,
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    repository_payload = data.get("repository") if isinstance(data, dict) else None
    if not isinstance(repository_payload, dict):
        raise WorkflowError("the stack query returned no repository")
    pull = repository_payload.get("pullRequest")
    if not isinstance(pull, dict):
        raise WorkflowError("the stack query returned no pull request")
    return parse_stack(pull.get("stack"))


def topology_fingerprint(stack: dict[str, Any]) -> str:
    """Identify a stack by everything an orchestration decision depends on."""
    material = json.dumps(
        {
            "id": stack.get("id"),
            "number": stack.get("number"),
            "trunk": stack.get("trunk"),
            "members": [
                [member["number"], member["head_branch"], member["base_branch"]]
                for member in stack["members"]
            ],
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def stack_source_identity(stack: dict[str, Any]) -> tuple[dict[str, Any], str]:
    source = {key: stack.get(key) for key in ("id", "number", "size", "trunk")}
    member_keys = ("number", "head_branch", "base_branch", "head_sha")
    source["members"] = [
        {key: member[key] for key in member_keys} for member in stack["members"]
    ]
    snapshot = (
        source["id"], source["number"], source["size"], source["trunk"],
        tuple(tuple(member[key] for key in member_keys) for member in source["members"]),
    )
    digest = hashlib.sha256(
        json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return source, digest


def selection_from_stack(
    target: dict[str, Any], stack: dict[str, Any] | None
) -> dict[str, Any]:
    if stack is None:
        raise WorkflowError(
            f"pull request #{target['number']} is not a member of a native stack"
        )
    numbers = [member["number"] for member in stack["members"]]
    if target["number"] not in numbers:
        raise WorkflowError(
            f"pull request #{target['number']} is missing from its native stack"
        )
    start = stack["members"][numbers.index(target["number"])]
    if start.get("state") != "OPEN":
        raise WorkflowError(
            f"pull request #{target['number']} is not an open native-stack member"
        )
    selected = [
        member["number"]
        for member in stack["members"][numbers.index(target["number"]):]
        if member.get("state") == "OPEN"
    ]
    source_stack, source_snapshot = stack_source_identity(stack)
    return {
        "repository": target["repo_name"],
        "stackNumber": stack["number"],
        "startPullRequest": target["number"],
        "pullRequests": selected,
        "topologyFingerprint": topology_fingerprint(stack),
        "sourceSnapshot": source_snapshot,
        "sourceStack": source_stack,
    }


def selection_evidence(selection: dict[str, Any]) -> dict[str, Any]:
    return {
        "repository": selection["repository"],
        "stack_number": selection["stackNumber"],
        "start_pull_request": selection["startPullRequest"],
        "selected": list(selection["pullRequests"]),
        "topology_fingerprint": selection.get("topologyFingerprint"),
        "source_snapshot": selection.get("sourceSnapshot"),
        "source_stack": selection.get("sourceStack"),
    }


def validate_selection(
    kickoff: dict[str, Any], stack: dict[str, Any] | None
) -> dict[str, Any]:
    """Check the live stack still matches the selection the run was started for."""
    if stack is None:
        return {"result": "stopped", "reason": "not_a_native_stack"}
    if stack.get("number") != kickoff["stackNumber"]:
        return {
            "result": "stopped",
            "reason": "stack_identity_changed",
            "detail": (
                f"the live stack is {stack.get('number')!r}, not "
                f"{kickoff['stackNumber']}"
            ),
        }
    numbers = [member["number"] for member in stack["members"]]
    start = kickoff["startPullRequest"]
    if start not in numbers:
        return {
            "result": "stopped",
            "reason": "start_is_not_a_member",
            "detail": f"#{start} is no longer in stack {kickoff['stackNumber']}",
        }
    index = numbers.index(start)
    suffix = [
        member
        for member in stack["members"][index:]
        if member.get("state") == "OPEN"
    ]
    if stack["members"][index].get("state") != "OPEN":
        return {
            "result": "stopped",
            "reason": "start_is_not_open",
            "detail": f"#{start} is no longer an open native-stack member",
        }
    if [member["number"] for member in suffix] != kickoff["pullRequests"]:
        return {
            "result": "stopped",
            "reason": "selection_is_not_the_stack_suffix",
            "detail": (
                "the selection "
                f"{kickoff['pullRequests']} is no longer the ordered stack suffix "
                f"{[member['number'] for member in suffix]}"
            ),
        }
    fingerprint = topology_fingerprint(stack)
    expected_fingerprint = kickoff.get("topologyFingerprint")
    if (
        isinstance(expected_fingerprint, str)
        and fingerprint != expected_fingerprint
    ):
        return {
            "result": "stopped",
            "reason": "topology_changed",
            "detail": "the native stack topology changed after target discovery",
        }
    return {
        "result": "ready",
        "selected": suffix,
        "fingerprint": fingerprint,
    }


def missing_dependencies(
    *, script_for: Callable[[dict[str, Any]], Path] = stage_script_path
) -> list[str]:
    return [
        entry["stage"] for entry in STAGES if not script_for(entry).is_file()
    ]


def accept_completion(
    completion: dict[str, Any],
    *,
    expected_nonce: str,
    expected_head_sha: str,
) -> bool:
    """Ignore a result that belongs to an earlier dispatch or an older head.

    A worker that was started before a rebase can still report after it. Its
    nonce and the head it was dispatched for are the durable evidence that
    decide whether the result describes the current revisions.
    """
    return (
        completion.get("nonce") == expected_nonce
        and completion.get("head_sha") == expected_head_sha
    )


def new_state(kickoff: dict[str, Any], run_id: str, fingerprint: str) -> dict[str, Any]:
    evidence = selection_evidence(kickoff)
    return {
        "state_version": STATE_VERSION,
        "kind": RUN_KIND,
        "run_id": run_id,
        "kickoff": kickoff,
        "selection": evidence,
        "repository": evidence["repository"],
        "stack_number": evidence["stack_number"],
        "start_pull_request": evidence["start_pull_request"],
        "topology_fingerprint": fingerprint,
        "selected": list(kickoff["pullRequests"]),
        "source_snapshot": evidence["source_snapshot"],
        "source_stack": evidence["source_stack"],
        "pass": 0,
        "phase": None,
        "dispatch": None,
        "pull_requests": {},
        "result": None,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    common.write_json_atomically(path, state)


def load_state(path: Path) -> dict[str, Any] | None:
    payload = common.read_json(path)
    if not isinstance(payload, dict):
        return None
    if payload.get("state_version") != STATE_VERSION or payload.get("kind") != RUN_KIND:
        return None
    return payload


def worktree_ownership_path(run_directory: Path, number: int) -> Path:
    return run_directory / "worktrees" / f"{number}{OWNERSHIP_SUFFIX}"


def worktree_path(worktree_root: Path, number: int) -> Path:
    return worktree_root / str(number)


def owns_worktree(record: Any, run_id: str, path: Path) -> bool:
    if not isinstance(record, dict):
        return False
    status = record.get("status")
    return (
        record.get("run_id") == run_id
        and record.get("path") == str(path)
        and (status is None or type(status) is str and status == "active")
    )


def removed_worktree(record: Any, run_id: str, path: Path) -> bool:
    return (
        isinstance(record, dict)
        and record.get("run_id") == run_id
        and record.get("path") == str(path)
        and record.get("status") == "removed"
    )


def worker_prompt(
    target: dict[str, Any], arguments: list[str], *, scope: str | None = None
) -> str:
    prompt = common.stage_prompt(target, arguments)
    if scope:
        head, separator, rest = prompt.partition("\n\n")
        prompt = f"{head}\n\n{scope}" + (f"{separator}{rest}" if separator else "")
    return prompt


class WorkerLauncher:
    """Create, verify, start, and confirm exactly one worker at a time.

    Every step is a separate method so the serialized launch loop can stop at
    the first failure with a reason, and so a test can drive the loop without a
    real repository or a real agent.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        repository: str,
        run_id: str,
        run_directory: Path,
        worktree_root: Path | None = None,
        models: dict[str, str],
        effort: str,
        readiness_timeout: float = READINESS_TIMEOUT,
        poll_interval: float = READINESS_POLL_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.repo_root = repo_root
        self.repository = repository
        self.run_id = run_id
        self.run_directory = run_directory
        self.worktree_root = validate_worktree_root(
            worktree_root or worktree_root_for(run_directory, run_id)
        )
        self.models = models
        self.effort = effort
        self.readiness_timeout = readiness_timeout
        self.poll_interval = poll_interval
        self.sleep = sleep
        self.monotonic = monotonic

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        number = request["number"]
        path = worktree_path(self.worktree_root, number)
        record_path = worktree_ownership_path(self.run_directory, number)
        record = common.read_json(record_path)
        if path.exists() and not owns_worktree(record, self.run_id, path):
            return {
                "result": "failed",
                "reason": "worktree_is_not_owned_by_this_run",
                "detail": f"{path} already exists",
            }
        target = common.target_for(self.repository, number)
        fetched = common.fetch_pr_head(self.repo_root, target)
        if fetched["result"] != "ready":
            return {"result": "failed", **fetched}
        if fetched["head_sha"] != request["head_sha"]:
            return {
                "result": "failed",
                "reason": "dispatch_head_is_stale",
                "detail": (
                    f"pull request #{number} is at {fetched['head_sha']}, not "
                    f"{request['head_sha']}"
                ),
            }
        if path.exists():
            dirt = common.worktree_dirt(path)
            if dirt:
                return {
                    "result": "failed",
                    "reason": "worktree_is_dirty",
                    "detail": dirt,
                }
            checked_out = common.checkout_fetched_head(path, request["head_sha"])
            if checked_out["result"] != "ready":
                return {"result": "failed", **checked_out}
            record["head_sha"] = request["head_sha"]
            record["status"] = "active"
            record["updated_at"] = utc_now()
            common.write_json_atomically(record_path, record)
            return {"result": "ready", "worktree": path, "reused": True}
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        added = common.run(
            [
                "git",
                "-C",
                str(self.repo_root),
                "worktree",
                "add",
                "--detach",
                str(path),
                request["head_sha"],
            ],
            check=False,
        )
        if added.returncode != 0:
            detail = added.stderr.strip() or added.stdout.strip() or "no output"
            return {
                "result": "failed",
                "reason": "worktree_create_failed",
                "detail": detail,
            }
        common.write_json_atomically(
            record_path,
            {
                "run_id": self.run_id,
                "repository": self.repository,
                "number": number,
                "path": str(path),
                "head_sha": request["head_sha"],
                "status": "active",
                "created_at": utc_now(),
            },
        )
        return {"result": "ready", "worktree": path, "reused": False}

    def verify(self, request: dict[str, Any], worktree: Path) -> dict[str, Any]:
        record = common.read_json(worktree_ownership_path(self.run_directory, request["number"]))
        if not owns_worktree(record, self.run_id, worktree):
            return {"result": "failed", "reason": "worktree_ownership_missing"}
        root = common.git_or_none(worktree, "rev-parse", "--show-toplevel")
        if root is None or Path(root).resolve() != worktree.resolve():
            return {
                "result": "failed",
                "reason": "worktree_root_mismatch",
                "detail": f"{root} is not {worktree}",
            }
        remote = common.git_or_none(worktree, "remote", "get-url", "origin") or ""
        name = common.github_repo_from_remote(remote)
        if name is not None and name.lower() != self.repository.lower():
            return {
                "result": "failed",
                "reason": "worktree_repository_mismatch",
                "detail": f"{name} is not {self.repository}",
            }
        head = common.git_or_none(worktree, "rev-parse", "HEAD")
        if head != request["head_sha"]:
            return {
                "result": "failed",
                "reason": "worktree_head_mismatch",
                "detail": f"{head} is not {request['head_sha']}",
            }
        dirt = common.git_or_none(worktree, "status", "--porcelain=v1")
        if dirt:
            return {
                "result": "failed",
                "reason": "worktree_is_dirty",
                "detail": dirt,
            }
        return {"result": "verified", "head_sha": head}

    def log_path(self, request: dict[str, Any]) -> Path:
        return (
            self.run_directory
            / "logs"
            / f"{request['pass']}-{request['stage']}-{request['number']}.log"
        )

    def record_path(self, request: dict[str, Any]) -> Path:
        return (
            self.run_directory
            / "workers"
            / f"{request['pass']}-{request['stage']}-{request['number']}.json"
        )

    def start(self, request: dict[str, Any], worktree: Path) -> dict[str, Any]:
        entry = STAGE_BY_NAME[request["stage"]]
        target = common.target_for(self.repository, request["number"])
        command = common.stage_command(
            entry,
            target,
            model=self.models[request["stage"]],
            effort=self.effort,
            arguments=request["arguments"],
            repo_root=worktree,
        )
        log_path = self.log_path(request)
        record_path = self.record_path(request)
        try:
            handle = common.start_background(
                command, cwd=worktree, log_path=log_path
            )
        except OSError as error:
            return {
                "result": "failed",
                "reason": "worker_start_failed",
                "detail": str(error),
            }
        common.write_json_atomically(
            record_path,
            {
                "run_id": self.run_id,
                "nonce": request["nonce"],
                "number": request["number"],
                "stage": request["stage"],
                "pass": request["pass"],
                "head_sha": request["head_sha"],
                "pid": handle.pid,
                "worktree": str(worktree),
                "log_path": str(log_path),
                "started_at": utc_now(),
            },
        )
        return {
            "result": "started",
            "handle": handle,
            "pid": handle.pid,
            "log_path": log_path,
            "record_path": record_path,
        }

    def confirm_ready(
        self,
        request: dict[str, Any],
        started: dict[str, Any],
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Confirm process startup without requiring coordinator log output."""
        handle = started["handle"]
        record_path = Path(started["record_path"])
        log_path = Path(started["log_path"])
        deadline = self.monotonic() + self.readiness_timeout
        while True:
            if should_cancel is not None and should_cancel():
                return {"result": "cancelled"}
            exited = handle.poll()
            if common._EXECUTION is not None:
                child_handle = Path(handle.launch_receipt["handle"])
                if child_handle.is_file():
                    execution = common._EXECUTION.child_status(child_handle)
                    if execution.get("terminal") is True:
                        if execution.get("exit_code") != 0:
                            return {"result": "failed", "reason": "worker_execution_failed",
                                    "detail": json.dumps(execution, sort_keys=True)}
                        return {"result": "active", "evidence": execution}
                    if execution.get("status") == "ready":
                        return {"result": "active", "evidence": execution}
                if exited is not None:
                    return {"result": "failed", "reason": "worker_exited_before_readiness",
                            "detail": "worker exited without its own readiness or terminal evidence"}
                if self.monotonic() >= deadline:
                    return {"result": "failed", "reason": "worker_readiness_timeout",
                            "detail": "worker did not write run-bound readiness"}
                self.sleep(self.poll_interval)
                continue
            log_size = log_path.stat().st_size if log_path.exists() else 0
            if record_path.is_file() and exited in {None, 0}:
                return {
                    "result": "active",
                    "evidence": {
                        "record_path": str(record_path),
                        "log_path": str(log_path),
                        "log_bytes": log_size,
                        "pid": started["pid"],
                        "exited": exited,
                        "observed_at": utc_now(),
                    },
                }
            if exited is not None:
                return {
                    "result": "failed",
                    "reason": "worker_exited_before_readiness",
                    "detail": f"the worker exited with {exited} during startup",
                }
            if self.monotonic() >= deadline:
                return {
                    "result": "failed",
                    "reason": "worker_readiness_timeout",
                    "detail": (
                        "no durable readiness evidence within "
                        f"{self.readiness_timeout} seconds"
                    ),
                }
            self.sleep(self.poll_interval)

    def cancel(self, started: dict[str, Any]) -> dict[str, Any]:
        handle = started["handle"]
        returncode = common.terminate_process_tree(handle)
        record_path = Path(
            started.get("record_path")
            or (started.get("evidence") or {}).get("record_path")
            or ""
        )
        record = common.read_json(record_path)
        if isinstance(record, dict):
            common.write_json_atomically(
                record_path,
                {**record, "status": "cancelled", "ended_at": utc_now()},
            )
        return {"returncode": returncode, "ended_at": utc_now()}

    def is_running(self, worker: dict[str, Any]) -> bool:
        return worker["handle"].poll() is None

    def wait(self, worker: dict[str, Any]) -> dict[str, Any]:
        returncode = worker["handle"].wait()
        return {"returncode": returncode, "ended_at": utc_now()}

    def cleanup(self, number: int) -> dict[str, Any]:
        path = worktree_path(self.worktree_root, number)
        record_path = worktree_ownership_path(self.run_directory, number)
        record = common.read_json(record_path)
        if not path.exists():
            if removed_worktree(record, self.run_id, path):
                return {
                    "result": "removed",
                    "number": number,
                    "ownership_record": str(record_path),
                    "already_removed": True,
                }
            return {"result": "absent", "number": number}
        if not owns_worktree(record, self.run_id, path):
            return {
                "result": "failed",
                "reason": "worktree_is_not_owned_by_this_run",
                "number": number,
            }
        preserved = {
            "result": "preserved",
            "number": number,
            "worktree": str(path),
            "ownership_record": str(record_path),
        }
        try:
            status = common.run(
                [
                    "git", "-C", str(path), "status",
                    "--porcelain=v1", "--untracked-files=all",
                ],
                check=False,
            )
        except (WorkflowError, OSError, subprocess.SubprocessError) as error:
            return {
                **preserved,
                "reason": "worktree_status_unavailable",
                "detail": str(error),
            }
        if status.returncode != 0:
            return {
                **preserved,
                "reason": "worktree_status_unavailable",
                "detail": status.stderr.strip() or status.stdout.strip() or "no output",
            }
        if status.stdout.strip():
            return {
                **preserved,
                "reason": "worktree_is_dirty",
                "detail": status.stdout.strip(),
            }
        try:
            removed = common.run(
                ["git", "-C", str(self.repo_root), "worktree", "remove", str(path)],
                check=False,
            )
        except (WorkflowError, OSError, subprocess.SubprocessError) as error:
            return {
                **preserved,
                "reason": "worktree_remove_failed",
                "detail": str(error),
            }
        if removed.returncode == 0 and not path.exists():
            try:
                common.write_json_atomically(
                    record_path,
                    {
                        **record,
                        "status": "removed",
                        "removed_at": utc_now(),
                    },
                )
            except (OSError, TypeError, ValueError) as error:
                return {
                    **preserved,
                    "result": "failed",
                    "reason": "ownership_record_update_failed",
                    "detail": str(error),
                    "worktree_removed": True,
                }
            try:
                self.worktree_root.rmdir()
            except OSError:
                pass
            return {
                "result": "removed",
                "number": number,
                "ownership_record": str(record_path),
            }
        return {
            **preserved,
            **(
                {"result": "failed", "worktree_removed": True}
                if not path.exists()
                else {}
            ),
            "reason": "worktree_remove_failed",
            "detail": removed.stderr.strip() or removed.stdout.strip() or "no output",
        }


def launch_workers(
    requests: list[dict[str, Any]],
    *,
    launcher: Any,
    report: Callable[[dict[str, Any]], None] | None = None,
    on_started: Callable[[dict[str, Any]], None] | None = None,
    on_active: Callable[[dict[str, Any]], None] | None = None,
    on_stopped: Callable[[dict[str, Any]], None] | None = None,
    on_created: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Start workers strictly one at a time, verifying each before the next.

    A failure to create, verify, or start a worker stops every later launch in
    this dispatch. Nothing is retried and no replacement worker is created,
    because a second attempt could leave two agents working the same pull
    request. Workers that are already active keep running.
    """
    workers: list[dict[str, Any]] = []
    stopped: dict[str, Any] | None = None
    cancelled = False
    for request in requests:
        if should_cancel is not None and should_cancel():
            cancelled = True
            break
        report_safely(
            report,
            "worker_starting",
            number=request["number"],
            stage=request["stage"],
            pull_request_pass=request["pass"],
        )
        created = launcher.create(request)
        if created.get("result") != "ready":
            stopped = {"step": "create", "number": request["number"], **created}
            break
        if on_created is not None:
            on_created(request)
        if should_cancel is not None and should_cancel():
            cancelled = True
            break
        worktree = created["worktree"]
        verified = launcher.verify(request, worktree)
        if verified.get("result") != "verified":
            stopped = {"step": "verify", "number": request["number"], **verified}
            break
        if should_cancel is not None and should_cancel():
            cancelled = True
            break
        started = launcher.start(request, worktree)
        if started.get("result") != "started":
            stopped = {"step": "start", "number": request["number"], **started}
            break
        worker = {
            "number": request["number"],
            "stage": request["stage"],
            "pass": request["pass"],
            "nonce": request["nonce"],
            "head_sha": request["head_sha"],
            "worktree": str(worktree),
            "handle": started["handle"],
            "pid": started.get("pid"),
            "log_path": str(started.get("log_path", "")),
            "record_path": str(started.get("record_path", "")),
            "evidence": {
                "record_path": str(started.get("record_path", "")),
                "pid": started.get("pid"),
                "observed_at": utc_now(),
            },
            "started_at": utc_now(),
        }
        if on_started is not None:
            on_started(worker)
        if should_cancel is not None and should_cancel():
            launcher.cancel(worker)
            if on_stopped is not None:
                on_stopped(worker)
            report_safely(
                report,
                "worker_cancelled",
                number=worker["number"],
                stage=worker["stage"],
                pull_request_pass=worker["pass"],
            )
            cancelled = True
            break
        ready = launcher.confirm_ready(
            request, started, should_cancel=should_cancel
        )
        if ready.get("result") == "cancelled":
            launcher.cancel(worker)
            if on_stopped is not None:
                on_stopped(worker)
            report_safely(
                report,
                "worker_cancelled",
                number=worker["number"],
                stage=worker["stage"],
                pull_request_pass=worker["pass"],
            )
            cancelled = True
            break
        if ready.get("result") != "active":
            launcher.cancel(started)
            if on_stopped is not None:
                on_stopped(worker)
            stopped = {"step": "readiness", "number": request["number"], **ready}
            break
        worker["evidence"] = ready.get("evidence")
        workers.append(worker)
        if on_active is not None:
            on_active(worker)
        report_safely(
            report,
            "worker_active",
            number=worker["number"],
            stage=worker["stage"],
            pull_request_pass=worker["pass"],
            pid=worker["pid"],
            head_sha=worker["head_sha"],
        )
        if should_cancel is not None and should_cancel():
            cancelled = True
            break
    if stopped is not None:
        stopped.pop("handle", None)
        report_safely(report, "worker_launch_stopped", **stopped)
    return {"workers": workers, "stopped": stopped, "cancelled": cancelled}


def accepted_push_checkpoints(
    repository: str,
    number: int,
    *,
    run_id: str,
    state_for: Callable[..., Path] = stage_state_path,
) -> list[dict[str, Any]]:
    """Read the CI stage's own record of the pushes it has published."""
    target = common.target_for(repository, number)
    payload = common.read_json(
        state_for(STAGE_BY_NAME[STAGE_CI], target, run_id)
    )
    pushes = payload.get("accepted_pushes") if isinstance(payload, dict) else None
    return [push for push in pushes or [] if isinstance(push, dict)]


def worker_live_progress(
    repository: str, number: int, stage: str, *, run_id: str
) -> dict[str, Any] | None:
    target = common.target_for(repository, number)
    return common.stage_live_progress(
        STAGE_BY_NAME[stage],
        target,
        state_for=lambda entry, current: stage_state_path(
            entry, current, run_id
        ),
    )


def propagate_descendants(
    repository: str,
    number: int,
    head_sha: str,
    stack_number: int,
    *,
    request_path: Path,
    state_path: Path,
    script_for: Callable[[dict[str, Any]], Path] = stage_script_path,
    runner: Callable[..., Any] = common.run,
) -> dict[str, Any]:
    """Ask the conflict plugin to carry one accepted push up the stack.

    Rebasing descendants is the conflict plugin's job. This helper never grows
    its own rebase engine, so a push that lands mid-run is propagated by the
    same code that resolves conflicts everywhere else.
    """
    script = script_for(STAGE_BY_NAME[STAGE_CONFLICT])
    if not script.is_file():
        return {
            "result": "unavailable",
            "reason": "plugin_not_installed",
            "script": str(script),
        }
    process = runner(
        [
            sys.executable,
            str(script),
            CONFLICT_PROPAGATE_COMMAND,
            "--repo",
            repository,
            "--pull-request",
            str(number),
            "--head-sha",
            head_sha,
            "--stack-number",
            str(stack_number),
            "--stack-request",
            str(request_path),
            "--state",
            str(state_path),
        ],
        check=False,
    )
    if process.returncode != 0:
        detail = (process.stderr or "").strip() or (process.stdout or "").strip()
        return {
            "result": "failed",
            "reason": "propagate_failed",
            "detail": detail or "no output",
            "head_sha": head_sha,
        }
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError:
        return {
            "result": "failed",
            "reason": "invalid_propagation_result",
            "detail": (process.stdout or "").strip() or "no output",
            "head_sha": head_sha,
        }
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), str):
        return {
            "result": "failed",
            "reason": "invalid_propagation_result",
            "detail": "the conflict helper did not return a result object",
            "head_sha": head_sha,
        }
    return {**payload, "head_sha": head_sha, "number": number}


class StackPipeline:
    """Orchestrate one stack run: who runs, where, in which order, and when.

    Stage behavior belongs to the stage agents. This class only decides which
    agent is dispatched, keeps the run's durable state, and folds results back
    in when their nonce and head still match.
    """

    def __init__(
        self,
        kickoff: dict[str, Any],
        repo_root: Path,
        *,
        models: dict[str, str],
        effort: str,
        conflict_strategy: str = "auto",
        github_mutation_policy: str = "allow",
        run_id: str | None = None,
        report: Callable[[dict[str, Any]], None] | None = None,
        launcher: Any | None = None,
        state_path: Path | None = None,
        run_directory: Path | None = None,
        result_path: Path | None = None,
        read_stack: Callable[..., dict[str, Any] | None] = read_native_stack,
        inspect: Callable[..., dict[str, Any]] = inspect_stage,
        base_tip: Callable[[str, str], str] = base_ref_tip,
        contains: Callable[[Path, str, str], bool] | None = None,
        checkpoints: Callable[..., list[dict[str, Any]]] = accepted_push_checkpoints,
        worker_progress: Callable[[str, int, str], dict[str, Any] | None] = worker_live_progress,
        propagate: Callable[..., dict[str, Any]] = propagate_descendants,
        dependencies: Callable[[], list[str]] = missing_dependencies,
        sleep: Callable[[float], None] = time.sleep,
        monitor_interval: float = MONITOR_POLL_INTERVAL,
        nonces: Callable[[], str] | None = None,
    ) -> None:
        self.kickoff = kickoff
        self.repository = kickoff["repository"]
        self.repo_root = repo_root
        self.models = models
        self.effort = effort
        self.conflict_strategy = conflict_strategy
        if github_mutation_policy not in {"allow", "source-only"}:
            raise WorkflowError(
                "github_mutation_policy must be 'allow' or 'source-only'"
            )
        self.github_mutation_policy = github_mutation_policy
        common.ACTIVE_GITHUB_MUTATION_POLICY = github_mutation_policy
        self.run_id = run_id or uuid.uuid4().hex
        self.report = report
        self.state_path = state_path or state_path_for(kickoff, self.run_id)
        self.run_directory = run_directory or run_directory_for(kickoff, self.run_id)
        self.cancellation_path = self.run_directory / "cancel-request.json"
        self.result_path = result_path or (self.run_directory / "result.json")
        self.launcher = launcher or WorkerLauncher(
            repo_root=repo_root,
            repository=self.repository,
            run_id=self.run_id,
            run_directory=self.run_directory,
            models=models,
            effort=effort,
        )
        self.read_stack = read_stack
        self.inspect = (
            (
                lambda entry, target, head, base: inspect_stage(
                    entry, target, head, base, self.run_id
                )
            )
            if inspect is inspect_stage
            else inspect
        )
        self.base_tip = base_tip
        self.contains = contains or (
            lambda _root, ancestor, descendant: commit_contains(
                self.repository, ancestor, descendant
            )
        )
        self.checkpoints = (
            (
                lambda repository, number: checkpoints(
                    repository, number, run_id=self.run_id
                )
            )
            if checkpoints is accepted_push_checkpoints
            else checkpoints
        )
        self.worker_progress = (
            (
                lambda repository, number, stage: worker_progress(
                    repository, number, stage, run_id=self.run_id
                )
            )
            if worker_progress is worker_live_progress
            else worker_progress
        )
        self.propagate = propagate
        self.dependencies = dependencies
        self.sleep = sleep
        self.monitor_interval = monitor_interval
        self.nonces = nonces or (lambda: uuid.uuid4().hex)
        self.state: dict[str, Any] = {}
        self.touched: set[int] = set()
        self.propagations: list[dict[str, Any]] = []
        self.session_title: str | None = None
        self.running_workers: dict[str, dict[str, Any]] = {}
        self.worker_requests: dict[str, dict[str, Any]] = {}
        self.progress_signatures: dict[str, str] = {}
        self.completed_phases: list[dict[str, Any]] = []
        self.completed_passes = 0

    # State ---------------------------------------------------------------

    def emit(self, event: str, **fields: Any) -> None:
        report_safely(self.report, event, run_id=self.run_id, **fields)

    def save(self) -> None:
        save_state(self.state_path, self.state)

    def record_stage(
        self, number: int, stage: str, payload: dict[str, Any]
    ) -> None:
        pull_requests = self.state.setdefault("pull_requests", {})
        record = pull_requests.setdefault(str(number), {"stages": {}})
        previous = record["stages"].get(stage, {})
        superseded = list(previous.get("superseded_candidates") or [])
        stage_result = payload.get("stage_result")
        if (
            isinstance(stage_result, dict)
            and stage_result.get("source_drift") is not None
            and stage_result not in superseded
        ):
            superseded.append(stage_result)
        record["stages"][stage] = {
            **payload,
            **({"superseded_candidates": superseded} if superseded else {}),
            "updated_at": utc_now(),
        }
        self.save()

    def cancellation_requested(self) -> bool:
        if common._EXECUTION is not None:
            common._EXECUTION.check_cancel()
        return False

    def check_cancellation(self) -> None:
        if self.cancellation_requested():
            raise PipelineCancelled()

    def worker_progress_update(
        self, worker: dict[str, Any], request: dict[str, Any]
    ) -> None:
        progress = self.worker_progress(
            self.repository, request["number"], request["stage"]
        )
        if progress is None:
            return
        signature = json.dumps(progress, sort_keys=True)
        nonce = worker["nonce"]
        if signature == self.progress_signatures.get(nonce):
            return
        self.progress_signatures[nonce] = signature
        self.emit(
            "worker_progress",
            number=request["number"],
            stage=request["stage"],
            pull_request_pass=request["pass"],
            **progress,
        )

    def cancel_active_workers(self) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        self.emit(
            "stack_pipeline_cancelling",
            numbers=sorted(worker["number"] for worker in self.running_workers.values()),
        )
        for nonce, worker in list(self.running_workers.items()):
            request = self.worker_requests[nonce]
            if not self.launcher.is_running(worker):
                self.finish_worker(worker, request, announce_wait=False)
                continue
            try:
                finished = self.launcher.cancel(worker)
            except (OSError, subprocess.SubprocessError) as error:
                errors.append(
                    {
                        "number": worker["number"],
                        "stage": worker["stage"],
                        "error": str(error),
                    }
                )
                continue
            self.emit(
                "worker_cancelled",
                number=worker["number"],
                stage=worker["stage"],
                pull_request_pass=request["pass"],
                returncode=finished.get("returncode"),
            )
            self.remove_active_worker(worker)
        return errors

    # Topology ------------------------------------------------------------

    def revalidate(
        self, *, require_initial_source_snapshot: bool = False
    ) -> dict[str, Any]:
        stack = self.read_stack(self.repository, self.kickoff["startPullRequest"])
        if stack is not None:
            start = next(
                (
                    member
                    for member in stack["members"]
                    if member["number"] == self.kickoff["startPullRequest"]
                ),
                None,
            )
            if start is not None:
                self.session_title = session_title(self.kickoff, start["title"])
        validation = validate_selection(self.kickoff, stack)
        if validation["result"] != "ready":
            return validation
        if require_initial_source_snapshot:
            _, source_snapshot = stack_source_identity(stack)
            expected_snapshot = self.kickoff.get("sourceSnapshot")
            if (
                isinstance(expected_snapshot, str)
                and source_snapshot != expected_snapshot
            ):
                return {
                    "result": "stopped",
                    "reason": "source_snapshot_changed_before_start",
                    "detail": (
                        "the native stack source changed after target discovery"
                    ),
                }
        return {**validation, "stack": stack}

    def base_sha_for(self, member: dict[str, Any]) -> str | None:
        try:
            return self.base_tip(self.repository, member["base_branch"])
        except WorkflowError:
            return None

    def live_head_for(self, number: int) -> str | None:
        current = self.revalidate()
        if (
            current["result"] != "ready"
            or current["fingerprint"] != self.state.get("topology_fingerprint")
        ):
            return None
        member = next(
            (entry for entry in current["selected"] if entry["number"] == number),
            None,
        )
        return None if member is None else member["head_sha"]

    def authorize_stack_publication(
        self, number: int, head_sha: str, *, operation: str, pass_number: int = 0
    ) -> tuple[Path, Path]:
        self.check_cancellation()
        current = self.revalidate()
        if (
            current["result"] != "ready"
            or current["fingerprint"] != self.state.get("topology_fingerprint")
        ):
            raise WorkflowError("stack publication authorization changed")
        source, source_snapshot = stack_source_identity(current["stack"])
        fixed = next((member for member in source["members"] if member["number"] == number), None)
        if fixed is None or fixed["head_sha"] != head_sha:
            raise WorkflowError("fixed head changed before stack publication authorization")
        name = (
            f"propagate-pr-{number}-{head_sha}" if operation == "descendant-propagation"
            else f"conflict-pass-{pass_number}-pr-{number}"
        )
        state_path = (self.run_directory / f"{name}.json").resolve()
        request_path = state_path.with_name(f"{name}--request.json")
        request = {
            "schema": {"id": "github.copilot.stack-publication-request", "version": 1},
            "operation": operation,
            "request_id": f"{self.run_id}-{name}",
            "request_sha256": "",
            "owner": {
                "kind": RUN_KIND, "run_id": self.run_id,
                "state": str(self.state_path.resolve()),
                "cancellation": str(self.cancellation_path.resolve()),
            },
            "repository": self.repository.lower(),
            "selected": list(self.kickoff["pullRequests"]),
            "topology_fingerprint": self.state["topology_fingerprint"],
            "source_stack": source,
            "source_snapshot": source_snapshot,
            "fixed_pr": number,
            "fixed_head": head_sha,
            "state": str(state_path),
        }
        request["request_sha256"] = hashlib.sha256(
            json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if request_path.exists():
            if common.read_json(request_path) != request:
                raise WorkflowError("the run's stack publication request changed")
        else:
            common.write_json_atomically(request_path, request)
        self.state.setdefault("stack_requests", {})[request["request_id"]] = request["request_sha256"]
        self.state["stack_owner_pid"] = os.getpid()
        self.state["stack_owner_recorded_at"] = time.time()
        self.save()
        return request_path, state_path

    def propagate_authorized(self, number: int, head_sha: str) -> dict[str, Any]:
        request_path, state_path = self.authorize_stack_publication(
            number, head_sha, operation="descendant-propagation"
        )
        return self.propagate(
            self.repository, number, head_sha, self.kickoff["stackNumber"],
            request_path=request_path, state_path=state_path,
        )

    # Dispatch ------------------------------------------------------------

    def request_for(
        self,
        member: dict[str, Any],
        stage: str,
        pass_number: int,
        *,
        scope: str | None = None,
    ) -> dict[str, Any]:
        entry = STAGE_BY_NAME[stage]
        target = common.target_for(self.repository, member["number"])
        arguments = common.pipeline_arguments(
            entry,
            self.run_id,
            pass_number,
            MAX_PASSES,
            accepts=common.stage_accepts_pipeline_position,
        )
        arguments.extend(
            [
                "--state",
                str(stage_state_path(entry, target, self.run_id)),
            ]
        )
        if stage == STAGE_CONFLICT:
            arguments.extend(
                [
                    "--strategy",
                    self.conflict_strategy,
                ]
            )
            if scope is not None:
                arguments.append("--whole-stack")
                request_path, _ = self.authorize_stack_publication(
                    member["number"], member["head_sha"],
                    operation="whole-stack", pass_number=pass_number,
                )
                arguments.extend(["--stack-request", str(request_path)])
        return {
            "number": member["number"],
            "stage": stage,
            "agent": entry["agent"],
            "pass": pass_number,
            "nonce": self.nonces(),
            "head_sha": member["head_sha"],
            "base_sha": self.base_sha_for(member),
            "arguments": arguments,
            "prompt": worker_prompt(target, arguments, scope=scope),
        }

    def dispatch(
        self, requests: list[dict[str, Any]], phase: str, pass_number: int
    ) -> dict[str, Any]:
        for request in requests:
            entry = STAGE_BY_NAME[request["stage"]]
            if entry.get("base_marker") is not None and request.get("base_sha") is None:
                stopped = {
                    "step": "stage_status",
                    "number": request["number"],
                    "stage": request["stage"],
                    "reason": "base_status_unavailable",
                    "detail": "the live base revision could not be read before launch",
                }
                self.emit(
                    "worker_launch_stopped",
                    number=request["number"],
                    stage=request["stage"],
                    pull_request_pass=pass_number,
                    step=stopped["step"],
                    reason=stopped["reason"],
                    status="blocked",
                )
                return {"workers": [], "stopped": stopped}
            stage_result = self.clearance(
                request["number"],
                request["stage"],
                request["head_sha"],
                request.get("base_sha"),
            )
            blocker = common.stage_blocker(
                stage_result,
                after_launch=False,
                conflict_strategy=self.conflict_strategy,
            )
            if blocker is None:
                continue
            reason, detail = blocker
            stopped = {
                "step": "stage_status",
                "number": request["number"],
                "stage": request["stage"],
                "reason": reason,
                "detail": detail,
                "stage_result": stage_result_summary(stage_result),
            }
            self.emit(
                "worker_launch_stopped",
                number=request["number"],
                stage=request["stage"],
                pull_request_pass=pass_number,
                step=stopped["step"],
                reason=reason,
                status="blocked",
            )
            return {"workers": [], "stopped": stopped}
        self.state["phase"] = phase
        self.state["dispatch"] = {
            "phase": phase,
            "pass": pass_number,
            "nonces": {
                str(request["number"]): request["nonce"] for request in requests
            },
            "heads": {
                str(request["number"]): request["head_sha"] for request in requests
            },
            "dispatched_at": utc_now(),
        }
        self.save()
        for request in requests:
            self.worker_requests[request["nonce"]] = request

        def record_created(request: dict[str, Any]) -> None:
            self.touched.add(request["number"])

        def record_started(worker: dict[str, Any]) -> None:
            self.touched.add(worker["number"])
            self.running_workers[worker["nonce"]] = worker
            self.state.setdefault("active_workers", []).append(
                {
                    "nonce": worker["nonce"],
                    "number": worker["number"],
                    "stage": worker["stage"],
                    "pid": worker["pid"],
                    "head_sha": worker["head_sha"],
                    "pass": pass_number,
                    "ready_at": worker.get("ready_at", utc_now()),
                }
            )
            self.save()

        def record_active(worker: dict[str, Any]) -> None:
            for active in self.state.get("active_workers", []):
                if active.get("nonce") == worker["nonce"]:
                    active["ready_at"] = utc_now()
                    active["evidence"] = worker.get("evidence")
            self.save()

        def record_stopped(worker: dict[str, Any]) -> None:
            self.remove_active_worker(worker)

        launched = launch_workers(
            requests,
            launcher=self.launcher,
            report=self.report,
            on_started=record_started,
            on_active=record_active,
            on_stopped=record_stopped,
            on_created=record_created,
            should_cancel=self.cancellation_requested,
        )
        if launched["cancelled"]:
            raise PipelineCancelled()
        return launched

    def remove_active_worker(self, worker: dict[str, Any]) -> None:
        nonce = worker["nonce"]
        self.running_workers.pop(nonce, None)
        self.worker_requests.pop(nonce, None)
        self.progress_signatures.pop(nonce, None)
        self.state["active_workers"] = [
            active
            for active in self.state.get("active_workers", [])
            if active.get("nonce") != nonce
        ]
        self.save()

    def finish_worker(
        self,
        worker: dict[str, Any],
        request: dict[str, Any],
        *,
        announce_wait: bool = True,
    ) -> dict[str, Any]:
        if announce_wait:
            self.emit(
                "worker_wait_started",
                number=worker["number"],
                stage=worker["stage"],
                pull_request_pass=request["pass"],
            )
        finished = self.launcher.wait(worker)
        completion = {
            "number": worker["number"],
            "stage": worker["stage"],
            "nonce": worker["nonce"],
            "head_sha": worker["head_sha"],
            **finished,
        }
        expected_nonce = request["nonce"]
        expected_head = request["head_sha"]
        completion["accepted"] = accept_completion(
            completion,
            expected_nonce=expected_nonce,
            expected_head_sha=expected_head,
        )
        self.remove_active_worker(worker)
        stage_result = self.current_stage_result(request)
        blocker = common.stage_blocker(
            stage_result,
            after_launch=True,
            conflict_strategy=self.conflict_strategy,
        )
        if completion.get("returncode") != 0:
            blocker = (
                "stage_execution_failed",
                f"{request['stage']} exited with code {completion.get('returncode')}",
            )
        if blocker is None and stage_result.get("control_reason") == "topology_changed":
            blocker = ("topology_changed", stage_result["detail"])
        if (
            blocker is None
            and request["stage"] in {STAGE_CONFLICT, STAGE_DESCRIPTION}
            and stage_result.get("outcome") is None
            and stage_result.get("source_drift") is None
        ):
            label = (
                "conflict" if request["stage"] == STAGE_CONFLICT else "description"
            )
            blocker = (
                f"{label}_did_not_record_outcome",
                (
                    f"{request['stage']} returned without recording a terminal outcome; "
                    "stage completion cannot be verified"
                ),
            )
        completion["stage_result"] = stage_result_summary(stage_result)
        completion["clear"] = bool(stage_result.get("clear"))
        completion["reason"] = stage_result.get("reason")
        completion["current_head_sha"] = stage_result.get("inspected_head_sha")
        completion["current_base_sha"] = stage_result.get("inspected_base_sha")
        if blocker is not None:
            completion["blocking_reason"], completion["blocking_detail"] = blocker
        self.emit(
            "worker_finished",
            number=completion["number"],
            stage=completion["stage"],
            pull_request_pass=request["pass"],
            returncode=completion.get("returncode"),
            accepted=completion["accepted"],
            clear=completion["clear"],
            reason=completion.get("reason"),
            blocking_reason=completion.get("blocking_reason"),
            clear_at_head_sha=completion["stage_result"].get("clear_at_head_sha"),
            clear_at_base_sha=completion["stage_result"].get("clear_at_base_sha"),
            current_head_sha=completion.get("current_head_sha"),
            current_base_sha=completion.get("current_base_sha"),
            clearance_kind=stage_result.get("clearance_kind"),
            **common.ci_warning_fields([stage_result]),
            status=(
                "blocked"
                if blocker is not None
                else "clearance_verified"
                if completion["clear"]
                else "result_collected"
            ),
        )
        return completion

    def current_stage_result(self, request: dict[str, Any]) -> dict[str, Any]:
        current = self.revalidate()
        if (
            current["result"] != "ready"
            or current["fingerprint"] != self.state.get("topology_fingerprint")
        ):
            return {
                "stage": request["stage"],
                "clear": False,
                "outcome": None,
                "reason": "status_not_ready",
                "detail": "the live stack identity could not be revalidated after the worker exited",
                "control_reason": "topology_changed",
                "status_state": str(
                    stage_state_path(
                        STAGE_BY_NAME[request["stage"]],
                        common.target_for(self.repository, request["number"]),
                        self.run_id,
                    )
                ),
                "status": {},
            }
        member = next(
            (
                entry
                for entry in current["selected"]
                if entry["number"] == request["number"]
            ),
            None,
        )
        if member is None:
            return {
                "stage": request["stage"],
                "clear": False,
                "outcome": None,
                "reason": "status_not_ready",
                "detail": "the pull request is no longer in the selected stack",
                "control_reason": "topology_changed",
                "status_state": str(
                    stage_state_path(
                        STAGE_BY_NAME[request["stage"]],
                        common.target_for(self.repository, request["number"]),
                        self.run_id,
                    )
                ),
                "status": {},
            }
        head_sha = member["head_sha"]
        base_sha = self.base_sha_for(member)
        return {
            **self.clearance(request["number"], request["stage"], head_sha, base_sha),
            "inspected_head_sha": head_sha,
            "inspected_base_sha": base_sha,
        }

    def collect_parallel_workers(
        self, workers: list[dict[str, Any]], requests: dict[int, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        pending = list(workers)
        completions: list[dict[str, Any]] = []
        order = {worker["number"]: index for index, worker in enumerate(workers)}
        for worker in pending:
            self.emit(
                "worker_wait_started",
                number=worker["number"],
                stage=worker["stage"],
                pull_request_pass=requests[worker["number"]]["pass"],
            )
        while pending:
            finished = [
                worker for worker in pending if not self.launcher.is_running(worker)
            ]
            for worker in finished:
                request = requests[worker["number"]]
                completions.append(
                    self.finish_worker(worker, request, announce_wait=False)
                )
                pending.remove(worker)
            if not pending:
                break
            self.check_cancellation()
            for worker in pending:
                self.worker_progress_update(worker, requests[worker["number"]])
            self.sleep(self.monitor_interval)
        return sorted(completions, key=lambda item: order[item["number"]])

    def monitor_worker(
        self, worker: dict[str, Any], request: dict[str, Any]
    ) -> dict[str, Any]:
        self.emit(
            "worker_wait_started",
            number=worker["number"],
            stage=worker["stage"],
            pull_request_pass=request["pass"],
        )
        while self.launcher.is_running(worker):
            self.check_cancellation()
            self.worker_progress_update(worker, request)
            self.sleep(self.monitor_interval)
        return self.finish_worker(worker, request, announce_wait=False)

    def clearance(
        self, number: int, stage: str, head_sha: str, base_sha: str | None
    ) -> dict[str, Any]:
        target = common.target_for(self.repository, number)
        return self.inspect(STAGE_BY_NAME[stage], target, head_sha, base_sha)

    def mergeability_clearance(
        self, member: dict[str, Any], base_sha: str | None
    ) -> dict[str, Any]:
        clear = (
            member.get("state") == "OPEN"
            and member.get("mergeable") == "MERGEABLE"
            and base_sha is not None
            and member.get("base_sha") == base_sha
        )
        return {
            "stage": STAGE_CONFLICT,
            "clear": clear,
            "clear_at_head_sha": member["head_sha"] if clear else None,
            "clear_at_base_sha": base_sha if clear else None,
            "clearance_kind": "github_mergeability" if clear else None,
            "outcome": "cleared" if clear else None,
            "reason": None if clear else "unsupported_partial_selection_conflict",
            "installed": stage_script_path(STAGE_BY_NAME[STAGE_CONFLICT]).is_file(),
            "status_state": None,
            "status": {
                "mergeable": member.get("mergeable"),
                "head_sha": member["head_sha"],
                "base_sha": member.get("base_sha"),
            },
        }

    # Phases --------------------------------------------------------------

    def run_conflict_phase(
        self, pass_number: int, selected: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Permit whole-stack publication only for a whole-stack selection."""
        current = self.revalidate()
        if (
            current["result"] != "ready"
            or current["fingerprint"] != self.state["topology_fingerprint"]
        ):
            raise WorkflowError("stack selection changed before conflict dispatch")
        whole_stack = [
            member["number"] for member in current["stack"]["members"]
        ] == self.kickoff["pullRequests"]
        if not whole_stack:
            results = [
                (
                    member,
                    self.mergeability_clearance(member, self.base_sha_for(member)),
                )
                for member in current["selected"]
            ]
            blocked = next(
                (member for member, result in results if not result["clear"]), None
            )
            result = {
                "phase": STAGE_CONFLICT,
                "mode": PHASE_STACK_DISPATCH,
                "dispatches": 0,
                "completions": [],
                "clear": blocked is None,
                "completed": blocked is None,
                "action": "mergeability_checked",
                "stopped": None if blocked is None else {
                    "step": "conflict_scope",
                    "number": blocked["number"],
                    "stage": STAGE_CONFLICT,
                    "reason": "unsupported_partial_selection_conflict",
                    "detail": (
                        "the selected suffix is not freshly mergeable at its exact "
                        "head and base; conflict publication would require "
                        "authorization for the full native stack"
                    ),
                },
            }
            self.emit(
                "phase_finished", pull_request_pass=pass_number,
                numbers=[member["number"] for member in current["selected"]],
                **summarize_phase(result),
            )
            return result
        clicked = next(
            member
            for member in selected
            if member["number"] == self.kickoff["startPullRequest"]
        )
        scope = (
            f"This pull request is the clicked member of native stack "
            f"{self.kickoff['stackNumber']}. Resolve the stack as a whole; the "
            "cascade may move members below it. Run PR Conflict Resolver preflight "
            "with --whole-stack."
        )
        requests = [self.request_for(clicked, STAGE_CONFLICT, pass_number, scope=scope)]
        by_number = {request["number"]: request for request in requests}
        self.emit(
            "phase_started",
            phase=STAGE_CONFLICT,
            pull_request_pass=pass_number,
            numbers=list(by_number),
            mode=PHASE_STACK_DISPATCH,
        )
        launched = self.dispatch(requests, STAGE_CONFLICT, pass_number)
        completions = [
            self.monitor_worker(worker, by_number[worker["number"]])
            for worker in launched["workers"]
        ]
        for completion in completions:
            self.record_stage(
                completion["number"],
                STAGE_CONFLICT,
                {
                    "pass": pass_number,
                    "accepted": completion["accepted"],
                    "returncode": completion.get("returncode"),
                    "dispatched_head_sha": completion["head_sha"],
                    "current_head_sha": completion.get("current_head_sha"),
                    "current_base_sha": completion.get("current_base_sha"),
                    "clear": completion.get("clear"),
                    "outcome": completion.get("stage_result", {}).get("outcome"),
                    "reason": completion.get("reason"),
                    "stage_result": completion["stage_result"],
                    "configuration": {
                        "model": self.models[STAGE_CONFLICT],
                        "effort": self.effort,
                        "strategy": self.conflict_strategy,
                        "github_mutation_policy": self.github_mutation_policy,
                    },
                },
            )
        stopped = launched["stopped"] or next(
            (
                {
                    "step": "stage_status",
                    "number": completion["number"],
                    "stage": completion["stage"],
                    "reason": completion["blocking_reason"],
                    "detail": completion["blocking_detail"],
                    "stage_result": completion["stage_result"],
                }
                for completion in completions
                if completion.get("blocking_reason")
            ),
            None,
        )
        result = {
            "phase": STAGE_CONFLICT,
            "mode": PHASE_STACK_DISPATCH,
            "dispatches": len(launched["workers"]),
            "completions": completions,
            "stopped": stopped,
            "completed": (
                any(completion.get("accepted") for completion in completions)
                and all(
                    completion.get("accepted")
                    and completion.get("returncode") == 0
                    and completion.get("clear")
                    for completion in completions
                )
            ),
        }
        self.emit(
            "phase_finished",
            pull_request_pass=pass_number,
            numbers=list(by_number),
            **summarize_phase(result),
        )
        return result

    def run_parallel_phase(
        self, phase: str, pass_number: int, selected: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Run selected members, reusing verified same-run Description clearance.

        Startup stays serialized, so exactly one worktree and one process are
        created and verified at a time. Once every worker is active they work
        concurrently.
        """
        requests = []
        reused = []
        stopped = None
        for member in selected:
            previous = (
                self.state.get("pull_requests", {})
                .get(str(member["number"]), {})
                .get("stages", {})
                .get(phase, {})
            )
            if (
                phase == STAGE_DESCRIPTION
                and previous.get("dispatched_head_sha") == member["head_sha"]
                and (previous.get("clear") or previous.get("outcome") == "cleared")
            ):
                base_sha = self.base_sha_for(member)
                current = self.clearance(
                    member["number"], phase, member["head_sha"], base_sha
                )
                status = current.get("status") or {}
                task = status.get("agent_task") or {}
                prior_result = previous.get("stage_result") or {}
                prior_pass = previous.get("pass")
                if (
                    self.state.get("run_id") == self.run_id
                    and previous.get("accepted") is True
                    and previous.get("returncode") == 0
                    and previous.get("clear") is True
                    and previous.get("outcome") == "cleared"
                    and type(prior_pass) is int
                    and 1 <= prior_pass < pass_number
                    and previous.get("current_head_sha") == member["head_sha"]
                    and base_sha is not None
                    and previous.get("current_base_sha") == base_sha
                    and current.get("clear") is True
                    and current.get("outcome") == "cleared"
                    and common.current_description_verification(status, self.run_id)
                    and type(status.get("pipeline_iteration")) is int
                    and status.get("pipeline_iteration") == prior_pass
                    and type(status.get("pipeline_max_iterations")) is int
                    and status.get("pipeline_max_iterations") == MAX_PASSES
                    and isinstance(status.get("run_id"), str)
                    and bool(status["run_id"])
                    and status["run_id"] == prior_result.get("run_id")
                    and prior_result.get("pipeline_run") == self.run_id
                    and task == prior_result.get("agent_task")
                    and task.get("model") == common.HOSTED_MODEL_FOR_COORDINATOR[self.models[phase]]
                    and task.get("github_mutation_policy") == self.github_mutation_policy
                ):
                    reuse = {
                        "number": member["number"],
                        "source_pass": prior_pass,
                        "head_sha": member["head_sha"],
                        "base_sha": base_sha,
                        "stage_result": stage_result_summary(current),
                    }
                    reused.append(reuse)
                    self.record_stage(
                        member["number"],
                        phase,
                        {
                            **previous,
                            "reused_in_pass": pass_number,
                            "stage_result": reuse["stage_result"],
                        },
                    )
                    continue
                stopped = {
                    "step": "stage_status",
                    "number": member["number"],
                    "stage": phase,
                    "reason": "description_clearance_not_reusable",
                    "detail": (
                        "same-head Description clearance could not be verified for "
                        "this run and its current inputs; no repeat evaluation was launched"
                    ),
                    "stage_result": stage_result_summary(current),
                }
                break
            requests.append(self.request_for(member, phase, pass_number))
        self.emit(
            "phase_started",
            phase=phase,
            pull_request_pass=pass_number,
            numbers=[member["number"] for member in selected],
            mode=PHASE_PARALLEL,
            **(
                {
                    "reused": [item["number"] for item in reused],
                    "dispatch_numbers": (
                        [] if stopped else [request["number"] for request in requests]
                    ),
                }
                if phase == STAGE_DESCRIPTION else {}
            ),
        )
        launched = (
            {"workers": [], "stopped": stopped}
            if stopped
            else self.dispatch(requests, phase, pass_number)
        )
        by_number = {request["number"]: request for request in requests}
        completions = self.collect_parallel_workers(launched["workers"], by_number)
        for completion in completions:
            self.record_stage(
                completion["number"],
                phase,
                {
                    "pass": pass_number,
                    "accepted": completion["accepted"],
                    "returncode": completion.get("returncode"),
                    "dispatched_head_sha": completion["head_sha"],
                    "current_head_sha": completion.get("current_head_sha"),
                    "current_base_sha": completion.get("current_base_sha"),
                    "clear": completion.get("clear"),
                    "outcome": completion.get("stage_result", {}).get("outcome"),
                    "reason": completion.get("reason"),
                    "stage_result": completion["stage_result"],
                },
            )
        stopped = launched["stopped"] or next(
            (
                {
                    "step": "stage_status",
                    "number": completion["number"],
                    "stage": completion["stage"],
                    "reason": completion["blocking_reason"],
                    "detail": completion["blocking_detail"],
                    "stage_result": completion["stage_result"],
                }
                for completion in completions
                if completion.get("blocking_reason")
            ),
            None,
        )
        result = {
            "phase": phase,
            "mode": PHASE_PARALLEL,
            "dispatches": len(launched["workers"]),
            "completions": completions,
            "stopped": stopped,
            **({"reused": reused} if phase == STAGE_DESCRIPTION else {}),
        }
        self.emit(
            "phase_finished",
            pull_request_pass=pass_number,
            numbers=[member["number"] for member in selected],
            **summarize_phase(result),
        )
        return result

    def ci_gate(
        self,
        member: dict[str, Any],
        predecessor: dict[str, Any] | None,
        predecessor_state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Decide whether repairing this member can start yet.

        A higher member starts once its predecessor has current CI clearance,
        including verified warnings, and its own head contains that commit.
        """
        if predecessor is None:
            return {"ready": True, "reason": "lowest_selected"}
        if not (predecessor_state or {}).get("clear"):
            return {
                "ready": False,
                "reason": "predecessor_is_not_green",
                "predecessor": predecessor["number"],
            }
        current = self.clearance(
            predecessor["number"],
            STAGE_CI,
            predecessor["head_sha"],
            self.base_sha_for(predecessor),
        )
        if not current["clear"]:
            return {
                "ready": False,
                "reason": current.get("reason") or "ci_clearance_not_verified",
                "predecessor": predecessor["number"],
                "stage_result": stage_result_summary(current),
            }
        predecessor_head = predecessor.get("head_sha")
        if not predecessor_head:
            return {
                "ready": False,
                "reason": "predecessor_head_unknown",
                "predecessor": predecessor["number"],
            }
        if not self.contains(self.repo_root, predecessor_head, member["head_sha"]):
            return {
                "ready": False,
                "reason": "predecessor_head_is_not_contained",
                "predecessor": predecessor["number"],
                "predecessor_head_sha": predecessor_head,
            }
        return {
            "ready": True,
            "reason": (
                "predecessor_has_ci_warning"
                if current.get("clearance_kind") == "ci_warning"
                else "predecessor_has_no_checks"
                if current.get("outcome") == "skipped"
                else "predecessor_is_green"
            ),
        }

    def monitor_ci_worker(
        self, worker: dict[str, Any], request: dict[str, Any]
    ) -> dict[str, Any]:
        """Carry each accepted push up the stack while the worker keeps running.

        The CI stage records every push it publishes. A new checkpoint means
        the descendants are now behind, so the conflict plugin is asked to
        propagate immediately rather than at the end of the phase.
        """
        seen: set[str] = set()
        propagations: list[dict[str, Any]] = []
        def sweep() -> None:
            self.check_cancellation()
            propagations.extend(self.propagate_ci_pushes(request, seen))
            self.worker_progress_update(worker, request)

        self.emit(
            "worker_wait_started",
            number=worker["number"],
            stage=worker["stage"],
            pull_request_pass=request["pass"],
        )
        while self.launcher.is_running(worker):
            self.check_cancellation()
            sweep()
            self.sleep(self.monitor_interval)
        self.check_cancellation()
        sweep()
        completion = self.finish_worker(worker, request)
        return {"completion": completion, "propagations": propagations}

    def propagate_ci_pushes(
        self, request: dict[str, Any], seen: set[str]
    ) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        completed = set(self.state.get("propagated_pushes", []))
        unsuccessful: list[tuple[str, dict[str, Any]]] = []
        for checkpoint in self.checkpoints(self.repository, request["number"]):
            self.check_cancellation()
            identity = checkpoint.get("id") or checkpoint.get("head_sha")
            head_sha = checkpoint.get("head_sha")
            iteration = checkpoint.get("pipeline_iteration")
            if (
                not identity
                or identity in seen
                or identity in completed
                or not head_sha
                or checkpoint.get("pipeline_run") != self.run_id
                or not isinstance(iteration, int)
                or iteration > request["pass"]
            ):
                continue
            live_head = self.live_head_for(request["number"])
            if live_head is None:
                outcome = {
                    "result": "failed",
                    "reason": "source_head_unknown",
                    "number": request["number"],
                    "head_sha": head_sha,
                    "trigger": "checkpoint_revalidation",
                }
                outcomes.append(outcome)
                seen.add(identity)
                self.emit(
                    "push_propagated",
                    number=request["number"],
                    stage=STAGE_CI,
                    pull_request_pass=request["pass"],
                    head_sha=head_sha,
                    result=outcome["result"],
                    trigger=outcome["trigger"],
                )
                continue
            if head_sha != live_head:
                outcome = {
                    "result": "superseded",
                    "reason": "source_head_moved",
                    "number": request["number"],
                    "head_sha": head_sha,
                    "superseded_by": live_head,
                    "trigger": "obsolete_checkpoint",
                }
                outcomes.append(outcome)
                completed.add(identity)
                self.state["propagated_pushes"] = sorted(completed)
                self.save()
                seen.add(identity)
                self.emit(
                    "push_propagated",
                    number=request["number"],
                    stage=STAGE_CI,
                    pull_request_pass=request["pass"],
                    head_sha=head_sha,
                    result=outcome["result"],
                    trigger=outcome["trigger"],
                )
                continue
            self.check_cancellation()
            outcome = self.propagate_authorized(request["number"], head_sha)
            outcomes.append(outcome)
            self.propagations.append(outcome)
            result = outcome.get("result")
            if result in {"published", "no_descendants"}:
                completed.add(identity)
                for superseded_identity, superseded_outcome in unsuccessful:
                    completed.add(superseded_identity)
                    superseded_outcome["superseded_by"] = identity
                unsuccessful.clear()
                self.state["propagated_pushes"] = sorted(completed)
                self.save()
                seen.add(identity)
            elif result == "conflicted":
                seen.add(identity)
                unsuccessful.append((identity, outcome))
            else:
                unsuccessful.append((identity, outcome))
            self.emit(
                "push_propagated",
                number=request["number"],
                stage=STAGE_CI,
                pull_request_pass=request["pass"],
                head_sha=head_sha,
                result=result,
            )
        return outcomes

    def run_ci_phase(
        self, pass_number: int, selected: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self.emit(
            "phase_started",
            phase=STAGE_CI,
            pull_request_pass=pass_number,
            numbers=[member["number"] for member in selected],
            mode=PHASE_BOTTOM_UP,
        )
        entry = STAGE_BY_NAME[STAGE_CI]
        completions: list[dict[str, Any]] = []
        gates: list[dict[str, Any]] = []
        propagations: list[dict[str, Any]] = []
        verified_clear: set[int] = set()
        warning_members: list[dict[str, Any]] = []
        blocked: dict[str, Any] | None = None
        stopped: dict[str, Any] | None = None
        current_selected = selected
        previous: dict[str, Any] | None = None
        previous_state: dict[str, Any] | None = None
        for index in range(len(current_selected)):
            gate: dict[str, Any] | None = None
            request: dict[str, Any] | None = None
            while True:
                member = current_selected[index]
                previous = current_selected[index - 1] if index else None
                if (
                    previous is not None
                    and previous_state is not None
                    and (
                        previous["head_sha"] != previous_state["head_sha"]
                        or (
                            previous_state.get("clearance_kind") == "ci_warning"
                            and self.base_sha_for(previous) != previous_state["base_sha"]
                        )
                    )
                ):
                    previous_state = {
                        "clear": False,
                        "head_sha": previous["head_sha"],
                    }
                gate = self.ci_gate(member, previous, previous_state)
                if (
                    not gate["ready"]
                    and gate["reason"] == "predecessor_head_is_not_contained"
                ):
                    predecessor_head = gate["predecessor_head_sha"]
                    self.check_cancellation()
                    alignment = {
                        **self.propagate_authorized(previous["number"], predecessor_head),
                        "trigger": "predecessor_alignment",
                    }
                    propagations.append(alignment)
                    self.propagations.append(alignment)
                    self.emit(
                        "push_propagated",
                        number=previous["number"],
                        stage=STAGE_CI,
                        pull_request_pass=pass_number,
                        head_sha=predecessor_head,
                        result=alignment.get("result"),
                        trigger=alignment["trigger"],
                    )
                    if alignment.get("result") in {"published", "no_descendants"}:
                        refreshed = self.refresh_selection(current_selected)
                        if refreshed is None:
                            stopped = {
                                "step": "revalidate",
                                "number": member["number"],
                                "reason": "topology_changed",
                            }
                            break
                        current_selected = refreshed
                        member = current_selected[index]
                        previous = current_selected[index - 1]
                        if previous["head_sha"] != predecessor_head or (
                            previous_state.get("clearance_kind") == "ci_warning"
                            and self.base_sha_for(previous) != previous_state["base_sha"]
                        ):
                            previous_state = {
                                "clear": False,
                                "head_sha": previous["head_sha"],
                            }
                        gate = self.ci_gate(member, previous, previous_state)
                    else:
                        self.record_stage(
                            member["number"],
                            STAGE_CI,
                            {
                                "pass": pass_number,
                                "action": "waiting",
                                "reason": "descendant_propagation_incomplete",
                                "predecessor": previous["number"],
                            },
                        )
                        blocked = {
                            "number": member["number"],
                            "reason": "descendant_propagation_incomplete",
                            "propagations": [alignment],
                        }
                        break
                if not gate["ready"]:
                    if gate["reason"] != "predecessor_head_is_not_contained":
                        verified_clear.discard(gate["predecessor"])
                        warning_members = [
                            warning_member
                            for warning_member in warning_members
                            if warning_member["number"] != gate["predecessor"]
                        ]
                    self.record_stage(
                        member["number"],
                        STAGE_CI,
                        {"pass": pass_number, "action": "waiting", **gate},
                    )
                    blocked = {
                        "number": member["number"],
                        "reason": gate["reason"],
                    }
                    break
                request = self.request_for(member, STAGE_CI, pass_number)
                pending = self.propagate_ci_pushes(request, set())
                propagations.extend(pending)
                failed_propagations = [
                    outcome
                    for outcome in pending
                    if outcome.get("result") not in {"published", "no_descendants"}
                    and "superseded_by" not in outcome
                ]
                if failed_propagations:
                    blocked = {
                        "number": member["number"],
                        "reason": "descendant_propagation_incomplete",
                        "propagations": failed_propagations,
                    }
                    break
                if any(
                    outcome.get("result") in {"published", "superseded"}
                    for outcome in pending
                ):
                    refreshed = self.refresh_selection(current_selected)
                    if refreshed is None:
                        stopped = {
                            "step": "revalidate",
                            "number": member["number"],
                            "reason": "topology_changed",
                        }
                        break
                    current_selected = refreshed
                    continue
                break
            if gate is not None:
                gates.append({"number": member["number"], **gate})
            if blocked is not None or stopped is not None:
                break
            assert request is not None
            base_sha = self.base_sha_for(member)
            before = self.inspect(
                entry,
                common.target_for(self.repository, member["number"]),
                member["head_sha"],
                base_sha,
            )
            if before["clear"]:
                self.record_stage(
                    member["number"],
                    STAGE_CI,
                    {
                        "pass": pass_number,
                        "action": "already_clear",
                        **stage_result_summary(before),
                    },
                )
                previous, previous_state = member, {
                    "clear": True,
                    "clearance_kind": before.get("clearance_kind"),
                    "head_sha": member["head_sha"],
                    "base_sha": base_sha,
                }
                warning_members.append({
                    "number": member["number"],
                    "head_sha": member["head_sha"],
                    "base_sha": base_sha,
                    **common.ci_warning_fields([before]),
                })
                verified_clear.add(member["number"])
                continue
            launched = self.dispatch([request], STAGE_CI, pass_number)
            if launched["stopped"] is not None:
                stopped = launched["stopped"]
            if not launched["workers"]:
                break
            monitored = self.monitor_ci_worker(launched["workers"][0], request)
            propagations.extend(monitored["propagations"])
            completion = monitored["completion"]
            completions.append(completion)
            if completion.get("blocking_reason"):
                stopped = {
                    "step": "stage_status",
                    "number": completion["number"],
                    "stage": completion["stage"],
                    "reason": completion["blocking_reason"],
                    "detail": completion["blocking_detail"],
                    "stage_result": completion["stage_result"],
                }
                break
            failed_propagations = [
                outcome
                for outcome in monitored["propagations"]
                if outcome.get("result") not in {"published", "no_descendants"}
                and "superseded_by" not in outcome
            ]
            if failed_propagations:
                blocked = {
                    "number": member["number"],
                    "reason": "descendant_propagation_incomplete",
                    "propagations": failed_propagations,
                }
                break
            refreshed = self.refresh_selection(current_selected)
            if refreshed is None:
                stopped = {
                    "step": "revalidate",
                    "number": member["number"],
                    "reason": "topology_changed",
                }
                break
            current_selected = refreshed
            member = current_selected[index]
            base_sha = self.base_sha_for(member)
            after = self.inspect(
                entry,
                common.target_for(self.repository, member["number"]),
                member["head_sha"],
                base_sha,
            )
            clear = (
                bool(after["clear"])
                and completion["accepted"]
                and completion.get("returncode") == 0
            )
            self.record_stage(
                member["number"],
                STAGE_CI,
                {
                    "pass": pass_number,
                    "accepted": completion["accepted"],
                    "returncode": completion.get("returncode"),
                    "clear": after["clear"],
                    "clear_at_head_sha": after.get("clear_at_head_sha"),
                    "clear_at_base_sha": after.get("clear_at_base_sha"),
                    "clearance_kind": after.get("clearance_kind"),
                    **common.ci_warning_fields([after]),
                    "current_head_sha": completion.get("current_head_sha"),
                    "current_base_sha": completion.get("current_base_sha"),
                    "outcome": completion.get("stage_result", {}).get("outcome"),
                    "reason": completion.get("reason"),
                    "stage_result": completion["stage_result"],
                },
            )
            previous = member
            previous_state = {
                "clear": clear,
                "clearance_kind": after.get("clearance_kind"),
                "head_sha": after.get("clear_at_head_sha") or member["head_sha"],
                "base_sha": base_sha,
            }
            if clear:
                verified_clear.add(member["number"])
                warning_members.append({
                    "number": member["number"],
                    "head_sha": member["head_sha"],
                    "base_sha": base_sha,
                    **common.ci_warning_fields([after]),
                })
            if stopped is not None:
                break
        result = {
            "phase": STAGE_CI,
            "mode": PHASE_BOTTOM_UP,
            "dispatches": len(completions),
            "completions": completions,
            "gates": gates,
            "blocked": blocked,
            "propagations": propagations,
            "stopped": stopped,
            "clear": (
                blocked is None
                and stopped is None
                and all(
                    member["number"] in verified_clear for member in current_selected
                )
            ),
            **stack_ci_warning_fields(warning_members),
        }
        self.emit(
            "phase_finished",
            pull_request_pass=pass_number,
            numbers=[member["number"] for member in current_selected],
            **summarize_phase(result),
        )
        return result

    # Snapshot ------------------------------------------------------------

    def apply_native_stack_clearance(
        self, validation: dict[str, Any], pull_requests: list[dict[str, Any]]
    ) -> None:
        """Use accepted no-op evidence only for missing member conflict state."""
        if (
            self.state.get("kind") != RUN_KIND
            or self.state.get("run_id") != self.run_id
            or self.state.get("selection") != selection_evidence(self.kickoff)
            or self.state.get("selected") != self.kickoff["pullRequests"]
            or validation["fingerprint"] != self.state.get("topology_fingerprint")
            or [member["number"] for member in validation["stack"]["members"]]
            != self.kickoff["pullRequests"]
        ):
            return
        clicked = self.kickoff["startPullRequest"]
        snapshot = next(item for item in pull_requests if item["number"] == clicked)
        current = next(stage for stage in snapshot["stages"] if stage["stage"] == STAGE_CONFLICT)
        previous = (
            self.state.get("pull_requests", {}).get(str(clicked), {})
            .get("stages", {}).get(STAGE_CONFLICT, {})
        )
        status = current.get("status") or {}
        clearance = status.get("native_stack_clearance")
        task = status.get("agent_task")
        prior_result = previous.get("stage_result") or {}
        prior_pass = previous.get("pass")
        iteration = task.get("iteration") if isinstance(task, dict) else None
        if (
            not isinstance(clearance, dict)
            or not isinstance(task, dict)
            or current.get("clear") is not True
            or current.get("outcome") != "cleared"
            or common.stage_blocker(current, after_launch=True) is not None
            or previous.get("accepted") is not True
            or previous.get("returncode") != 0
            or previous.get("clear") is not True
            or previous.get("outcome") != "cleared"
            or type(prior_pass) is not int
            or type(self.state.get("pass")) is not int
            or not 1 <= prior_pass <= self.state.get("pass", 0) <= MAX_PASSES
            or previous.get("dispatched_head_sha") != snapshot["head_sha"]
            or previous.get("current_head_sha") != snapshot["head_sha"]
            or previous.get("current_base_sha") != snapshot["base_sha"]
            or previous.get("configuration") != {
                "model": self.models[STAGE_CONFLICT],
                "effort": self.effort,
                "strategy": self.conflict_strategy,
                "github_mutation_policy": self.github_mutation_policy,
            }
            or clearance != prior_result.get("native_stack_clearance")
            or task != prior_result.get("agent_task")
            or not paths_match(current.get("status_state"), stage_state_path(
                STAGE_BY_NAME[STAGE_CONFLICT],
                common.target_for(self.repository, clicked), self.run_id,
            ))
            or current.get("status_state") != prior_result.get("status_state")
            or task.get("invocation_id") != self.run_id
            or not isinstance(task.get("run_id"), str) or not task["run_id"]
            or task.get("status") != "completed"
            or task.get("outcome") != "already_mergeable"
            or task.get("task_id") is not None
            or task.get("task_id_status") != "not_needed"
            or task.get("whole_stack") is not True
            or task.get("requested_strategy") != "auto"
            or self.conflict_strategy != "auto"
            or task.get("model") != common.HOSTED_MODEL_FOR_COORDINATOR[self.models[STAGE_CONFLICT]]
            or not isinstance(task.get("policy"), str) or not task["policy"]
            or task.get("target") != common.target_for(self.repository, clicked)["pr_url"]
            or not isinstance(iteration, dict)
            or type(iteration.get("number")) is not int
            or type(iteration.get("budget")) is not int
            or iteration != {
                "id": f"{self.run_id}-{prior_pass}", "number": prior_pass, "budget": MAX_PASSES,
            }
        ):
            return
        authorization = clearance.get("authorization")
        if not isinstance(authorization, dict):
            return
        name = f"conflict-pass-{prior_pass}-pr-{clicked}"
        digest = hashlib.sha256(json.dumps(
            {**authorization, "request_sha256": ""},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        stack = validation["stack"]
        source, source_snapshot = stack_source_identity(stack)
        if (
            authorization.get("schema") != {
                "id": "github.copilot.stack-publication-request", "version": 1,
            }
            or authorization.get("operation") != "whole-stack"
            or authorization.get("request_id") != f"{self.run_id}-{name}"
            or authorization.get("request_sha256") != digest
            or self.state.get("stack_requests", {}).get(authorization["request_id"]) != digest
            or authorization.get("owner") != {
                "kind": RUN_KIND, "run_id": self.run_id,
                "state": str(self.state_path.resolve()),
                "cancellation": str(self.cancellation_path.resolve()),
            }
            or authorization.get("state") != str((self.run_directory / f"{name}.json").resolve())
            or authorization.get("repository") != self.repository.lower()
            or authorization.get("selected") != self.kickoff["pullRequests"]
            or authorization.get("fixed_pr") != clicked
            or authorization.get("fixed_head") != snapshot["head_sha"]
            or authorization.get("source_stack") != source
            or authorization.get("source_snapshot") != source_snapshot
            or authorization.get("topology_fingerprint") != validation["fingerprint"]
            or clearance.get("topology_fingerprint") != validation["fingerprint"]
            or clearance.get("source_snapshot") != source_snapshot
            or not isinstance(clearance.get("observed_at"), str) or not clearance["observed_at"]
            or clearance.get("trunk") != {"ref": stack["trunk"], "sha": pull_requests[0]["base_sha"]}
            or any(
                member.get("state") != "OPEN"
                or member.get("mergeable") != "MERGEABLE"
                or member.get("base_sha") != item["base_sha"]
                for member, item in zip(validation["selected"], pull_requests)
            )
            or clearance.get("members") != [
                {
                    "pr_number": member["number"],
                    "repository": self.repository,
                    "head_ref": member["head_branch"],
                    "head_sha": item["head_sha"],
                    "direct_base_ref": member["base_branch"],
                    "direct_base_sha": item["base_sha"],
                    "merge_base": item["base_sha"],
                    "mergeable": "MERGEABLE",
                }
                for member, item in zip(validation["selected"], pull_requests)
            ]
        ):
            return
        for item in pull_requests:
            conflict = next(stage for stage in item["stages"] if stage["stage"] == STAGE_CONFLICT)
            if (
                conflict.get("reason") != "no_state"
                or conflict.get("clear") is not False
                or conflict.get("outcome") is not None
                or conflict.get("status")
                or conflict.get("clear_at_head_sha") is not None
                or conflict.get("clear_at_base_sha") is not None
                or conflict.get("installed") is not True
                or not paths_match(conflict.get("status_state"), stage_state_path(
                    STAGE_BY_NAME[STAGE_CONFLICT],
                    common.target_for(self.repository, item["number"]), self.run_id,
                ))
            ):
                continue
            conflict.update(
                clear=True, outcome="cleared", reason=None, identity="current",
                clear_at_head_sha=item["head_sha"], clear_at_base_sha=item["base_sha"],
                clearance_kind="native_stack_clearance",
                clearance_source={
                    "number": clicked, "status_state": current["status_state"],
                    "request_id": authorization["request_id"], "request_sha256": digest,
                    "run_id": task["run_id"], "invocation_id": self.run_id,
                },
            )
            item["uncleared"] = [stage["stage"] for stage in item["stages"] if not stage["clear"]]

    def final_snapshot(self) -> dict[str, Any]:
        """Require all five markers current for every selected pull request.

        The stack is read before and after the markers are inspected. A stack
        that moved while it was being inspected cannot produce one consistent
        snapshot, so the run stays incomplete instead of claiming success from
        markers taken at two different topologies.
        """
        opening = self.revalidate()
        if opening["result"] != "ready":
            return {**opening, "result": "incomplete", "revalidation": opening["result"]}
        pull_requests = []
        whole_stack = [
            member["number"] for member in opening["stack"]["members"]
        ] == self.kickoff["pullRequests"]
        for member in opening["selected"]:
            base_sha = self.base_sha_for(member)
            target = common.target_for(self.repository, member["number"])
            if base_sha is None:
                stages = [
                    {
                        "stage": entry["stage"],
                        "clear": False,
                        "clear_at_head_sha": None,
                        "clear_at_base_sha": None,
                        "outcome": None,
                        "reason": "base_status_unavailable",
                        "identity": "unverified",
                        "installed": stage_script_path(entry).is_file(),
                        "status_state": str(
                            stage_state_path(entry, target, self.run_id)
                        ),
                        "status": {},
                        "inspected_head_sha": member["head_sha"],
                        "inspected_base_sha": None,
                    }
                    for entry in STAGES
                ]
                pull_requests.append(
                    {
                        "number": member["number"],
                        "head_sha": member["head_sha"],
                        "base_sha": None,
                        "is_draft": member.get("is_draft"),
                        "stages": stages,
                        "uncleared": list(STAGE_NAMES),
                    }
                )
                return {
                    "result": "incomplete",
                    "reason": "base_status_unavailable",
                    "fingerprint": opening["fingerprint"],
                    "pull_requests": pull_requests,
                }
            stages = [
                self.mergeability_clearance(member, base_sha)
                if entry["stage"] == STAGE_CONFLICT and not whole_stack
                else self.inspect(entry, target, member["head_sha"], base_sha)
                for entry in STAGES
            ]
            if whole_stack and member["base_branch"] != opening["stack"]["trunk"]:
                conflict = next(
                    stage for stage in stages if stage["stage"] == STAGE_CONFLICT
                )
                if conflict["clear"] and conflict["clear_at_base_sha"] != base_sha:
                    conflict.update(
                        clear=False, clearance_kind=None,
                        reason="clearance_is_for_an_older_base",
                    )
            for stage in stages:
                stage["identity"] = (
                    "current"
                    if stage["clear"]
                    else "stale"
                    if stage.get("reason")
                    in {
                        "clearance_is_for_an_older_head",
                        "clearance_is_for_an_older_base",
                        "ci_warning_snapshot_changed",
                    }
                    else "unverified"
                )
                stage["inspected_head_sha"] = member["head_sha"]
                stage["inspected_base_sha"] = base_sha
            pull_requests.append(
                {
                    "number": member["number"],
                    "head_sha": member["head_sha"],
                    "base_sha": base_sha,
                    "is_draft": member.get("is_draft"),
                    "stages": stages,
                    "uncleared": [
                        stage["stage"] for stage in stages if not stage["clear"]
                    ],
                    **common.ci_warning_fields(stages),
                }
            )
        closing = self.revalidate()
        if closing["result"] != "ready":
            return {
                **closing,
                "result": "incomplete",
                "revalidation": closing["result"],
                "pull_requests": pull_requests,
            }
        if closing["fingerprint"] != opening["fingerprint"]:
            return {
                "result": "incomplete",
                "reason": "topology_changed_during_snapshot",
                "fingerprint": opening["fingerprint"],
                "pull_requests": pull_requests,
            }
        heads_moved = [
            member["number"]
            for member, snapshot in zip(closing["selected"], pull_requests)
            if member["head_sha"] != snapshot["head_sha"]
        ]
        if heads_moved:
            return {
                "result": "incomplete",
                "reason": "heads_moved_during_snapshot",
                "fingerprint": opening["fingerprint"],
                "pull_requests": pull_requests,
                "moved": heads_moved,
            }
        bases_moved = [
            member["number"]
            for member, snapshot in zip(closing["selected"], pull_requests)
            if self.base_sha_for(member) != snapshot["base_sha"]
        ]
        if bases_moved:
            return {
                "result": "incomplete",
                "reason": "bases_moved_during_snapshot",
                "fingerprint": opening["fingerprint"],
                "pull_requests": pull_requests,
                "moved": bases_moved,
            }
        if whole_stack:
            self.apply_native_stack_clearance(closing, pull_requests)
        complete = all(not entry["uncleared"] for entry in pull_requests)
        ci_fields = stack_ci_warning_fields(pull_requests)
        if complete and not ci_fields:
            ci_fields["all_ci_passed"] = True
        return {
            "result": "complete" if complete else "incomplete",
            "reason": None if complete else "stages_not_clear",
            "fingerprint": opening["fingerprint"],
            "pull_requests": pull_requests,
            **ci_fields,
        }

    # Run -----------------------------------------------------------------

    def cleanup(self) -> list[dict[str, Any]]:
        active = {
            worker["number"]
            for worker in self.state.get("active_workers", [])
            if isinstance(worker, dict) and isinstance(worker.get("number"), int)
        }
        return [
            (
                {
                    "result": "preserved",
                    "reason": "worker_still_active",
                    "number": number,
                }
                if number in active
                else self.launcher.cleanup(number)
            )
            for number in sorted(self.touched)
        ]

    def persist_result(self, payload: dict[str, Any]) -> None:
        common.write_json_atomically(
            self.result_path,
            {
                "kind": RUN_KIND,
                "run_id": self.run_id,
                "kickoff": self.kickoff,
                "finished_at": utc_now(),
                "pipeline_result": payload,
            },
        )

    def current_ci_warnings(self) -> dict[str, Any]:
        recorded = self.state.get("pull_requests", {})
        numbers = [
            int(number)
            for number, record in recorded.items()
            if record.get("stages", {}).get(STAGE_CI, {}).get("ci_warnings")
        ]
        if not numbers:
            return {}
        current = self.revalidate()
        if (
            current["result"] != "ready"
            or current["fingerprint"] != self.state.get("topology_fingerprint")
        ):
            return {
                "ci_warning_revalidation_error": (
                    "the live stack identity could not be revalidated"
                )
            }
        warnings = []
        errors = []
        for member in current["selected"]:
            if member["number"] not in numbers:
                continue
            base_sha = self.base_sha_for(member)
            if base_sha is None:
                errors.append(f"#{member['number']}: the live base revision could not be read")
                continue
            stage = self.clearance(
                member["number"], STAGE_CI, member["head_sha"], base_sha
            )
            if stage.get("reason") in common.UNAVAILABLE_STATUS_REASONS | {
                "no_state", "ci_warning_not_verified",
            }:
                errors.append(f"#{member['number']}: {stage['reason']}")
            warnings.append(
                {
                    "number": member["number"],
                    "head_sha": member["head_sha"],
                    "base_sha": base_sha,
                    **common.ci_warning_fields([stage]),
                }
            )
        return {
            **stack_ci_warning_fields(warnings),
            **({"ci_warning_revalidation_error": "; ".join(errors)} if errors else {}),
        }

    def finish(
        self,
        result: str,
        *,
        reason: str | None = None,
        detail: str | None = None,
        snapshot: dict[str, Any] | None = None,
        passes: int = 0,
        phases: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        cleanup = self.cleanup()
        cleanup_failures = [
            item
            for item in cleanup
            if isinstance(item, dict) and item.get("result") == "failed"
        ]
        finished_result = result
        finished_reason = reason
        finished_detail = detail
        if result == "complete" and cleanup_failures:
            finished_result = "error"
            finished_reason = "worktree_cleanup_failed"
            finished_detail = json.dumps(
                cleanup_failures, ensure_ascii=False, sort_keys=True
            )
        self.state["result"] = finished_result
        self.state["reason"] = finished_reason
        self.state["cleanup"] = cleanup
        if finished_detail is not None:
            self.state["detail"] = finished_detail
        else:
            self.state.pop("detail", None)
        if cleanup_failures:
            self.state["cleanup_failures"] = cleanup_failures
        else:
            self.state.pop("cleanup_failures", None)
        self.save()
        payload = {
            "result": finished_result,
            "run_id": self.run_id,
            "repository": self.repository,
            "stack_number": self.kickoff["stackNumber"],
            "start_pull_request": self.kickoff["startPullRequest"],
            "selected": list(self.kickoff["pullRequests"]),
            "passes": passes,
            "state_path": str(self.state_path),
            "pull_requests": self.state.get("pull_requests", {}),
            "phases": [summarize_phase(phase) for phase in phases or []],
            "propagations": self.propagations,
            "cleanup": cleanup,
        }
        if finished_reason is not None:
            payload["reason"] = finished_reason
        if finished_detail is not None:
            payload["detail"] = finished_detail
        if cleanup_failures:
            payload["cleanup_failures"] = cleanup_failures
        if snapshot is not None:
            payload["snapshot"] = snapshot
        if result in {"complete", "partial"} and snapshot is not None:
            if snapshot.get("ci_warnings"):
                payload.update({
                    "ci_warnings": snapshot["ci_warnings"],
                    "all_ci_passed": False,
                })
            elif snapshot.get("all_ci_passed") is True:
                payload["all_ci_passed"] = True
        else:
            try:
                payload.update(self.current_ci_warnings())
            except (WorkflowError, json.JSONDecodeError, OSError) as error:
                payload["ci_warning_revalidation_error"] = str(error)
        if self.session_title is not None:
            payload["session_title"] = self.session_title
        self.persist_result(payload)
        return payload

    def execute(self) -> dict[str, Any]:
        try:
            return self._execute()
        except PipelineCancelled:
            if not self.state:
                self.state = new_state(self.kickoff, self.run_id, "")
            errors = self.cancel_active_workers()
            return self.finish(
                "cancelled",
                reason="cancel_requested",
                detail=json.dumps(errors, sort_keys=True) if errors else None,
                passes=self.completed_passes,
                phases=self.completed_phases,
            )
        except (
            WorkflowError,
            json.JSONDecodeError,
            OSError,
            subprocess.SubprocessError,
        ) as error:
            if not self.state:
                self.state = new_state(self.kickoff, self.run_id, "")
            cancellation_errors = self.cancel_active_workers()
            detail = {"error": str(error)}
            if cancellation_errors:
                detail["cancellation_errors"] = cancellation_errors
            return self.finish(
                "error",
                reason="scheduler_error",
                detail=json.dumps(detail, sort_keys=True),
                passes=self.completed_passes,
                phases=self.completed_phases,
            )

    def _execute(self) -> dict[str, Any]:
        self.emit(
            "stack_pipeline_started",
            repository=self.repository,
            stack_number=self.kickoff["stackNumber"],
            start_pull_request=self.kickoff["startPullRequest"],
            selected=list(self.kickoff["pullRequests"]),
        )
        self.check_cancellation()
        opening = self.revalidate(require_initial_source_snapshot=True)
        if opening["result"] != "ready":
            self.state = new_state(self.kickoff, self.run_id, "")
            return self.finish(
                "stopped",
                reason=opening["reason"],
                detail=opening.get("detail"),
            )
        fingerprint = self.kickoff.get("topologyFingerprint") or opening["fingerprint"]
        if self.state_path.exists():
            self.state = new_state(self.kickoff, self.run_id, fingerprint)
            result = {
                "result": "stopped",
                "reason": "run_state_already_exists",
                "detail": (
                    "the exact run state path already exists and is sealed "
                    "against replay"
                ),
                "run_id": self.run_id,
                "repository": self.repository,
                "stack_number": self.kickoff["stackNumber"],
                "state_path": str(self.state_path),
                **(
                    {"session_title": self.session_title}
                    if self.session_title is not None
                    else {}
                ),
            }
            self.persist_result(result)
            return result
        self.state = new_state(self.kickoff, self.run_id, fingerprint)
        self.check_cancellation()
        self.state["active_workers"] = []
        self.state["run_directory"] = str(self.run_directory)
        self.state["github_mutation_policy"] = self.github_mutation_policy
        self.state["expected_heads"] = {
            str(member["number"]): member["head_sha"]
            for member in opening["selected"]
        }
        self.state["expected_bases"] = {
            str(member["number"]): self.base_sha_for(member)
            for member in opening["selected"]
        }
        self.save()
        missing = self.dependencies()
        if missing:
            return self.finish(
                "stopped",
                reason="missing_dependencies",
                detail=f"these stage plugins are not installed: {', '.join(missing)}",
            )
        self.emit(
            "topology_validated",
            fingerprint=fingerprint,
            selected=[member["number"] for member in opening["selected"]],
        )

        phases: list[dict[str, Any]] = []
        snapshot: dict[str, Any] | None = None
        completed_passes = 0
        completed_conflict_resolution = False
        for pass_number in range(1, MAX_PASSES + 1):
            self.check_cancellation()
            current = self.revalidate()
            if current["result"] != "ready":
                return self.finish(
                    "stopped",
                    reason=current["reason"],
                    detail=current.get("detail"),
                    passes=completed_passes,
                    phases=phases,
                    snapshot=snapshot,
                )
            if current["fingerprint"] != fingerprint:
                return self.finish(
                    "stopped",
                    reason="topology_changed",
                    detail="the stack changed while the run was in progress",
                    passes=completed_passes,
                    phases=phases,
                    snapshot=snapshot,
                )
            selected = current["selected"]
            self.state["pass"] = pass_number
            self.save()
            self.emit("pass_started", pull_request_pass=pass_number)

            for phase in PHASES:
                self.check_cancellation()
                if phase["mode"] == PHASE_STACK_DISPATCH:
                    if completed_conflict_resolution:
                        clicked_number = self.kickoff["startPullRequest"]
                        outcome = {
                            "phase": STAGE_CONFLICT,
                            "mode": PHASE_STACK_DISPATCH,
                            "dispatches": 0,
                            "completions": [],
                            "stopped": None,
                            "action": "completed_this_run",
                            "clear": True,
                        }
                        self.emit(
                            "phase_finished",
                            pull_request_pass=pass_number,
                            numbers=[clicked_number],
                            **summarize_phase(outcome),
                        )
                    else:
                        outcome = self.run_conflict_phase(pass_number, selected)
                        completed_conflict_resolution = (
                            completed_conflict_resolution
                            or bool(outcome.get("completed"))
                        )
                elif phase["mode"] == PHASE_BOTTOM_UP:
                    outcome = self.run_ci_phase(pass_number, selected)
                else:
                    outcome = self.run_parallel_phase(
                        phase["phase"], pass_number, selected
                    )
                phases.append(outcome)
                self.completed_phases.append(outcome)
                self.check_cancellation()
                if outcome.get("stopped") is not None:
                    stopped = outcome["stopped"]
                    if stopped.get("step") == "stage_status":
                        return self.finish(
                            "blocked",
                            reason=stopped["reason"],
                            detail=stopped.get("detail"),
                            passes=completed_passes,
                            phases=phases,
                            snapshot=snapshot,
                        )
                    return self.finish(
                        "stopped",
                        reason="worker_launch_stopped",
                        detail=json.dumps(stopped, sort_keys=True),
                        passes=completed_passes,
                        phases=phases,
                        snapshot=snapshot,
                    )
                selected = self.refresh_selection(selected)
                if selected is None:
                    return self.finish(
                        "stopped",
                        reason="topology_changed",
                        detail="the stack changed between phases",
                        passes=completed_passes,
                        phases=phases,
                        snapshot=snapshot,
                    )

            completed_passes = pass_number
            self.completed_passes = pass_number
            snapshot = self.final_snapshot()
            self.emit(
                "snapshot_taken",
                pull_request_pass=pass_number,
                result=snapshot["result"],
                reason=snapshot.get("reason"),
                **(
                    {"ci_warnings": snapshot["ci_warnings"], "all_ci_passed": False}
                    if snapshot.get("ci_warnings")
                    else {}
                ),
            )
            if snapshot["result"] == "complete":
                return self.finish(
                    "complete",
                    snapshot=snapshot,
                    passes=completed_passes,
                    phases=phases,
                )
        return self.finish(
            "partial",
            reason="two_passes_finished",
            snapshot=snapshot,
            passes=completed_passes,
            phases=phases,
        )

    def refresh_selection(
        self, selected: list[dict[str, Any]]
    ) -> list[dict[str, Any]] | None:
        current = self.revalidate()
        if current["result"] != "ready":
            return None
        if current["fingerprint"] != self.state.get("topology_fingerprint"):
            return None
        return current["selected"]


def summarize_phase(phase: dict[str, Any]) -> dict[str, Any]:
    completions = phase.get("completions", [])
    reused = phase.get("reused", [])
    accepted = [
        completion["number"]
        for completion in completions
        if completion.get("accepted")
    ]
    reasons = sorted(
        {
            str(completion.get("reason") or "not_cleared")
            for completion in completions
            if completion.get("accepted") and not completion.get("clear")
        }
    )
    summary = {
        "phase": phase["phase"],
        "mode": phase["mode"],
        "dispatches": phase.get("dispatches", 0),
        "accepted": accepted,
        "ignored": [
            completion["number"]
            for completion in completions
            if not completion.get("accepted")
        ],
        "stopped": phase.get("stopped"),
        "clear": (
            phase["clear"]
            if isinstance(phase.get("clear"), bool)
            else not phase.get("stopped")
            and bool(accepted or reused)
            and len(accepted) == len(completions)
            and all(
                completion.get("returncode") == 0
                and completion.get("clear")
                for completion in completions
            )
        ),
    }
    if "reused" in phase:
        summary["reused"] = [item["number"] for item in reused]
    if reasons:
        summary["reasons"] = reasons
    source_drifts = [
        {
            "number": completion["number"],
            "stage": completion["stage"],
            **completion["stage_result"]["source_drift"],
        }
        for completion in completions
        if isinstance(completion.get("stage_result"), dict)
        and isinstance(completion["stage_result"].get("source_drift"), dict)
    ]
    if source_drifts:
        summary["source_drifts"] = source_drifts
    if phase.get("blocked") is not None:
        summary["blocked"] = phase["blocked"]
    if phase.get("action") is not None:
        summary["action"] = phase["action"]
    if phase.get("ci_warnings"):
        summary.update({
            "ci_warnings": phase["ci_warnings"],
            "all_ci_passed": False,
        })
    return summary


def clipped_text(value: Any, limit: int = TERMINAL_TEXT_MAX_CHARS) -> str | None:
    return common.clipped_text(value, limit)


def compact_terminal_result(
    payload: dict[str, Any], *, result_path: Path | str | None = None
) -> dict[str, Any]:
    result_sha256 = None
    if result_path is not None and Path(result_path).is_file():
        payload, result_sha256 = common.canonical_terminal_payload(
            payload, Path(result_path), envelope_key="pipeline_result"
        )
    snapshot = payload.get("snapshot")
    snapshot_requests = (
        snapshot.get("pull_requests", []) if isinstance(snapshot, dict) else []
    )
    compact_requests = []
    for pull_request in snapshot_requests[:TERMINAL_RESULT_MAX_PULL_REQUESTS]:
        stages = []
        for stage in pull_request.get("stages", [])[: len(STAGES)]:
            stages.append(
                {
                    key: value
                    for key, value in {
                        "stage": stage.get("stage"),
                        "clear": stage.get("clear"),
                        "identity": stage.get("identity"),
                        "outcome": stage.get("outcome"),
                        "reason": stage.get("reason"),
                        "clear_at_head_sha": stage.get("clear_at_head_sha"),
                        "clear_at_base_sha": stage.get("clear_at_base_sha"),
                        "clearance_kind": stage.get("clearance_kind"),
                        "all_ci_passed": stage.get("all_ci_passed"),
                    }.items()
                    if value is not None
                }
            )
        compact_requests.append(
            {
                key: value
                for key, value in {
                    "number": pull_request.get("number"),
                    "head_sha": pull_request.get("head_sha"),
                    "base_sha": pull_request.get("base_sha"),
                    "uncleared": pull_request.get("uncleared"),
                    "stages": stages,
                }.items()
                if value is not None
            }
        )

    def limited(values: Any) -> list[Any]:
        return list(values[:TERMINAL_RESULT_MAX_PULL_REQUESTS]) if isinstance(values, list) else []

    warnings_source = payload.get("ci_warnings", [])
    warnings = [
        {
            **{
                key: warning[key]
                for key in ("number", "head_sha", "base_sha", "diagnosis")
            },
            **{
                key: clipped_text(warning[key])
                for key in ("check_key", "name", "reason")
            },
            "evidence": [clipped_text(item) for item in limited(warning["evidence"])],
            **(
                {"evidence_omitted": len(warning["evidence"]) - len(limited(warning["evidence"]))}
                if len(warning["evidence"]) > TERMINAL_RESULT_MAX_PULL_REQUESTS
                else {}
            ),
        }
        for warning in limited(warnings_source)
    ]
    cleanup_failures_source = payload.get("cleanup_failures", [])
    cleanup_failures = []
    cleanup_failure_truncations = []
    for failure in limited(cleanup_failures_source):
        if not isinstance(failure, dict):
            continue
        fields = {
            "result": failure.get("result"),
            "reason": failure.get("reason"),
            "detail": failure.get("detail"),
            "number": failure.get("number"),
            "worktree": failure.get("worktree"),
            "ownership_record": failure.get("ownership_record"),
            "worktree_removed": failure.get("worktree_removed"),
        }
        cleanup_failure_truncations.append(
            any(
                isinstance(value, str)
                and len(value) > TERMINAL_TEXT_MAX_CHARS
                for value in fields.values()
            )
        )
        cleanup_failures.append({
            key: clipped_text(value) if isinstance(value, str) else value
            for key, value in fields.items()
            if value is not None
        })
    phases_source = payload.get("phases")
    phases_source = phases_source if isinstance(phases_source, list) else []
    stage_failure = {}
    terminal_phase = phases_source[-1] if phases_source else None
    stopped = terminal_phase.get("stopped") if isinstance(terminal_phase, dict) else None
    if payload.get("result") != "complete" and isinstance(stopped, dict):
        stage_failure = common.stage_failure_summary(
            stopped.get("stage_result"), text_limit=TERMINAL_TEXT_MAX_CHARS,
        )
        if stage_failure and type(stopped.get("number")) is int:
            stage_failure["number"] = stopped["number"]
    phases = []
    for phase in phases_source[:TERMINAL_RESULT_MAX_PHASES]:
        if not isinstance(phase, dict):
            continue
        stopped = phase.get("stopped")
        compact_stopped = (
            {
                key: stopped.get(key)
                for key in ("step", "number", "stage", "reason")
                if stopped.get(key) is not None
            }
            if isinstance(stopped, dict)
            else None
        )
        phases.append(
            {
                key: value
                for key, value in {
                    "phase": phase.get("phase"),
                    "mode": phase.get("mode"),
                    "dispatches": phase.get("dispatches"),
                    "accepted": limited(phase.get("accepted")),
                    "reused": limited(phase.get("reused")),
                    "reused_omitted": (
                        len(phase["reused"]) - TERMINAL_RESULT_MAX_PULL_REQUESTS
                        if isinstance(phase.get("reused"), list)
                        and len(phase["reused"]) > TERMINAL_RESULT_MAX_PULL_REQUESTS
                        else None
                    ),
                    "ignored": limited(phase.get("ignored")),
                    "clear": phase.get("clear"),
                    "all_ci_passed": phase.get("all_ci_passed"),
                    "reasons": [
                        clipped_text(reason, 128) for reason in limited(phase.get("reasons"))
                    ],
                    "stopped": compact_stopped,
                    "action": phase.get("action"),
                    "source_drifts": limited(phase.get("source_drifts")),
                    "source_drifts_omitted": (
                        len(phase["source_drifts"]) - TERMINAL_RESULT_MAX_PULL_REQUESTS
                        if isinstance(phase.get("source_drifts"), list)
                        and len(phase["source_drifts"]) > TERMINAL_RESULT_MAX_PULL_REQUESTS
                        else None
                    ),
                }.items()
                if value not in (None, [], {})
            }
        )

    propagations_source = payload.get("propagations", [])
    propagations = [
        {
            key: clipped_text(value, 128) if isinstance(value, str) else value
            for key, value in {
                "number": propagation.get("number"),
                "head_sha": propagation.get("head_sha"),
                "result": propagation.get("result"),
                "reason": propagation.get("reason"),
                "trigger": propagation.get("trigger"),
            }.items()
            if value is not None
        }
        for propagation in limited(propagations_source)
        if isinstance(propagation, dict)
    ]
    artifacts = {
        key: clipped_text(value)
        for key, value in {
            "result": str(result_path) if result_path is not None else None,
            "state": payload.get("state_path"),
            "result_sha256": result_sha256,
        }.items()
        if value is not None
    }
    compact = {
        key: value
        for key, value in {
            "result": payload.get("result"),
            "reason": payload.get("reason"),
            "detail": clipped_text(payload.get("detail")),
            "stage_failure": stage_failure,
            "cleanup_failures": cleanup_failures,
            "cleanup_failure_details_truncated": (
                True if any(cleanup_failure_truncations) else None
            ),
            "cleanup_failures_omitted": (
                len(cleanup_failures_source) - len(cleanup_failures)
                if isinstance(cleanup_failures_source, list)
                and len(cleanup_failures_source) > len(cleanup_failures)
                else None
            ),
            "run_id": payload.get("run_id"),
            "repository": payload.get("repository"),
            "stack_number": payload.get("stack_number"),
            "start_pull_request": payload.get("start_pull_request"),
            "selected": limited(payload.get("selected")),
            "selected_omitted": (
                len(payload["selected"]) - len(limited(payload["selected"]))
                if isinstance(payload.get("selected"), list)
                and len(payload["selected"]) > TERMINAL_RESULT_MAX_PULL_REQUESTS
                else None
            ),
            "passes": payload.get("passes"),
            "all_ci_passed": payload.get("all_ci_passed"),
            "ci_warnings": warnings,
            "ci_warning_revalidation_error": clipped_text(
                payload.get("ci_warning_revalidation_error")
            ),
            "ci_warnings_omitted": (
                len(warnings_source) - len(warnings)
                if len(warnings_source) > len(warnings)
                else None
            ),
            "ci_warning_details_truncated": (
                True
                if any(
                    len(warning[key]) > TERMINAL_TEXT_MAX_CHARS
                    for warning in warnings_source
                    for key in ("check_key", "name", "reason")
                )
                or any(
                    len(item) > TERMINAL_TEXT_MAX_CHARS
                    or len(warning["evidence"]) > TERMINAL_RESULT_MAX_PULL_REQUESTS
                    for warning in warnings_source
                    for item in warning["evidence"]
                )
                else None
            ),
            "session_title": clipped_text(payload.get("session_title")),
            "phases": phases,
            "propagations": propagations,
            "propagations_omitted": (
                len(propagations_source) - len(propagations)
                if isinstance(propagations_source, list)
                and len(propagations_source) > len(propagations)
                else None
            ),
            "snapshot": (
                {
                    "result": snapshot.get("result"),
                    "reason": snapshot.get("reason"),
                    "pull_requests": compact_requests,
                    **(
                        {
                            "pull_requests_omitted": len(snapshot_requests)
                            - len(compact_requests)
                        }
                        if len(snapshot_requests) > len(compact_requests)
                        else {}
                    ),
                }
                if isinstance(snapshot, dict)
                else None
            ),
            "artifacts": artifacts,
        }.items()
        if value not in (None, [], {})
    }

    def size() -> int:
        event = {"event": "stack_pipeline_finished", **compact}
        return len((json.dumps(event, sort_keys=True) + os.linesep).encode("utf-8"))

    while compact_requests and size() > TERMINAL_RESULT_MAX_BYTES:
        compact_requests.pop()
        compact["snapshot"]["pull_requests_omitted"] = (
            len(snapshot_requests) - len(compact_requests)
        )
    if size() > TERMINAL_RESULT_MAX_BYTES:
        compact.pop("phases", None)
        compact["terminal_detail_omitted"] = True
    if size() > TERMINAL_RESULT_MAX_BYTES:
        compact.pop("snapshot", None)
        compact["terminal_detail_omitted"] = True
    while warnings and size() > TERMINAL_RESULT_MAX_BYTES:
        warnings.pop()
        compact["ci_warnings_omitted"] = len(warnings_source) - len(warnings)
    while cleanup_failures and size() > TERMINAL_RESULT_MAX_BYTES:
        cleanup_failures.pop()
        cleanup_failure_truncations.pop()
        compact["cleanup_failures_omitted"] = (
            len(cleanup_failures_source) - len(cleanup_failures)
        )
        if any(cleanup_failure_truncations):
            compact["cleanup_failure_details_truncated"] = True
        else:
            compact.pop("cleanup_failure_details_truncated", None)
    if size() > TERMINAL_RESULT_MAX_BYTES and compact.pop("stage_failure", None):
        compact["stage_failure_omitted"] = True
    if size() > TERMINAL_RESULT_MAX_BYTES:
        raise WorkflowError("terminal result metadata exceeds the output byte limit")
    return compact


def command_run(args: argparse.Namespace) -> None:
    common.ACTIVE_GITHUB_MUTATION_POLICY = args.github_mutation_policy
    common.require_tools()
    repo_root = common.resolve_repo_root()
    target = common.resolve_target(args.target, repo_root)
    kickoff = selection_from_stack(
        target, read_native_stack(target["repo_name"], target["number"])
    )
    reporter = ProgressReporter()
    pipeline = StackPipeline(
        kickoff,
        repo_root,
        models=common.stage_models(args.stage_model, args.effort),
        effort=args.effort,
        conflict_strategy=args.conflict_strategy,
        github_mutation_policy=args.github_mutation_policy,
        run_id=common._EXECUTION.run_id if common._EXECUTION is not None else None,
        report=reporter,
    )
    result = pipeline.execute()
    reporter(
        {
            "event": "stack_pipeline_finished",
            **compact_terminal_result(
                result, result_path=getattr(pipeline, "result_path", None)
            ),
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser(
        "run", help="run up to two bounded passes over one native stack"
    )
    run.add_argument(
        "target",
        help=(
            "starting PR URL, owner/repo#number, or a bare number resolved from "
            "the current workspace repository"
        ),
    )
    run.add_argument(
        "--stage-model",
        action="append",
        help="pin one stage's model as <stage>=<model>; repeatable",
    )
    run.add_argument("--effort", default=DEFAULT_EFFORT)
    run.add_argument(
        "--conflict-strategy",
        choices=common.CONFLICT_STRATEGIES,
        default="auto",
    )
    run.add_argument(
        "--github-mutation-policy",
        choices=("allow", "source-only"),
        default="allow",
    )
    run.set_defaults(function=command_run)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.function(args)
        return 0
    except (WorkflowError, json.JSONDecodeError, OSError) as error:
        event = {
            "event": "stack_pipeline_finished",
            "result": "error",
            "error": str(error),
        }
        ProgressReporter()(event)
        return 1
    except KeyboardInterrupt:
        event = {
            "event": "stack_pipeline_finished",
            "result": "error",
            "error": "interrupted",
        }
        ProgressReporter()(event)
        return 130


_EXECUTION = None
EXECUTION_TERMINAL_RESULTS = frozenset({
    "complete",
    "partial",
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
    commands = ('run',)
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
