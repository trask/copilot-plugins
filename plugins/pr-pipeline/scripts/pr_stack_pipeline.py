#!/usr/bin/env python3
"""Drive one native GitHub stack through the five pipeline stages.

This helper is orchestration only. Every unit of work is delegated to the
plugin-qualified agent that already owns that stage, and no stage policy is
reimplemented here: the helper decides who runs, where, in which order, and
when a result counts, while each stage decides what to do.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any, Callable


COMMON_MODULE_NAME = "pr_pipeline_common"
COMMON_PATH = Path(__file__).resolve().parent / "pipeline_common.py"


def load_common() -> Any:
    """Load the shared pipeline module that sits beside this script."""
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

KICKOFF_VERSION = 1
STATE_VERSION = 1
MAX_PASSES = 2
DEFAULT_EFFORT = common.DEFAULT_EFFORT
READINESS_TIMEOUT = 300.0
READINESS_POLL_INTERVAL = 2.0
MONITOR_POLL_INTERVAL = 15.0
STACK_ENTRIES_PAGE = 50
RUN_KIND = "pr-stack-pipeline"
OWNERSHIP_SUFFIX = ".worktree.json"
CONFLICT_PROPAGATE_COMMAND = "descendant-propagate"
PROGRESS_EVENT = common.PROGRESS_EVENT
PROGRESS_UPDATE_EVENT = common.PROGRESS_UPDATE_EVENT
PROGRESS_HEARTBEAT_INTERVAL = common.PROGRESS_HEARTBEAT_INTERVAL

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


def stage_state_path(entry: dict[str, Any], target: dict[str, Any]) -> Path:
    return common.stage_state_path(entry, target)


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


def parse_kickoff(payload: Any) -> dict[str, Any]:
    """Accept exactly the structured kickoff this agent is started with.

    Anything that is not schema version 1, naming one repository, one native
    stack, one clicked pull request, and the ordered selection that starts at
    it, is rejected. A guessed selection would run stages against pull requests
    the user never picked.
    """
    if not isinstance(payload, dict):
        raise WorkflowError("the kickoff payload must be a JSON object")
    version = payload.get("version")
    if version != KICKOFF_VERSION:
        raise WorkflowError(
            f"the kickoff payload must use version {KICKOFF_VERSION}, not {version!r}"
        )
    repository = payload.get("repository")
    if not isinstance(repository, str) or not common.REPO_NAME_PATTERN.fullmatch(
        repository.strip()
    ):
        raise WorkflowError("the kickoff payload needs an owner/repo repository")
    stack_number = payload.get("stackNumber")
    if not isinstance(stack_number, int) or isinstance(stack_number, bool):
        raise WorkflowError("the kickoff payload needs an integer stackNumber")
    start = payload.get("startPullRequest")
    if not isinstance(start, int) or isinstance(start, bool) or start <= 0:
        raise WorkflowError("the kickoff payload needs a positive startPullRequest")
    numbers = payload.get("pullRequests")
    if not isinstance(numbers, list) or not numbers:
        raise WorkflowError("the kickoff payload needs a non-empty pullRequests list")
    selected: list[int] = []
    for number in numbers:
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise WorkflowError(
                f"pullRequests must hold positive pull request numbers: {number!r}"
            )
        if number in selected:
            raise WorkflowError(f"pullRequests repeats #{number}")
        selected.append(number)
    if selected[0] != start:
        raise WorkflowError(
            "pullRequests must start at startPullRequest "
            f"#{start}, not #{selected[0]}"
        )
    return {
        "version": KICKOFF_VERSION,
        "repository": repository.strip(),
        "stackNumber": stack_number,
        "startPullRequest": start,
        "pullRequests": selected,
    }


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


def state_path_for(kickoff: dict[str, Any]) -> Path:
    return run_root() / f"{run_slug(kickoff)}.json"


def lock_path_for(kickoff: dict[str, Any]) -> Path:
    return run_root() / f"{run_slug(kickoff)}.lock"


def run_directory_for(kickoff: dict[str, Any], run_id: str) -> Path:
    return run_root() / run_slug(kickoff) / run_id


def progress_log_path(kickoff: dict[str, Any], run_id: str) -> Path:
    return run_directory_for(kickoff, run_id) / "progress.jsonl"


def launch_state_path(kickoff: dict[str, Any], run_id: str) -> Path:
    return run_directory_for(kickoff, run_id) / "launch.json"


def observer_state_path(kickoff: dict[str, Any], run_id: str) -> Path:
    return run_directory_for(kickoff, run_id) / "observer.json"


def scheduler_log_path(kickoff: dict[str, Any], run_id: str) -> Path:
    return run_directory_for(kickoff, run_id) / "scheduler.log"


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
        if phase == "diagnosing":
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
        update = {
            "message": message,
            "next_action": next_action,
            "waiting": True,
            "wait_reason": (
                f"{phase} a known CI failure for #{number}"
                if phase != "waiting"
                else f"waiting for remaining CI checks on #{number}"
            ),
        }
    elif event == "worker_finished":
        returncode = payload.get("returncode")
        accepted = payload.get("accepted")
        if returncode not in {None, 0}:
            outcome = f"failed for #{number} with exit code {returncode}"
            next_action = "Finish the stage and preserve the failure in the pipeline result."
        elif not accepted:
            outcome = f"finished for #{number}, but its stale result was ignored"
            next_action = "Continue with evidence for the current pull request head."
        else:
            outcome = f"completed for #{number}"
            next_action = "Collect any remaining worker results."
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
        if stopped:
            outcome = "failed"
            next_action = "Stop the pipeline and report the launch failure."
        elif blocked:
            outcome = f"blocked: {blocked.get('reason', 'unknown reason')}"
            next_action = "Continue to the snapshot or next bounded pass."
        else:
            outcome = "complete"
            next_action = "Revalidate the stack, then start the next stage."
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
        update = {
            "message": (
                f"Stack pipeline {result}"
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
            "pull_request_pass": pass_number,
            "stage": stage,
            "pull_requests": numbers,
        }
    )
    return {key: value for key, value in update.items() if value is not None}


class ProgressReporter(common.ConversationProgressReporter):
    def __init__(
        self,
        *,
        event_log: Path | None = None,
        output: Callable[[dict[str, Any]], None] = common.emit,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(
            transition=progress_transition,
            event_log=event_log,
            output=output,
            wall_time=wall_time,
        )


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
    "              number title headRefName baseRefName headRefOid isDraft state"
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
    trunk = raw.get("baseRefName")
    if not isinstance(trunk, str) or not trunk:
        raise WorkflowError("the native stack has no trunk branch")
    entries = raw.get("entries")
    nodes = entries.get("nodes") if isinstance(entries, dict) else None
    members: list[dict[str, Any]] = []
    for node in nodes or []:
        member = node.get("pullRequest") if isinstance(node, dict) else None
        if not isinstance(member, dict):
            raise WorkflowError("the native stack has an unreadable member")
        number = member.get("number")
        title = member.get("title")
        head_branch = member.get("headRefName")
        base_branch = member.get("baseRefName")
        head_sha = member.get("headRefOid")
        if (
            not isinstance(number, int)
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
                "position": node.get("position"),
                "number": number,
                "title": title,
                "head_branch": head_branch,
                "base_branch": base_branch,
                "head_sha": head_sha,
                "is_draft": bool(member.get("isDraft")),
                "state": member.get("state"),
            }
        )
    members.sort(
        key=lambda item: (item["position"] is None, item["position"], item["number"])
    )
    size = raw.get("size")
    if not isinstance(size, int) or size != len(members):
        raise WorkflowError(
            f"the native stack reports {size!r} members but exposes {len(members)}"
        )
    return {
        "id": raw.get("id"),
        "number": raw.get("number"),
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
    suffix = stack["members"][index:]
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
    return {
        "result": "ready",
        "selected": suffix,
        "fingerprint": topology_fingerprint(stack),
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


def lock_holder_is_live(
    holder: dict[str, Any], *, alive: Callable[[int], bool] = common.process_is_alive
) -> bool:
    pid = holder.get("pid")
    return isinstance(pid, int) and alive(pid)


def live_recorded_workers(
    state: dict[str, Any],
    *,
    alive: Callable[[int], bool] = common.process_is_alive,
) -> list[dict[str, Any]]:
    workers = state.get("active_workers")
    if not isinstance(workers, list):
        return []
    return [
        worker
        for worker in workers
        if isinstance(worker, dict)
        and isinstance(worker.get("pid"), int)
        and alive(worker["pid"])
    ]


def live_worker_files(
    run_directory: Path,
    *,
    alive: Callable[[int], bool] = common.process_is_alive,
) -> list[dict[str, Any]]:
    records = run_directory / "workers"
    if not records.is_dir():
        return []
    live: list[dict[str, Any]] = []
    for path in records.glob("*.json"):
        worker = common.read_json(path)
        if (
            isinstance(worker, dict)
            and isinstance(worker.get("pid"), int)
            and alive(worker["pid"])
        ):
            live.append({**worker, "record_path": str(path)})
    return live


@contextmanager
def lock_guard(path: Path):
    guard_path = path.with_name(f"{path.name}.guard")
    guard_path.parent.mkdir(parents=True, exist_ok=True)
    with guard_path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def acquire_lock(
    path: Path,
    run_id: str,
    *,
    alive: Callable[[int], bool] = common.process_is_alive,
) -> dict[str, Any]:
    holder = {"run_id": run_id, "pid": os.getpid(), "created_at": utc_now()}
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_guard(path):
        existing = common.read_json(path)
        if isinstance(existing, dict) and lock_holder_is_live(
            existing, alive=alive
        ):
            return {"result": "held", "holder": existing}
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(holder, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        return {"result": "acquired", "holder": holder}


def release_lock(path: Path, run_id: str) -> None:
    with lock_guard(path):
        existing = common.read_json(path)
        if isinstance(existing, dict) and existing.get("run_id") != run_id:
            return
        try:
            path.unlink()
        except OSError:
            pass


def new_state(kickoff: dict[str, Any], run_id: str, fingerprint: str) -> dict[str, Any]:
    return {
        "state_version": STATE_VERSION,
        "kind": RUN_KIND,
        "run_id": run_id,
        "kickoff": kickoff,
        "topology_fingerprint": fingerprint,
        "selected": list(kickoff["pullRequests"]),
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


def resume_state(
    path: Path, kickoff: dict[str, Any], run_id: str, fingerprint: str
) -> dict[str, Any]:
    """Recover from durable evidence, never from how long ago something ran."""
    existing = load_state(path)
    if existing is None or existing.get("result") is not None:
        return new_state(kickoff, run_id, fingerprint)
    previous_run_id = existing.get("run_id")
    if existing.get("topology_fingerprint") != fingerprint:
        recovered = new_state(kickoff, run_id, fingerprint)
        recovered["recovered_from"] = {
            "run_id": previous_run_id,
            "reason": "topology_changed",
        }
        return recovered
    existing["run_id"] = run_id
    existing["recovered_from"] = {
        "run_id": previous_run_id,
        "reason": "resumed",
        "pass": existing.get("pass"),
        "phase": existing.get("phase"),
    }
    return existing


def worktree_ownership_path(run_directory: Path, number: int) -> Path:
    return run_directory / "worktrees" / f"{number}{OWNERSHIP_SUFFIX}"


def worktree_path(run_directory: Path, number: int) -> Path:
    return run_directory / "worktrees" / str(number)


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
        self.models = models
        self.effort = effort
        self.readiness_timeout = readiness_timeout
        self.poll_interval = poll_interval
        self.sleep = sleep
        self.monotonic = monotonic

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        number = request["number"]
        path = worktree_path(self.run_directory, number)
        record_path = worktree_ownership_path(self.run_directory, number)
        record = common.read_json(record_path)
        if path.exists() and not (
            isinstance(record, dict)
            and record.get("run_id") == self.run_id
            and record.get("path") == str(path)
        ):
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
            record["updated_at"] = utc_now()
            common.write_json_atomically(record_path, record)
            return {"result": "ready", "worktree": path, "reused": True}
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
                "created_at": utc_now(),
            },
        )
        return {"result": "ready", "worktree": path, "reused": False}

    def verify(self, request: dict[str, Any], worktree: Path) -> dict[str, Any]:
        record = common.read_json(worktree_ownership_path(self.run_directory, request["number"]))
        if not isinstance(record, dict) or record.get("run_id") != self.run_id:
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
            prompt=request["prompt"],
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
        self, request: dict[str, Any], started: dict[str, Any]
    ) -> dict[str, Any]:
        """Require durable evidence that this worker is running before the next.

        The record file and the worker's own log are written to disk, so the
        evidence survives a crash and can be read again on recovery. A worker
        that exits before producing either one is a failed launch, not a
        started one.
        """
        handle = started["handle"]
        record_path = Path(started["record_path"])
        log_path = Path(started["log_path"])
        deadline = self.monotonic() + self.readiness_timeout
        while True:
            exited = handle.poll()
            log_size = log_path.stat().st_size if log_path.exists() else 0
            if record_path.is_file() and (log_size > 0 or exited == 0):
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
                    "detail": f"the worker exited with {exited} before it wrote output",
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

    def cancel(self, started: dict[str, Any]) -> None:
        handle = started["handle"]
        if handle.poll() is None:
            handle.terminate()
            try:
                handle.wait(timeout=10)
            except subprocess.TimeoutExpired:
                handle.kill()
                handle.wait()
        record_path = Path(started["record_path"])
        record = common.read_json(record_path)
        if isinstance(record, dict):
            common.write_json_atomically(
                record_path,
                {**record, "status": "cancelled", "ended_at": utc_now()},
            )

    def is_running(self, worker: dict[str, Any]) -> bool:
        return worker["handle"].poll() is None

    def wait(self, worker: dict[str, Any]) -> dict[str, Any]:
        returncode = worker["handle"].wait()
        return {"returncode": returncode, "ended_at": utc_now()}

    def cleanup(self, number: int) -> dict[str, Any]:
        path = worktree_path(self.run_directory, number)
        if not path.exists():
            return {"result": "absent", "number": number}
        removed = common.run(
            [
                "git",
                "-C",
                str(self.repo_root),
                "worktree",
                "remove",
                "--force",
                str(path),
            ],
            check=False,
        )
        if removed.returncode != 0 and path.exists():
            shutil.rmtree(path, ignore_errors=True)
        record = worktree_ownership_path(self.run_directory, number)
        try:
            record.unlink()
        except OSError:
            pass
        return {
            "result": "removed" if not path.exists() else "failed",
            "number": number,
        }


def launch_workers(
    requests: list[dict[str, Any]],
    *,
    launcher: Any,
    report: Callable[[dict[str, Any]], None] | None = None,
    on_started: Callable[[dict[str, Any]], None] | None = None,
    on_active: Callable[[dict[str, Any]], None] | None = None,
    on_stopped: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Start workers strictly one at a time, verifying each before the next.

    A failure to create, verify, or start a worker stops every later launch in
    this dispatch. Nothing is retried and no replacement worker is created,
    because a second attempt could leave two agents working the same pull
    request. Workers that are already active keep running.
    """
    workers: list[dict[str, Any]] = []
    stopped: dict[str, Any] | None = None
    for request in requests:
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
        worktree = created["worktree"]
        verified = launcher.verify(request, worktree)
        if verified.get("result") != "verified":
            stopped = {"step": "verify", "number": request["number"], **verified}
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
            "evidence": {
                "record_path": str(started.get("record_path", "")),
                "pid": started.get("pid"),
                "observed_at": utc_now(),
            },
            "started_at": utc_now(),
        }
        if on_started is not None:
            on_started(worker)
        ready = launcher.confirm_ready(request, started)
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
    if stopped is not None:
        stopped.pop("handle", None)
        report_safely(report, "worker_launch_stopped", **stopped)
    return {"workers": workers, "stopped": stopped}


def accepted_push_checkpoints(
    repository: str,
    number: int,
    *,
    state_for: Callable[..., Path] = stage_state_path,
) -> list[dict[str, Any]]:
    """Read the CI stage's own record of the pushes it has published."""
    target = common.target_for(repository, number)
    payload = common.read_json(state_for(STAGE_BY_NAME[STAGE_CI], target))
    pushes = payload.get("accepted_pushes") if isinstance(payload, dict) else None
    return [push for push in pushes or [] if isinstance(push, dict)]


def ci_worker_progress(repository: str, number: int) -> dict[str, Any] | None:
    target = common.target_for(repository, number)
    return common.stage_live_progress(STAGE_BY_NAME[STAGE_CI], target)


def propagate_descendants(
    repository: str,
    number: int,
    head_sha: str,
    stack_number: int,
    *,
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
        run_id: str | None = None,
        report: Callable[[dict[str, Any]], None] | None = None,
        launcher: Any | None = None,
        state_path: Path | None = None,
        lock_path: Path | None = None,
        run_directory: Path | None = None,
        read_stack: Callable[..., dict[str, Any] | None] = read_native_stack,
        inspect: Callable[..., dict[str, Any]] = inspect_stage,
        base_tip: Callable[[str, str], str] = base_ref_tip,
        contains: Callable[[Path, str, str], bool] | None = None,
        checkpoints: Callable[..., list[dict[str, Any]]] = accepted_push_checkpoints,
        worker_progress: Callable[[str, int], dict[str, Any] | None] = ci_worker_progress,
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
        self.run_id = run_id or uuid.uuid4().hex
        self.report = report
        self.state_path = state_path or state_path_for(kickoff)
        self.lock_path = lock_path or lock_path_for(kickoff)
        self.run_directory = run_directory or run_directory_for(kickoff, self.run_id)
        self.launcher = launcher or WorkerLauncher(
            repo_root=repo_root,
            repository=self.repository,
            run_id=self.run_id,
            run_directory=self.run_directory,
            models=models,
            effort=effort,
        )
        self.read_stack = read_stack
        self.inspect = inspect
        self.base_tip = base_tip
        self.contains = contains or (
            lambda _root, ancestor, descendant: commit_contains(
                self.repository, ancestor, descendant
            )
        )
        self.checkpoints = checkpoints
        self.worker_progress = worker_progress
        self.propagate = propagate
        self.dependencies = dependencies
        self.sleep = sleep
        self.monitor_interval = monitor_interval
        self.nonces = nonces or (lambda: uuid.uuid4().hex)
        self.state: dict[str, Any] = {}
        self.touched: set[int] = set()
        self.propagations: list[dict[str, Any]] = []
        self.session_title: str | None = None

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
        record["stages"][stage] = {**payload, "updated_at": utc_now()}
        self.save()

    # Topology ------------------------------------------------------------

    def revalidate(self) -> dict[str, Any]:
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
        return {
            "number": member["number"],
            "stage": stage,
            "agent": entry["agent"],
            "pass": pass_number,
            "nonce": self.nonces(),
            "head_sha": member["head_sha"],
            "arguments": arguments,
            "prompt": worker_prompt(target, arguments, scope=scope),
        }

    def dispatch(
        self, requests: list[dict[str, Any]], phase: str, pass_number: int
    ) -> dict[str, Any]:
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
        def record_started(worker: dict[str, Any]) -> None:
            self.touched.add(worker["number"])
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
            self.state["active_workers"] = [
                active
                for active in self.state.get("active_workers", [])
                if active.get("nonce") != worker["nonce"]
            ]
            self.save()

        launched = launch_workers(
            requests,
            launcher=self.launcher,
            report=self.report,
            on_started=record_started,
            on_active=record_active,
            on_stopped=record_stopped,
        )
        return launched

    def finish_worker(
        self, worker: dict[str, Any], request: dict[str, Any]
    ) -> dict[str, Any]:
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
        self.emit(
            "worker_finished",
            number=completion["number"],
            stage=completion["stage"],
            pull_request_pass=request["pass"],
            returncode=completion.get("returncode"),
            accepted=completion["accepted"],
        )
        self.state["active_workers"] = [
            active
            for active in self.state.get("active_workers", [])
            if active.get("nonce") != worker["nonce"]
        ]
        self.save()
        return completion

    def clearance(
        self, number: int, stage: str, head_sha: str, base_sha: str | None
    ) -> dict[str, Any]:
        target = common.target_for(self.repository, number)
        return self.inspect(STAGE_BY_NAME[stage], target, head_sha, base_sha)

    # Phases --------------------------------------------------------------

    def run_conflict_phase(
        self, pass_number: int, selected: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Delegate the whole stack's conflicts once, for the clicked pull request.

        The conflict agent cascades a native stack itself, and that cascade can
        move members below the click, so dispatching it per member would repeat
        the same work and fight over the same branches.
        """
        clicked = next(
            member
            for member in selected
            if member["number"] == self.kickoff["startPullRequest"]
        )
        scope = (
            f"This pull request is the clicked member of native stack "
            f"{self.kickoff['stackNumber']}. Resolve the stack as a whole; the "
            "cascade may move members below it. Run Conflict Fix Loop preflight "
            "with --whole-stack."
        )
        request = self.request_for(clicked, STAGE_CONFLICT, pass_number, scope=scope)
        self.emit(
            "phase_started",
            phase=STAGE_CONFLICT,
            pull_request_pass=pass_number,
            numbers=[clicked["number"]],
            mode=PHASE_STACK_DISPATCH,
        )
        launched = self.dispatch([request], STAGE_CONFLICT, pass_number)
        completions = [
            self.finish_worker(worker, request) for worker in launched["workers"]
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
                },
            )
        result = {
            "phase": STAGE_CONFLICT,
            "mode": PHASE_STACK_DISPATCH,
            "dispatches": 1,
            "completions": completions,
            "stopped": launched["stopped"],
        }
        self.emit(
            "phase_finished",
            pull_request_pass=pass_number,
            numbers=[clicked["number"]],
            **summarize_phase(result),
        )
        return result

    def run_parallel_phase(
        self, phase: str, pass_number: int, selected: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Start one worker per selected pull request, then let them run together.

        Startup stays serialized, so exactly one worktree and one process are
        created and verified at a time. Once every worker is active they work
        concurrently.
        """
        requests = [
            self.request_for(member, phase, pass_number) for member in selected
        ]
        self.emit(
            "phase_started",
            phase=phase,
            pull_request_pass=pass_number,
            numbers=[request["number"] for request in requests],
            mode=PHASE_PARALLEL,
        )
        launched = self.dispatch(requests, phase, pass_number)
        by_number = {request["number"]: request for request in requests}
        completions = [
            self.finish_worker(worker, by_number[worker["number"]])
            for worker in launched["workers"]
        ]
        for completion in completions:
            self.record_stage(
                completion["number"],
                phase,
                {
                    "pass": pass_number,
                    "accepted": completion["accepted"],
                    "returncode": completion.get("returncode"),
                    "dispatched_head_sha": completion["head_sha"],
                },
            )
        result = {
            "phase": phase,
            "mode": PHASE_PARALLEL,
            "dispatches": len(requests),
            "completions": completions,
            "stopped": launched["stopped"],
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

        A higher member is only repaired once the member directly below it is
        green at the head it currently has, and once this member's own head
        already contains that commit. Repairing above a red or unmerged
        predecessor produces failures that belong to the predecessor.
        """
        if predecessor is None:
            return {"ready": True, "reason": "lowest_selected"}
        if not (predecessor_state or {}).get("green"):
            return {
                "ready": False,
                "reason": "predecessor_is_not_green",
                "predecessor": predecessor["number"],
            }
        predecessor_head = (predecessor_state or {}).get("head_sha")
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
        return {"ready": True, "reason": "predecessor_is_green"}

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
        last_progress_signature: str | None = None

        def sweep() -> None:
            nonlocal last_progress_signature
            propagations.extend(self.propagate_ci_pushes(request, seen))
            progress = self.worker_progress(self.repository, request["number"])
            if progress is None:
                return
            signature = json.dumps(progress, sort_keys=True)
            if signature == last_progress_signature:
                return
            last_progress_signature = signature
            self.emit(
                "worker_progress",
                number=request["number"],
                stage=STAGE_CI,
                pull_request_pass=request["pass"],
                **progress,
            )

        self.emit(
            "worker_wait_started",
            number=worker["number"],
            stage=worker["stage"],
            pull_request_pass=request["pass"],
        )
        while self.launcher.is_running(worker):
            sweep()
            self.sleep(self.monitor_interval)
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
            outcome = self.propagate(
                self.repository,
                request["number"],
                head_sha,
                self.kickoff["stackNumber"],
            )
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
                    and previous["head_sha"] != previous_state["head_sha"]
                ):
                    previous_state = {
                        "green": False,
                        "head_sha": previous["head_sha"],
                    }
                gate = self.ci_gate(member, previous, previous_state)
                if (
                    not gate["ready"]
                    and gate["reason"] == "predecessor_head_is_not_contained"
                ):
                    predecessor_head = gate["predecessor_head_sha"]
                    alignment = {
                        **self.propagate(
                            self.repository,
                            previous["number"],
                            predecessor_head,
                            self.kickoff["stackNumber"],
                        ),
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
                        if previous["head_sha"] != predecessor_head:
                            previous_state = {
                                "green": False,
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
                    {"pass": pass_number, "action": "already_clear"},
                )
                previous, previous_state = member, {
                    "green": True,
                    "head_sha": member["head_sha"],
                }
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
            green = bool(after["clear"]) and completion["accepted"]
            self.record_stage(
                member["number"],
                STAGE_CI,
                {
                    "pass": pass_number,
                    "accepted": completion["accepted"],
                    "returncode": completion.get("returncode"),
                    "clear": after["clear"],
                    "clear_at_head_sha": after.get("clear_at_head_sha"),
                },
            )
            previous = member
            previous_state = {
                "green": green,
                "head_sha": after.get("clear_at_head_sha") or member["head_sha"],
            }
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
        }
        self.emit(
            "phase_finished",
            pull_request_pass=pass_number,
            numbers=[member["number"] for member in current_selected],
            **summarize_phase(result),
        )
        return result

    # Snapshot ------------------------------------------------------------

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
        for member in opening["selected"]:
            base_sha = self.base_sha_for(member)
            target = common.target_for(self.repository, member["number"])
            stages = [
                self.inspect(entry, target, member["head_sha"], base_sha)
                for entry in STAGES
            ]
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
        complete = all(not entry["uncleared"] for entry in pull_requests)
        return {
            "result": "complete" if complete else "incomplete",
            "reason": None if complete else "stages_not_clear",
            "fingerprint": opening["fingerprint"],
            "pull_requests": pull_requests,
        }

    # Run -----------------------------------------------------------------

    def cleanup(self) -> list[dict[str, Any]]:
        return [self.launcher.cleanup(number) for number in sorted(self.touched)]

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
        self.state["result"] = result
        self.state["reason"] = reason
        self.save()
        payload = {
            "result": result,
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
            "cleanup": self.cleanup(),
        }
        if reason is not None:
            payload["reason"] = reason
        if detail is not None:
            payload["detail"] = detail
        if snapshot is not None:
            payload["snapshot"] = snapshot
        if self.session_title is not None:
            payload["session_title"] = self.session_title
        release_lock(self.lock_path, self.run_id)
        return payload

    def execute(self) -> dict[str, Any]:
        self.emit(
            "stack_pipeline_started",
            repository=self.repository,
            stack_number=self.kickoff["stackNumber"],
            start_pull_request=self.kickoff["startPullRequest"],
            selected=list(self.kickoff["pullRequests"]),
        )
        opening = self.revalidate()
        if opening["result"] != "ready":
            self.state = new_state(self.kickoff, self.run_id, "")
            return self.finish(
                "stopped",
                reason=opening["reason"],
                detail=opening.get("detail"),
            )
        fingerprint = opening["fingerprint"]
        lock = acquire_lock(self.lock_path, self.run_id)
        if lock["result"] != "acquired":
            self.state = new_state(self.kickoff, self.run_id, fingerprint)
            return {
                "result": "stopped",
                "reason": "another_run_holds_the_lock",
                "run_id": self.run_id,
                "repository": self.repository,
                "stack_number": self.kickoff["stackNumber"],
                "holder": lock["holder"],
                "state_path": str(self.state_path),
                **(
                    {"session_title": self.session_title}
                    if self.session_title is not None
                    else {}
                ),
            }
        self.state = resume_state(
            self.state_path, self.kickoff, self.run_id, fingerprint
        )
        prior_run_directory = self.state.get("run_directory")
        active_workers = live_recorded_workers(self.state)
        if isinstance(prior_run_directory, str):
            known_nonces = {worker.get("nonce") for worker in active_workers}
            active_workers.extend(
                worker
                for worker in live_worker_files(Path(prior_run_directory))
                if worker.get("nonce") not in known_nonces
            )
        if active_workers:
            release_lock(self.lock_path, self.run_id)
            return {
                "result": "incomplete",
                "reason": "previous_workers_still_active",
                "run_id": self.run_id,
                "repository": self.repository,
                "stack_number": self.kickoff["stackNumber"],
                "workers": active_workers,
                "state_path": str(self.state_path),
                **(
                    {"session_title": self.session_title}
                    if self.session_title is not None
                    else {}
                ),
            }
        self.state["active_workers"] = []
        self.state["run_directory"] = str(self.run_directory)
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
        for pass_number in range(1, MAX_PASSES + 1):
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
                if phase["mode"] == PHASE_STACK_DISPATCH:
                    outcome = self.run_conflict_phase(pass_number, selected)
                elif phase["mode"] == PHASE_BOTTOM_UP:
                    outcome = self.run_ci_phase(pass_number, selected)
                else:
                    outcome = self.run_parallel_phase(
                        phase["phase"], pass_number, selected
                    )
                phases.append(outcome)
                if outcome.get("stopped") is not None:
                    return self.finish(
                        "stopped",
                        reason="worker_launch_stopped",
                        detail=json.dumps(outcome["stopped"], sort_keys=True),
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
            snapshot = self.final_snapshot()
            self.emit(
                "snapshot_taken",
                pull_request_pass=pass_number,
                result=snapshot["result"],
                reason=snapshot.get("reason"),
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
    summary = {
        "phase": phase["phase"],
        "mode": phase["mode"],
        "dispatches": phase.get("dispatches", 0),
        "accepted": [
            completion["number"]
            for completion in phase.get("completions", [])
            if completion.get("accepted")
        ],
        "ignored": [
            completion["number"]
            for completion in phase.get("completions", [])
            if not completion.get("accepted")
        ],
        "stopped": phase.get("stopped"),
    }
    if phase.get("blocked") is not None:
        summary["blocked"] = phase["blocked"]
    return summary


def load_kickoff(args: argparse.Namespace) -> dict[str, Any]:
    if args.kickoff_file:
        raw = Path(args.kickoff_file).read_text(encoding="utf-8")
    elif args.kickoff:
        raw = args.kickoff
    else:
        raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise WorkflowError(f"the kickoff payload is not valid JSON: {error}") from error
    return parse_kickoff(payload)


validate_run_id = common.validate_run_id
read_progress_log = common.read_progress_log
watch_progress = common.watch_progress


def scheduler_command(
    args: argparse.Namespace,
    kickoff: dict[str, Any],
    repo_root: Path,
    run_id: str,
    event_log: Path,
) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
        "--kickoff",
        json.dumps(kickoff, separators=(",", ":")),
        "--repo-root",
        str(repo_root),
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


def start_scheduler(
    command: list[str], *, repo_root: Path, log_path: Path
) -> subprocess.Popen[Any]:
    return common.start_detached(command, cwd=repo_root, log_path=log_path)


def command_start(args: argparse.Namespace) -> None:
    kickoff = load_kickoff(args)
    repo_root = (
        Path(args.repo_root).resolve() if args.repo_root else common.resolve_repo_root()
    )
    run_id = uuid.uuid4().hex
    event_log = progress_log_path(kickoff, run_id)
    launch_path = launch_state_path(kickoff, run_id)
    started_at_epoch = time.time()
    common.write_json_atomically(
        launch_path,
        {
            "kind": RUN_KIND,
            "run_id": run_id,
            "kickoff": kickoff,
            "pid": None,
            "event_log": str(event_log),
            "started_at": utc_now(),
            "started_at_epoch": started_at_epoch,
        },
    )
    command = scheduler_command(args, kickoff, repo_root, run_id, event_log)
    process = start_scheduler(
        command,
        repo_root=repo_root,
        log_path=scheduler_log_path(kickoff, run_id),
    )
    try:
        common.write_json_atomically(
            launch_path,
            {
                "kind": RUN_KIND,
                "run_id": run_id,
                "kickoff": kickoff,
                "pid": process.pid,
                "event_log": str(event_log),
                "started_at": utc_now(),
                "started_at_epoch": started_at_epoch,
            },
        )
    except OSError:
        process.terminate()
        raise
    common.emit(
        {
            "event": "stack_pipeline_launched",
            "run_id": run_id,
            "pid": process.pid,
            "cursor": 0,
        }
    )


def command_watch(args: argparse.Namespace) -> None:
    kickoff = load_kickoff(args)
    run_id = validate_run_id(args.run_id)
    common.emit(
        watch_progress(
            event_log=progress_log_path(kickoff, run_id),
            launch_path=launch_state_path(kickoff, run_id),
            observer_path=observer_state_path(kickoff, run_id),
            cursor=args.cursor,
            wait_seconds=args.wait_seconds,
        )
    )


def command_run(args: argparse.Namespace) -> None:
    common.require_tools()
    kickoff = load_kickoff(args)
    repo_root = Path(args.repo_root).resolve() if args.repo_root else common.resolve_repo_root()
    event_log = Path(args.event_log).resolve() if args.event_log else None
    reporter = ProgressReporter(event_log=event_log)
    pipeline = StackPipeline(
        kickoff,
        repo_root,
        models=common.stage_models(args.stage_model),
        effort=args.effort,
        run_id=validate_run_id(args.run_id) if args.run_id else None,
        report=reporter,
    )
    result = pipeline.execute()
    reporter({"event": "stack_pipeline_finished", **result})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser(
        "run", help="run up to two bounded passes over one native stack"
    )
    run.add_argument(
        "--kickoff",
        help="the structured kickoff JSON; omit to read it from standard input",
    )
    run.add_argument(
        "--kickoff-file", help="read the structured kickoff JSON from this file"
    )
    run.add_argument(
        "--repo-root", help="the repository clone the run works from"
    )
    run.add_argument(
        "--stage-model",
        action="append",
        help="pin one stage's model as <stage>=<model>; repeatable",
    )
    run.add_argument("--effort", default=DEFAULT_EFFORT)
    run.add_argument("--run-id", help=argparse.SUPPRESS)
    run.add_argument("--event-log", help=argparse.SUPPRESS)
    run.set_defaults(function=command_run)

    start = subparsers.add_parser(
        "start", help="launch the scheduler and return a durable monitor handle"
    )
    start.add_argument(
        "--kickoff",
        help="the structured kickoff JSON; omit to read it from standard input",
    )
    start.add_argument(
        "--kickoff-file", help="read the structured kickoff JSON from this file"
    )
    start.add_argument(
        "--repo-root", help="the repository clone the run works from"
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
    watch.add_argument(
        "--kickoff",
        help="the structured kickoff JSON; omit to read it from standard input",
    )
    watch.add_argument(
        "--kickoff-file", help="read the structured kickoff JSON from this file"
    )
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
            common.emit(
                {
                    "event": PROGRESS_UPDATE_EVENT,
                    "updates": [],
                    "finished": False,
                    "monitor_failure": str(error),
                }
            )
            return 1
        if args.command == "start":
            common.emit(
                {
                    "event": "stack_pipeline_launch_failed",
                    "error": str(error),
                }
            )
            return 1
        event = {
            "event": "stack_pipeline_finished",
            "result": "error",
            "error": str(error),
        }
        event_log = getattr(args, "event_log", None)
        ProgressReporter(
            event_log=Path(event_log).resolve() if event_log else None
        )(event)
        return 1
    except KeyboardInterrupt:
        if args.command == "watch":
            common.emit(
                {
                    "event": PROGRESS_UPDATE_EVENT,
                    "updates": [],
                    "finished": False,
                    "monitor_failure": "interrupted",
                }
            )
            return 130
        if args.command == "start":
            common.emit(
                {
                    "event": "stack_pipeline_launch_failed",
                    "error": "interrupted",
                }
            )
            return 130
        event = {
            "event": "stack_pipeline_finished",
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
