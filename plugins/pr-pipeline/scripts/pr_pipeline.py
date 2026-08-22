#!/usr/bin/env python3
"""Deterministic mechanics for the PR Pipeline custom agent."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any, Iterable


STATE_VERSION = 1
DEFAULT_MAX_ITERATIONS = 2
NO_PROGRESS_LIMIT = 2
MERGEABLE_RETRY_DELAYS = (2, 4, 8)
CHECK_SETTLE_GRACE_SECONDS = 180
CHECK_COVERAGE_DEADLINE_SECONDS = 1800
IS_WINDOWS = os.name == "nt"
PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
    r"/?(?:#\S*)?$"
)
SHORT_TARGET_PATTERN = re.compile(
    r"^(?P<owner>[^/\s]+)/(?P<repo>[^#/\s]+)#(?P<number>\d+)$"
)
BARE_NUMBER_PATTERN = re.compile(r"^#?(?P<number>\d+)$")

STAGE_CONFLICT = "conflict-fix-loop"
STAGE_SELF_REVIEW = "self-review-loop"
STAGE_COPILOT_REVIEW = "copilot-review-loop"
STAGE_CI = "ci-fix-loop"
STAGE_DESCRIPTION = "pr-description"

CLAUDE_FAMILY = "claude"
DEFAULT_STAGE_MODEL = "gpt-5.6-sol"
DEFAULT_EFFORT = "high"

# Every stage runs as a non-interactive subprocess with no person to answer a
# permission prompt. ``--allow-all-tools`` is what makes non-interactive mode
# possible at all. ``--allow-all-paths`` is required on top of it because a stage
# helper reads its installed scripts and writes its run state under
# ``~/.copilot``, outside the pipeline worktree, and path verification denies
# that by default. ``--allow-all-urls`` is deliberately left off: stages reach
# GitHub through the ``gh`` CLI, which is a shell tool rather than a URL fetch.
STAGE_PERMISSION_FLAGS = ("--allow-all-tools", "--allow-all-paths")
STAGE_AUTOPILOT_FLAGS = ("--autopilot", "--max-autopilot-continues", "5")

# One wait invocation returns control regularly, while activity determines
# whether the stage itself may keep running.
STAGE_WAIT_SLICE_SECONDS = 300
STAGE_WAIT_POLL_SECONDS = 15
STAGE_INACTIVITY_LIMIT_SECONDS = 1800

# Execution order. This is the pipeline's own order and is deliberately not the
# bottleneck chain any dashboard shows. The conflict stage leads because a
# conflicted pull request cannot produce meaningful checks and may not present a
# coherent diff. The CI stage trails both review stages because those stages push
# commits and checks are slow, so fixing checks earlier would fix a head that no
# longer exists.
#
# One pass runs this order once, forward. A stage that pushes a commit stales the
# clearances behind it, but the pass finishes the order first and only then goes
# back for them, which starts the next pass.
#
# ``model`` names the model this stage runs best on. A stage that leaves it None
# runs on DEFAULT_STAGE_MODEL. ``self-review-loop`` carries its own because its
# fixed GPT evaluator has to stay in a different family from the agent it grades.
# A per-stage model is a starting point rather than a rule: a ``--stage-model``
# override at preflight beats it, and the ``requires_family`` gate still checks
# whatever model the stage ends up with.
STAGES: tuple[dict[str, Any], ...] = (
    {
        "stage": STAGE_CONFLICT,
        "plugin": STAGE_CONFLICT,
        "agent": f"{STAGE_CONFLICT}:{STAGE_CONFLICT}",
        "module": "conflict_fix_loop",
        "evidence": "github",
        "requires_family": None,
        "model": None,
        "summary": "resolve merge conflicts with the base branch",
    },
    {
        "stage": STAGE_SELF_REVIEW,
        "plugin": STAGE_SELF_REVIEW,
        "agent": f"{STAGE_SELF_REVIEW}:{STAGE_SELF_REVIEW}",
        "module": "self_review_loop",
        "evidence": "helper",
        "requires_family": CLAUDE_FAMILY,
        "model": "claude-opus-5",
        "summary": "review the diff and commit the verified fixes",
    },
    {
        "stage": STAGE_COPILOT_REVIEW,
        "plugin": STAGE_COPILOT_REVIEW,
        "agent": f"{STAGE_COPILOT_REVIEW}:{STAGE_COPILOT_REVIEW}",
        "module": "copilot_review_loop",
        "evidence": "helper",
        "requires_family": None,
        "model": None,
        "summary": "address the Copilot review comments",
    },
    {
        "stage": STAGE_CI,
        "plugin": STAGE_CI,
        "agent": f"{STAGE_CI}:{STAGE_CI}",
        "module": "ci_fix_loop",
        "evidence": "github",
        "requires_family": None,
        "model": None,
        "summary": "fix the failing checks this pull request caused",
    },
    {
        "stage": STAGE_DESCRIPTION,
        "plugin": STAGE_DESCRIPTION,
        "agent": f"{STAGE_DESCRIPTION}:{STAGE_DESCRIPTION}",
        "module": "pr_description",
        "evidence": "helper",
        "requires_family": None,
        "model": None,
        "summary": "validate or replace the title and description",
    },
)
STAGE_NAMES = tuple(entry["stage"] for entry in STAGES)
STAGE_BY_NAME = {entry["stage"]: entry for entry in STAGES}
STAGE_INDEX = {entry["stage"]: index for index, entry in enumerate(STAGES)}
HELPER_EVIDENCE_STAGES = tuple(
    entry["stage"] for entry in STAGES if entry["evidence"] == "helper"
)

STAGE_OUTCOMES = ("cleared", "skipped", "no_progress", "escalated", "carried")
CLEARING_OUTCOMES = ("cleared", "skipped")
# An outcome and a head SHA account for a clearance on their own. Nothing
# accounts for a stall or a surrender except a sentence someone wrote.
DETAIL_REQUIRED_OUTCOMES = ("no_progress", "escalated")

# A carried stage could not clear on this pass but has budget left. It is set
# aside for the rest of the pass and picked up again on the next one. A machine
# reading always means the stage hit its own per-pass iteration cap; a process
# that died before recording an outcome is carried under its own reason.
CARRIED_REASONS = ("max_iterations_reached", "process_exited_without_outcome")

# A stage scopes a budget to one pass of the pipeline by resetting when this
# pair changes. The run is opaque and compared only for equality; the iteration
# is compared for order, and only against an iteration of the same run.
PIPELINE_RUN_FLAG = "--pipeline-run"
PIPELINE_ITERATION_FLAG = "--pipeline-iteration"
PIPELINE_MAX_ITERATIONS_FLAG = "--pipeline-max-iterations"

ESCALATION_ACTIONS = {
    "checks_never_registered": (
        "Check whether the repository skips these checks on a draft pull "
        "request. If it does, take the pull request out of draft yourself, or "
        "start the pipeline again once the checks can run."
    ),
    "max_iterations_reached": (
        "Read the tail of the kept stage logs, decide what still needs a human, "
        "and start the remaining stage yourself."
    ),
    "stage_escalated": (
        "Read the tail of the kept stage log, which holds the reason the stage "
        "stopped."
    ),
    "no_progress": (
        "Read the tail of the kept stage log. The stage ran twice without "
        "changing anything, so it needs a decision the pipeline cannot make."
    ),
    "pr_not_open": "Reopen the pull request or start the pipeline on an open one.",
    "helper_missing": (
        "Install the missing plugin from the trask-plugins marketplace, then start "
        "the pipeline again."
    ),
    "model_gate": (
        "Start the pipeline again where it can pin a model for every stage."
    ),
    "process_exited_without_outcome": (
        "Read the tail of the stage log named in the escalation. The stage "
        "process ended before its helper recorded an outcome, so the log is the "
        "only account of what it did."
    ),
    "stage_inactive": (
        "Read the tail of the stage log named in the escalation. The stage process "
        "tree was terminated after 30 minutes with no observable activity."
    ),
    "stage_abandoned": (
        "Read the tail of the stage log named in the escalation. The stage "
        "process is gone and never recorded an outcome, so nothing knows how "
        "far it got or whether its work is sound. Judge that yourself, then "
        "start the pipeline again."
    ),
    "dirty_worktree_before_run": (
        "The pipeline worktree had uncommitted changes before any stage ran, so "
        "they are yours. Commit, stash, or discard them, then start the pipeline "
        "again."
    ),
    "local_head_ahead_of_remote": (
        "A stage committed without pushing, so the local branch is ahead of the "
        "pull request head. Push or reconcile the branch yourself, then start "
        "the pipeline again."
    ),
    "local_head_holds_unreachable_commits": (
        "The worktree's HEAD holds commits that no branch, remote-tracking ref, "
        "or tag contains, so moving it would leave them unreachable. Push them, "
        "or put a branch on them, then start the pipeline again."
    ),
    "local_head_diverged_from_remote": (
        "The worktree is on the pull request's branch but its commits are not "
        "the pull request's. Reconcile the branch yourself, then start the "
        "pipeline again."
    ),
    "checkout_pr_head_failed": (
        "The pipeline could not put its worktree on the pull request head. "
        "Check out the pull request head by hand, then start the pipeline again."
    ),
    "worktree_reset_failed": (
        "The pipeline could not return its worktree to a clean state between "
        "stages. Clean the worktree by hand, then start the pipeline again."
    ),
}

CHECK_SUCCESS_STATES = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
CHECK_FAILURE_STATES = frozenset(
    {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "ACTION_REQUIRED"}
)
CHECK_PENDING_STATES = frozenset(
    {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED", "STALE"}
)

# How the pipeline's own worktree stands against the pull request head. Four
# answers, because the count of commits the local head holds beyond the pull
# request head cannot tell an extra commit from an unrelated history, and those
# two need opposite treatment: one is a stage's work that must not be lost, the
# other is a fresh session that has not been put on the pull request yet.
LOCAL_HEAD_AT_PR_HEAD = "at_pr_head"
LOCAL_HEAD_AHEAD = "ahead"
LOCAL_HEAD_DIVERGED = "diverged"
# Commits that no ref but HEAD holds. Moving HEAD would leave them unreferenced,
# whatever their ancestry says, so they are the one thing a checkout must never
# step over.
LOCAL_HEAD_UNREACHABLE = "unreachable"
LOCAL_HEAD_NEEDS_CHECKOUT = "checkout_required"
# The facts could not be read, so the pipeline neither escalates nor moves the
# worktree. An answer never read is not one to act on in either direction.
LOCAL_HEAD_UNKNOWN = "unknown"

# The verdicts that hold commits the pipeline must not check out over, and the
# escalation reason each one is reported under.
LOCAL_HEAD_ESCALATIONS = {
    LOCAL_HEAD_AHEAD: "local_head_ahead_of_remote",
    LOCAL_HEAD_UNREACHABLE: "local_head_holds_unreachable_commits",
    LOCAL_HEAD_DIVERGED: "local_head_diverged_from_remote",
}

# What the process a run recorded as running turns out to be. Four answers,
# because "not alive" and "not answerable" are different facts: one is evidence
# the stage is gone, the other is the absence of evidence either way, and only
# the first may stop the pipeline.
RUNNING_STAGE_ALIVE = "alive"
RUNNING_STAGE_FINISHED = "finished"
RUNNING_STAGE_ABANDONED = "abandoned"
RUNNING_STAGE_UNVERIFIABLE = "unverifiable"

# What each verdict means for the caller. None of the three below stops the
# pipeline, and none of them advances it either: a stage is still recorded as
# running, so the next thing to do is finish that stage, not start another.
RUNNING_STAGE_DETAILS = {
    RUNNING_STAGE_ALIVE: (
        "the stage process is still running, so wait for it rather than "
        "deciding anything else"
    ),
    RUNNING_STAGE_FINISHED: (
        "the stage recorded its result and is still recorded as running, so "
        "finish it before asking what comes next"
    ),
    RUNNING_STAGE_UNVERIFIABLE: (
        "the stage's process cannot be identified from what the run recorded, "
        "so whether it is still running is unknown; read its log and finish or "
        "escalate the stage yourself"
    ),
}


class WorkflowError(RuntimeError):
    pass


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            input=input_text,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise WorkflowError(
            f"{' '.join(command)} did not return within {timeout} seconds"
        ) from error
    if check and process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "no output"
        raise WorkflowError(
            f"{' '.join(command)} failed ({process.returncode}): {detail}"
        )
    return process


def git(repo_root: Path, *arguments: str) -> str:
    return run(["git", "-C", str(repo_root), *arguments]).stdout.strip()


WINDOWS_EPOCH_TICKS = 621355968000000000
TICKS_PER_SECOND = 10000000


def process_create_time(pid: int) -> float | None:
    """The creation time of a process, used to tell it from a reused pid.

    Windows reuses process ids, so a bare pid can name a different program by the
    time a later ``wait`` checks it. Pairing the pid with its creation time makes
    a stale match detectable. ``None`` means the platform could not answer, and a
    caller treats that as "cannot confirm identity" rather than as a match.

    The value is in seconds, so that comparing two of them stays meaningful. The
    Windows reading arrives as 100-nanosecond ticks since year 1, a number so
    large that a difference of a second or less disappears into floating point
    rounding, which would silently make a recycled pid look like a match.
    """

    try:
        import psutil  # type: ignore
    except Exception:
        return _process_create_time_native(pid)
    try:
        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


def _process_create_time_native(pid: int) -> float | None:
    if IS_WINDOWS:
        try:
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "(Get-Process -Id "
                    f"{pid}"
                    " -ErrorAction Stop).StartTime.ToUniversalTime().Ticks",
                ],
                text=True,
                capture_output=True,
                timeout=10,
            )
        except Exception:
            return None
        text = completed.stdout.strip()
        if completed.returncode != 0 or not text:
            return None
        try:
            return (float(text) - WINDOWS_EPOCH_TICKS) / TICKS_PER_SECOND
        except ValueError:
            return None
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
        return float(fields[19]) / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None


PROCESS_IDENTITY_TOLERANCE = 1.0


def process_alive(pid: int, create_time: float | None) -> bool:
    """Whether the launched stage process is still running.

    A recorded creation time guards against a reused pid: a live process whose
    creation time no longer matches is a different program, so the stage is
    treated as gone rather than alive.
    """

    try:
        import psutil  # type: ignore
    except Exception:
        psutil = None  # type: ignore
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
        except Exception:
            return False
        if create_time is not None:
            try:
                if (
                    abs(float(proc.create_time()) - create_time)
                    > PROCESS_IDENTITY_TOLERANCE
                ):
                    return False
            except Exception:
                return False
        return proc.is_running() and proc.status() != psutil.ZOMBIE
    current = _process_create_time_native(pid)
    if current is None:
        return False
    if (
        create_time is not None
        and abs(current - create_time) > PROCESS_IDENTITY_TOLERANCE
    ):
        return False
    return True


def process_exited(pid: int, create_time: float | None) -> bool:
    """Whether the recorded process is confirmed gone rather than merely unreadable."""

    try:
        import psutil  # type: ignore
    except ImportError:
        current = _process_create_time_native(pid)
        return current is not None and (
            create_time is not None
            and abs(current - create_time) > PROCESS_IDENTITY_TOLERANCE
        )
    try:
        process = psutil.Process(pid)
        current = float(process.create_time())
        running = process.is_running()
        status = process.status()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return True
    except psutil.AccessDenied:
        return False
    if (
        create_time is not None
        and abs(current - create_time) > PROCESS_IDENTITY_TOLERANCE
    ):
        return True
    return not running or status == psutil.ZOMBIE


def _psutil_process_tree_snapshot(
    pid: int, create_time: float | None
) -> dict[str, Any] | None:
    try:
        import psutil  # type: ignore
    except ImportError:
        return None
    try:
        root = psutil.Process(pid)
        if create_time is not None and (
            abs(float(root.create_time()) - create_time) > PROCESS_IDENTITY_TOLERANCE
        ):
            return None
        processes = [root, *root.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None

    identities: list[str] = []
    process_cpu: dict[str, float] = {}
    cpu_seconds = 0.0
    for process in processes:
        try:
            identity = f"{process.pid}:psutil:{float(process.create_time()):.6f}"
            cpu = process.cpu_times()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        identities.append(identity)
        used_cpu = float(cpu.user) + float(cpu.system)
        process_cpu[identity] = round(used_cpu, 6)
        cpu_seconds += used_cpu
    return {
        "process_tree": sorted(identities),
        "process_cpu": process_cpu,
        "cpu_seconds": round(cpu_seconds, 6),
    }


def _native_process_rows() -> list[dict[str, Any]] | None:
    if IS_WINDOWS:
        command = (
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,ParentProcessId,CreationDate,"
            "KernelModeTime,UserModeTime | ConvertTo-Json -Compress"
        )
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return None
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return None
        rows = payload if isinstance(payload, list) else [payload]
        return [row for row in rows if isinstance(row, dict)]

    rows: list[dict[str, Any]] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    for path in proc.iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        try:
            rows.append(
                {
                    "ProcessId": int(path.name),
                    "ParentProcessId": int(fields[1]),
                    "ProcessGroupId": int(fields[2]),
                    "State": fields[0],
                    "CreationDate": fields[19],
                    "KernelModeTime": float(fields[12]),
                    "UserModeTime": float(fields[11]),
                }
            )
        except (ValueError, IndexError):
            continue
    return rows


def _descendant_rows(rows: list[dict[str, Any]], root_pid: int) -> list[dict[str, Any]]:
    by_parent: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        try:
            parent = int(row["ParentProcessId"])
        except (KeyError, TypeError, ValueError):
            continue
        by_parent.setdefault(parent, []).append(row)
    selected: list[dict[str, Any]] = []
    pending = [root_pid]
    seen = {root_pid}
    while pending:
        parent = pending.pop()
        for row in by_parent.get(parent, []):
            try:
                child = int(row["ProcessId"])
            except (KeyError, TypeError, ValueError):
                continue
            if child in seen:
                continue
            seen.add(child)
            selected.append(row)
            pending.append(child)
    root = next(
        (
            row
            for row in rows
            if str(row.get("ProcessId")) == str(root_pid)
        ),
        None,
    )
    return ([root] if root is not None else []) + selected


def process_tree_snapshot(
    pid: int, create_time: float | None
) -> dict[str, Any] | None:
    if IS_WINDOWS:
        snapshot = _psutil_process_tree_snapshot(pid, create_time)
        if snapshot is not None:
            return snapshot
    current_create_time = _process_create_time_native(pid)
    root_reused = current_create_time is not None and (
        create_time is not None
        and abs(current_create_time - create_time) > PROCESS_IDENTITY_TOLERANCE
    )
    rows = _native_process_rows()
    if rows is None:
        return None
    if IS_WINDOWS:
        selected = _descendant_rows(rows, pid)
        if root_reused:
            selected = []
    else:
        selected = [
            row
            for row in rows
            if str(row.get("ProcessGroupId")) == str(pid)
            and str(row.get("State") or "").upper() != "Z"
        ]
    identities: list[str] = []
    process_cpu: dict[str, float] = {}
    cpu_seconds = 0.0
    divisor = 10_000_000.0 if IS_WINDOWS else float(os.sysconf("SC_CLK_TCK"))
    for row in selected:
        try:
            process_id = int(row["ProcessId"])
            identity = str(row.get("CreationDate") or "")
            kernel = float(row.get("KernelModeTime") or 0)
            user = float(row.get("UserModeTime") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        process_identity = f"{process_id}:native:{identity}"
        used_cpu = (kernel + user) / divisor
        identities.append(process_identity)
        process_cpu[process_identity] = round(used_cpu, 6)
        cpu_seconds += used_cpu
    return {
        "process_tree": sorted(identities),
        "process_cpu": process_cpu,
        "cpu_seconds": round(cpu_seconds, 6),
    }


def matching_known_process_ids(identities: Iterable[str]) -> tuple[list[int], bool]:
    parsed: list[tuple[int, str, str]] = []
    for identity in identities:
        parts = identity.split(":", 2)
        if len(parts) != 3:
            continue
        try:
            parsed.append((int(parts[0]), parts[1], parts[2]))
        except ValueError:
            continue

    unknown = False
    matches: list[int] = []
    psutil_entries = [entry for entry in parsed if entry[1] == "psutil"]
    if psutil_entries:
        try:
            import psutil  # type: ignore
        except ImportError:
            unknown = True
        else:
            for process_id, _, token in psutil_entries:
                try:
                    process = psutil.Process(process_id)
                    same = (
                        abs(float(process.create_time()) - float(token))
                        <= PROCESS_IDENTITY_TOLERANCE
                    )
                    if (
                        same
                        and process.is_running()
                        and process.status() != psutil.ZOMBIE
                    ):
                        matches.append(process_id)
                except (ValueError, psutil.NoSuchProcess, psutil.ZombieProcess):
                    continue
                except psutil.AccessDenied:
                    unknown = True

    native_entries = [entry for entry in parsed if entry[1] == "native"]
    if native_entries:
        rows = _native_process_rows()
        if rows is None:
            unknown = True
        else:
            current = {
                int(row["ProcessId"]): str(row.get("CreationDate") or "")
                for row in rows
                if row.get("ProcessId") is not None
                and str(row.get("State") or "").upper() != "Z"
            }
            for process_id, _, token in native_entries:
                if current.get(process_id) == token:
                    matches.append(process_id)
    return sorted(set(matches)), unknown


def known_processes_alive(identities: Iterable[str]) -> tuple[bool, bool]:
    matches, unknown = matching_known_process_ids(identities)
    return bool(matches), unknown


def known_process_snapshot(identities: Iterable[str]) -> dict[str, Any]:
    parsed: list[tuple[int, str, str, str]] = []
    for identity in identities:
        parts = identity.split(":", 2)
        if len(parts) != 3:
            continue
        try:
            parsed.append((int(parts[0]), parts[1], parts[2], identity))
        except ValueError:
            continue

    live_identities: list[str] = []
    process_cpu: dict[str, float] = {}
    cpu_seconds = 0.0
    try:
        import psutil  # type: ignore
    except ImportError:
        psutil = None  # type: ignore
    if psutil is not None:
        for process_id, source, token, identity in parsed:
            if source != "psutil":
                continue
            try:
                process = psutil.Process(process_id)
                if (
                    abs(float(process.create_time()) - float(token))
                    > PROCESS_IDENTITY_TOLERANCE
                    or not process.is_running()
                    or process.status() == psutil.ZOMBIE
                ):
                    continue
                cpu = process.cpu_times()
            except (
                ValueError,
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess,
            ):
                continue
            live_identities.append(identity)
            used_cpu = float(cpu.user) + float(cpu.system)
            process_cpu[identity] = round(used_cpu, 6)
            cpu_seconds += used_cpu

    native = [entry for entry in parsed if entry[1] == "native"]
    if native:
        rows = _native_process_rows() or []
        by_pid = {
            int(row["ProcessId"]): row
            for row in rows
            if row.get("ProcessId") is not None
            and str(row.get("State") or "").upper() != "Z"
        }
        divisor = 10_000_000.0 if IS_WINDOWS else float(os.sysconf("SC_CLK_TCK"))
        for process_id, _, token, identity in native:
            row = by_pid.get(process_id)
            if row is None or str(row.get("CreationDate") or "") != token:
                continue
            live_identities.append(identity)
            used_cpu = (
                float(row.get("KernelModeTime") or 0)
                + float(row.get("UserModeTime") or 0)
            ) / divisor
            process_cpu[identity] = round(used_cpu, 6)
            cpu_seconds += used_cpu
    return {
        "process_tree": sorted(set(live_identities)),
        "process_cpu": process_cpu,
        "cpu_seconds": round(cpu_seconds, 6),
    }


def stage_process_tree_alive(
    pid: int,
    create_time: float | None,
    tracker: dict[str, Any] | None = None,
) -> bool:
    if process_alive(pid, create_time):
        return True
    snapshot = process_tree_snapshot(pid, create_time)
    if snapshot is None:
        return True
    if snapshot.get("process_tree"):
        return True
    known = []
    if isinstance(tracker, dict):
        known = tracker.get("known_processes") or tracker.get("process_tree") or []
    alive, unknown = known_processes_alive(known)
    return alive or unknown


def terminate_process_tree(
    pid: int,
    create_time: float | None,
    tracker: dict[str, Any] | None = None,
) -> list[int] | None:
    current_create_time = process_create_time(pid)
    if (
        current_create_time is not None
        and create_time is not None
        and abs(current_create_time - create_time) > PROCESS_IDENTITY_TOLERANCE
    ):
        raise WorkflowError("the inactive stage pid now belongs to another process")
    if not IS_WINDOWS:
        snapshot = process_tree_snapshot(pid, create_time)
        process_ids = [
            int(identity.split(":", 1)[0])
            for identity in (snapshot or {}).get("process_tree") or []
        ]
        known = (
            tracker.get("known_processes") or tracker.get("process_tree") or []
            if isinstance(tracker, dict)
            else []
        )
        known_ids, known_unknown = matching_known_process_ids(known)
        process_ids = sorted(set(process_ids) | set(known_ids))
        signaled = False
        try:
            os.killpg(pid, signal.SIGTERM)
            signaled = True
        except ProcessLookupError:
            pass
        except PermissionError as error:
            raise WorkflowError(
                f"could not terminate the inactive stage process tree: {error}"
            ) from error
        for process_id in known_ids:
            try:
                os.kill(process_id, signal.SIGTERM)
                signaled = True
            except ProcessLookupError:
                continue
            except PermissionError as error:
                raise WorkflowError(
                    f"could not terminate inactive stage process {process_id}: {error}"
                ) from error
        if not signaled:
            return None
        deadline = time.monotonic() + 10
        while (
            stage_process_tree_alive(pid, create_time, tracker)
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)
        if stage_process_tree_alive(pid, create_time, tracker):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            for process_id in known_ids:
                try:
                    os.kill(process_id, signal.SIGKILL)
                except ProcessLookupError:
                    continue
                except PermissionError as error:
                    raise WorkflowError(
                        f"could not kill inactive stage process {process_id}: {error}"
                    ) from error
        if stage_process_tree_alive(pid, create_time, tracker) or known_unknown:
            raise WorkflowError("inactive stage process group did not exit")
        return process_ids

    try:
        import psutil  # type: ignore
    except ImportError:
        psutil = None  # type: ignore
    if psutil is not None:
        processes: list[Any] = []
        try:
            root = psutil.Process(pid)
            if create_time is not None and (
                abs(float(root.create_time()) - create_time)
                > PROCESS_IDENTITY_TOLERANCE
            ):
                raise WorkflowError("the inactive stage pid now belongs to another process")
            processes = [*root.children(recursive=True), root]
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            psutil = None  # type: ignore
        except psutil.AccessDenied as error:
            raise WorkflowError(
                f"could not inspect the inactive stage process tree: {error}"
            ) from error
        if psutil is not None:
            known = (
                tracker.get("known_processes") or tracker.get("process_tree") or []
                if isinstance(tracker, dict)
                else []
            )
            known_ids, known_unknown = matching_known_process_ids(known)
            represented = {process.pid for process in processes}
            for known_id in known_ids:
                if known_id in represented:
                    continue
                try:
                    processes.insert(-1, psutil.Process(known_id))
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    continue
                except psutil.AccessDenied:
                    known_unknown = True
            process_ids = [process.pid for process in processes]
            for process in processes:
                try:
                    process.terminate()
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    continue
                except psutil.AccessDenied as error:
                    raise WorkflowError(
                        f"could not terminate inactive stage process {process.pid}: "
                        f"{error}"
                    ) from error
            _, alive = psutil.wait_procs(processes, timeout=10)
            for process in alive:
                try:
                    process.kill()
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    continue
                except psutil.AccessDenied as error:
                    raise WorkflowError(
                        f"could not kill inactive stage process {process.pid}: {error}"
                    ) from error
            if alive:
                _, alive = psutil.wait_procs(alive, timeout=10)
            if alive:
                raise WorkflowError(
                    "inactive stage processes did not exit: "
                    + ", ".join(str(process.pid) for process in alive)
                )
            if known_unknown:
                raise WorkflowError(
                    "could not confirm that every observed stage process exited"
                )
            return process_ids

    if IS_WINDOWS:
        rows = _native_process_rows()
        if rows is None:
            raise WorkflowError("could not inspect the inactive stage process tree")
        selected_rows = list(reversed(_descendant_rows(rows, pid)))
        selected_identities = {
            int(row["ProcessId"]): str(row.get("CreationDate") or "")
            for row in selected_rows
            if row.get("ProcessId") is not None
        }
        known = (
            tracker.get("known_processes") or tracker.get("process_tree") or []
            if isinstance(tracker, dict)
            else []
        )
        known_ids, known_unknown = matching_known_process_ids(known)
        fresh_rows = _native_process_rows()
        if fresh_rows is None:
            raise WorkflowError(
                "could not revalidate the inactive stage process tree"
            )
        fresh_identities = {
            int(row["ProcessId"]): str(row.get("CreationDate") or "")
            for row in fresh_rows
            if row.get("ProcessId") is not None
        }
        process_ids = sorted(
            {
                process_id
                for process_id, identity in selected_identities.items()
                if fresh_identities.get(process_id) == identity
            }
            | set(known_ids),
            reverse=True,
        )
        if not process_ids:
            if known_unknown:
                raise WorkflowError(
                    "could not confirm that every observed stage process exited"
                )
            return None
        literal_ids = ",".join(str(process_id) for process_id in process_ids)
        command = (
            f"@({literal_ids}) | ForEach-Object "
            "{ Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"
        )
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired as error:
            raise WorkflowError(
                "timed out terminating the inactive stage process tree"
            ) from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "no error detail"
            raise WorkflowError(
                f"could not terminate the inactive stage process tree: {detail}"
            )
        remaining_rows = _native_process_rows()
        if remaining_rows is None:
            raise WorkflowError(
                "could not confirm that the inactive stage process tree exited"
            )
        remaining_ids = {
            int(row["ProcessId"])
            for row in remaining_rows
            if row.get("ProcessId") is not None
        }
        survivors = sorted(set(process_ids) & remaining_ids)
        if survivors:
            raise WorkflowError(
                "inactive stage processes did not exit: "
                + ", ".join(str(process_id) for process_id in survivors)
            )
        if known_unknown:
            raise WorkflowError(
                "could not confirm that every observed stage process exited"
            )
        return process_ids


def worktree_dirt(repo_root: Path) -> str:
    """The porcelain status the stage preflights key on, tracked and untracked.

    Untracked files ignored by ``.gitignore`` never appear here, so a warm
    gitignored ``build/`` directory reads as clean. Everything a stage preflight
    would refuse does appear.
    """

    return git(repo_root, "status", "--porcelain=v1")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)


def gh_json(arguments: list[str]) -> Any:
    output = run(["gh", *arguments]).stdout
    try:
        return json.loads(output) if output.strip() else None
    except json.JSONDecodeError as error:
        raise WorkflowError(f"gh returned invalid JSON: {error}") from error


def graphql(query: str, variables: dict[str, str | int | None]) -> Any:
    arguments = ["api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        if value is None:
            arguments.extend(["-F", f"{name}=null"])
        else:
            flag = "-F" if isinstance(value, int) else "-f"
            arguments.extend([flag, f"{name}={value}"])
    payload = gh_json(arguments)
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if errors:
        raise WorkflowError(f"GraphQL failed: {json.dumps(errors, sort_keys=True)}")
    return payload


def base_ref_tip(repo_name: str, base_branch: str) -> str:
    """Return the live tip commit of a pull request's base branch.

    GitHub's ``baseRefOid`` freezes at the moment the pull request was created
    or last synced and does not follow the base branch as it moves, so reading
    it names a commit the base branch has since left behind. The branch ref
    always names the current tip, so this reads that instead, and ``base_sha``
    always means the current base tip.

    A base branch that has been deleted or is otherwise unreadable is a hard
    error. Falling back to the frozen ``baseRefOid`` would silently restore the
    staleness this exists to remove, and nothing downstream would see it happen.
    """
    result = run(
        ["gh", "api", f"repos/{repo_name}/git/ref/heads/{base_branch}"],
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise WorkflowError(
            f"could not read the tip of base branch {base_branch!r} in {repo_name}; "
            f"it may have been deleted: {detail}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise WorkflowError(
            f"reading the tip of base branch {base_branch!r} in {repo_name} "
            f"returned invalid JSON: {error}"
        ) from error
    obj = payload.get("object") if isinstance(payload, dict) else None
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if not isinstance(sha, str) or not sha:
        raise WorkflowError(
            f"the tip of base branch {base_branch!r} in {repo_name} has no commit SHA"
        )
    return sha


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> dt.datetime | None:
    """Read one of this helper's own timestamps back, or give up quietly."""

    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def elapsed_seconds(start: Any, end: Any) -> float | None:
    """Seconds between two recorded timestamps, or ``None`` if either is unreadable."""

    first = parse_utc(start)
    second = parse_utc(end)
    if first is None or second is None:
        return None
    return max((second - first).total_seconds(), 0.0)


def require_tools() -> None:
    missing = [name for name in ("git", "gh") if shutil.which(name) is None]
    if missing:
        raise WorkflowError(f"required tools not found: {', '.join(missing)}")


SHIM_SUFFIXES = (".cmd", ".bat")


def path_image(name: str) -> str | None:
    """Find a directly executable image for a bare command name on PATH.

    ``shutil.which`` answers what Windows would run, which is whichever entry
    comes first including a shim. This looks past a shim for a real image
    anywhere on PATH, because the two are usually different installs of the same
    tool and only one of them can carry the stage's prompt.
    """

    suffixes = [
        suffix
        for suffix in os.environ.get("PATHEXT", ".EXE").split(os.pathsep)
        if suffix and suffix.lower() not in SHIM_SUFFIXES
    ]
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for suffix in suffixes:
            candidate = Path(directory) / f"{name}{suffix}"
            if candidate.is_file():
                return str(candidate)
    return None


def resolve_launch_program(name: str) -> str:
    """Resolve a stage's program to something that can carry its prompt.

    A stage is launched with a prompt that spans several lines, and Windows
    hands a ``.cmd`` or ``.bat`` shim one command line rather than an argument
    list. Everything from the first newline on is dropped, and the shim still
    exits reporting success, so the stage runs without the pipeline's position
    and without its instruction and then reports nothing. That is
    indistinguishable from a stage that had nothing to do, which is the one
    thing the pipeline must never confuse. So a shim is refused by name.
    """

    if not IS_WINDOWS:
        return name
    if os.sep in name or (os.altsep and os.altsep in name):
        if Path(name).suffix.lower() in SHIM_SUFFIXES:
            raise WorkflowError(
                f"the stage program {name} is a shim, and a shim cannot carry "
                "an argument containing a newline; name the executable itself"
            )
        return name
    if Path(name).suffix:
        resolved = shutil.which(name)
        if resolved is None:
            raise WorkflowError(f"the stage program {name} is not on PATH")
        if Path(resolved).suffix.lower() in SHIM_SUFFIXES:
            raise WorkflowError(
                f"the stage program {name} resolves to the shim {resolved}, and "
                "a shim cannot carry an argument containing a newline"
            )
        return resolved
    image = path_image(name)
    if image is not None:
        return image
    resolved = shutil.which(name)
    if resolved is None:
        raise WorkflowError(f"the stage program {name} is not on PATH")
    raise WorkflowError(
        f"the stage program {name} resolves only to the shim {resolved}, and a "
        "shim cannot carry an argument containing a newline: the prompt would "
        "be cut at its first line and the stage would report success having "
        "done nothing. Install the executable, or put it ahead on PATH."
    )


def normalize_cli_path(value: str, *, windows: bool) -> str:
    if windows:
        match = re.fullmatch(r"/([A-Za-z])(?:/(.*))?", value)
        if match:
            drive, remainder = match.groups()
            value = f"{drive.upper()}:/{remainder or ''}"
    return value


def cli_path(value: str) -> Path:
    return Path(normalize_cli_path(value, windows=IS_WINDOWS)).resolve()


def copilot_home() -> Path:
    value = os.environ.get("COPILOT_HOME", "").strip()
    if value:
        return Path(normalize_cli_path(value, windows=IS_WINDOWS))
    return Path.home() / ".copilot"


def parse_target(target: str, repo_name: str | None = None) -> dict[str, Any]:
    match = PR_URL_PATTERN.fullmatch(target) or SHORT_TARGET_PATTERN.fullmatch(target)
    if match:
        values = match.groupdict()
        return build_target(values["owner"], values["repo"], int(values["number"]))
    bare = BARE_NUMBER_PATTERN.fullmatch(target)
    if bare and repo_name:
        owner, _, repo = repo_name.partition("/")
        if owner and repo:
            return build_target(owner, repo, int(bare.group("number")))
    if bare:
        raise WorkflowError("a bare PR number requires repository context")
    raise WorkflowError(
        "target must be a GitHub PR URL, owner/repo#number, or bare PR number"
    )


def build_target(owner: str, repo: str, number: int) -> dict[str, Any]:
    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        "repo_name": f"{owner}/{repo}",
        "pr_url": f"https://github.com/{owner}/{repo}/pull/{number}",
    }


def default_state_path(target: dict[str, Any]) -> Path:
    name = f"{target['owner']}--{target['repo']}--{target['number']}.json"
    return Path.home() / ".copilot" / "run" / "pr-pipeline" / name


def stage_state_path(plugin: str, target: dict[str, Any]) -> Path:
    name = f"{target['owner']}--{target['repo']}--{target['number']}.json"
    return Path.home() / ".copilot" / "run" / plugin / name


def stage_log_path(target: dict[str, Any], stage: str, iteration: int) -> Path:
    """Where one stage subprocess writes its combined output.

    The path sits beside the run state rather than in a temporary directory, so a
    later reader can find it from the history entry that records it. Its name
    carries the pull request, the stage, and the iteration, so one run's stages
    never overwrite each other and a re-run at a higher iteration keeps its own
    log.
    """

    name = (
        f"{target['owner']}--{target['repo']}--{target['number']}"
        f"--{stage}--{iteration}.log"
    )
    return copilot_home() / "run" / "pr-pipeline" / "logs" / name


def stage_script_path(entry: dict[str, Any]) -> Path:
    return (
        copilot_home()
        / "installed-plugins"
        / "trask-plugins"
        / entry["plugin"]
        / "scripts"
        / f"{entry['module']}.py"
    )


def stage_installed(entry: dict[str, Any]) -> bool:
    """Report whether a stage's plugin is installed.

    Every stage needs this, including the two whose greenness comes from GitHub.
    Being installed and being green are separate facts: a passing check rollup
    says nothing about whether the agent that fixes checks can be launched. An
    unresolvable plugin-qualified agent name falls back to the default agent
    without an error, so a stage that is not installed has to stop the pipeline
    rather than launch a general-purpose agent against a real pull request.
    """

    return stage_script_path(entry).is_file()


def stage_accepts_pipeline_position(entry: dict[str, Any]) -> bool:
    """Report whether a stage's installed helper takes the pipeline's position.

    Only a stage that scopes a budget to one pass of the pipeline accepts these
    arguments, and a stage that does not would fail on an unrecognized one. The
    answer is read from the helper actually installed rather than assumed from a
    version, so a pipeline running against an older stage simply omits them and
    that stage keeps its own budget.
    """

    path = stage_script_path(entry)
    try:
        return PIPELINE_RUN_FLAG in path.read_text(encoding="utf-8")
    except OSError:
        return False


def status_path_for(state_path: Path) -> Path:
    return state_path.parent / f"{state_path.name}.status.json"


def write_result_file(path: Path, payload: dict[str, Any], label: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except OSError as error:
        raise WorkflowError(
            f"could not write the {label} result file: {error}"
        ) from error


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise WorkflowError(f"state file does not exist: {path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise WorkflowError(f"state file holds invalid JSON: {path}: {error}") from error
    if not isinstance(state, dict):
        raise WorkflowError(f"state file does not hold an object: {path}")
    if state.get("version") != STATE_VERSION:
        raise WorkflowError(f"unsupported state version in {path}")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = utc_now()
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def resolve_repo_root() -> Path:
    """The worktree the pipeline itself is running in.

    The process working directory is the only source. No command takes a repo
    path, because a path supplied per invocation lets two commands in one run
    name two different trees, and then a guard reads one tree while the stages
    write another.
    """

    cwd = Path.cwd()
    output = run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"]).stdout.strip()
    return Path(output).resolve()


def recorded_repo_root(state: dict[str, Any]) -> Path:
    """The one worktree this run established, read back from the state.

    ``preflight`` resolves it once and records it. Every later command reads it
    from here, so the tree a guard inspects is the tree the stages run in.
    """

    value = state.get("repo_root")
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(
            "the pipeline state records no repo_root; run preflight in the "
            "worktree the pipeline runs in"
        )
    return Path(value)


def github_repo_from_remote(url: str) -> str | None:
    patterns = (
        re.compile(
            r"^(?:https?|git|ssh)://(?:[^@/\s]+@)?github\.com(?::\d+)?/"
            r"(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?/?$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:[^@/\s]+@)?github\.com:(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?$",
            re.IGNORECASE,
        ),
    )
    for pattern in patterns:
        match = pattern.match(url)
        if match:
            return match.group("repo")
    return None


def configured_upstream(repo_root: Path, branch: str) -> dict[str, str] | None:
    remote = run(
        ["git", "-C", str(repo_root), "config", "--get", f"branch.{branch}.remote"],
        check=False,
    )
    merge = run(
        ["git", "-C", str(repo_root), "config", "--get", f"branch.{branch}.merge"],
        check=False,
    )
    if remote.returncode != 0 and merge.returncode != 0:
        return None
    if remote.returncode != 0 or merge.returncode != 0:
        raise WorkflowError(
            f"current branch {branch!r} has incomplete upstream configuration"
        )

    remote_name = remote.stdout.strip()
    merge_ref = merge.stdout.strip()
    if not remote_name or remote_name == ".":
        raise WorkflowError(
            f"current branch {branch!r} does not track a GitHub remote branch"
        )
    prefix = "refs/heads/"
    if not merge_ref.startswith(prefix) or merge_ref == prefix:
        raise WorkflowError(
            f"current branch {branch!r} has unsupported upstream merge ref {merge_ref!r}"
        )

    remote_url = run(
        ["git", "-C", str(repo_root), "remote", "get-url", remote_name]
    ).stdout.strip()
    remote_repo = github_repo_from_remote(remote_url)
    if remote_repo is None:
        raise WorkflowError(
            f"upstream remote {remote_name!r} is not a supported GitHub URL: {remote_url}"
        )
    return {
        "remote": remote_name,
        "repo": remote_repo,
        "branch": merge_ref[len(prefix) :],
    }


def pr_target_from_payload(
    payload: Any, expected_upstream: dict[str, str] | None = None
) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("url"), str):
        raise WorkflowError("gh pr view did not return a pull request URL")
    if payload.get("state") != "OPEN":
        return None
    if expected_upstream is not None:
        owner = payload.get("headRepositoryOwner")
        repository = payload.get("headRepository")
        head_repo = (
            f"{owner.get('login')}/{repository.get('name')}"
            if isinstance(owner, dict)
            and isinstance(owner.get("login"), str)
            and isinstance(repository, dict)
            and isinstance(repository.get("name"), str)
            else None
        )
        if (
            head_repo is None
            or head_repo.lower() != expected_upstream["repo"].lower()
            or payload.get("headRefName") != expected_upstream["branch"]
        ):
            return None
    return parse_target(payload["url"])


def simple_current_pr_target(
    repo_root: Path, expected_upstream: dict[str, str] | None
) -> dict[str, Any] | None:
    fields = "url,state,headRefName,headRepositoryOwner,headRepository"
    process = run(["gh", "pr", "view", "--json", fields], cwd=repo_root, check=False)
    if process.returncode != 0:
        return None
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise WorkflowError(f"gh pr view returned invalid JSON: {error}") from error
    return pr_target_from_payload(payload, expected_upstream)


def exact_upstream_pr_targets(upstream: dict[str, str]) -> list[dict[str, Any]]:
    query = """
query($owner:String!,$repo:String!,$refName:String!,$after:String){
  repository(owner:$owner,name:$repo){
    ref(qualifiedName:$refName){
      target{
        ... on Commit{
          associatedPullRequests(first:100,after:$after){
            pageInfo{hasNextPage endCursor}
            nodes{
              url state headRefName headRepository{nameWithOwner}
            }
          }
        }
      }
    }
  }
}
"""
    owner, repo = upstream["repo"].split("/", 1)
    after: str | None = None
    targets: dict[str, dict[str, Any]] = {}
    while True:
        payload = graphql(
            query,
            {
                "owner": owner,
                "repo": repo,
                "refName": f"refs/heads/{upstream['branch']}",
                "after": after,
            },
        )
        repository = payload["data"].get("repository") or {}
        ref = repository.get("ref") or {}
        commit = ref.get("target") or {}
        connection = commit.get("associatedPullRequests")
        if connection is None:
            return []
        for node in connection["nodes"]:
            head_repository = node.get("headRepository") or {}
            if (
                node.get("state") == "OPEN"
                and node.get("headRefName") == upstream["branch"]
                and head_repository.get("nameWithOwner", "").lower()
                == upstream["repo"].lower()
            ):
                target = parse_target(node["url"])
                targets[target["pr_url"]] = target
        if not connection["pageInfo"]["hasNextPage"]:
            return list(targets.values())
        after = connection["pageInfo"]["endCursor"]


def current_pr_target(repo_root: Path) -> dict[str, Any]:
    branch = git(repo_root, "branch", "--show-current")
    if not branch:
        raise WorkflowError(
            "cannot resolve the current pull request from detached HEAD, which "
            "names no branch to look up; pass the pull request explicitly"
        )
    upstream = configured_upstream(repo_root, branch)

    if upstream is None or branch == upstream["branch"]:
        target = simple_current_pr_target(repo_root, upstream)
        if upstream is None and target is not None:
            return target
    if upstream is None:
        raise WorkflowError(
            f"no pull request found for current branch {branch!r}, "
            "which has no configured upstream"
        )

    targets = exact_upstream_pr_targets(upstream)
    if not targets:
        raise WorkflowError(
            "no open pull request found for upstream "
            f"{upstream['repo']}:{upstream['branch']}"
        )
    if len(targets) > 1:
        urls = ", ".join(sorted(target["pr_url"] for target in targets))
        raise WorkflowError(
            "multiple open pull requests found for upstream "
            f"{upstream['repo']}:{upstream['branch']}: {urls}"
        )
    return targets[0]


def repo_name_for(repo_root: Path) -> str | None:
    process = run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"], check=False
    )
    if process.returncode != 0:
        return None
    return github_repo_from_remote(process.stdout.strip())


def resolve_target(value: str | None, repo_root: Path) -> dict[str, Any]:
    if not value:
        return current_pr_target(repo_root)
    return parse_target(value, repo_name_for(repo_root))


def corroborate_mergeability(mergeable: Any) -> dict[str, Any]:
    """Judge whether GitHub's mergeability answer has settled.

    GitHub computes mergeability in the background, and the result lags the head
    the pull request already reports. One response can therefore carry a fresh
    ``headRefOid`` beside a settled ``MERGEABLE`` or ``CONFLICTING`` computed
    against the head it replaced. ``UNKNOWN`` covers only the interval while
    GitHub recomputes, so waiting for ``UNKNOWN`` to clear does not rule that
    out, and nothing here can. The only thing that narrows the window is the
    caller refusing the first read after the head moved.

    ``mergeStateStatus`` is deliberately not consulted. It was once required to
    agree with ``mergeable`` before a mergeable answer could be trusted, on the
    theory that a self-contradicting response is one to throw away. Measurement
    killed that theory: across 81 open draft pull requests the two fields agreed
    every time, with ``CONFLICTING`` always paired with ``DIRTY`` and ``UNKNOWN``
    always paired with ``UNKNOWN``. They are two views of one asynchronous
    computation and they go stale together, so requiring agreement cannot catch
    the stale answer the guard existed to catch. A check that can never fire is
    worse than no check, because the next reader counts it as a defense that has
    been holding all along. The field is still recorded; it decides nothing.

    So the residual window is open, and this says so rather than implying
    otherwise. No GitHub field states which commit a mergeability answer was
    computed at, so no caller can prove that a response *about* a head carries
    an answer computed *at* it.
    """

    mergeable_value = str(mergeable or "").strip().upper()
    fields = {"mergeable": mergeable_value or None}

    if mergeable_value == "CONFLICTING":
        return {**fields, "state": "conflicting", "settled": True, "reason": "settled"}

    if mergeable_value == "MERGEABLE":
        return {**fields, "state": "mergeable", "settled": True, "reason": "settled"}

    return {
        **fields,
        "state": "unsettled",
        "settled": False,
        "reason": (
            "mergeable_unknown"
            if mergeable_value in ("", "UNKNOWN")
            else "unrecognized"
        ),
    }


def observe_pull_request(
    target: dict[str, Any], *, known_head_sha: str | None = None
) -> dict[str, Any]:
    """Read every live GitHub fact the stage decisions depend on.

    Two of these facts are not simply true when GitHub returns them, and this
    reads more than once for both.

    Mergeability is computed in the background and lags the head the pull
    request already reports, so a response can carry a fresh ``headRefOid``
    beside an answer computed against the head it replaced. A response taken
    right after the head moved is therefore refused and asked again after a
    delay. That is the whole of the defense: the two mergeability fields GitHub
    returns are two views of one computation and go stale together, so no
    agreement between them can stand in for freshness.

    The check rollup is not stale in that way. Each check run belongs to a
    commit, so the rollup genuinely describes this head. It can still be
    *incomplete*: right after a push, GitHub may have registered only the
    quickest workflows, and a rollup with two passing entries and nothing else
    yet looks exactly like a finished green one. The first read after a push is
    refused for that reason too, and coverage is judged separately against the
    contexts the base branch declares as required.

    This narrows both windows; it closes neither. No GitHub field states which
    commit a mergeability answer was computed at, so no caller can prove that a
    response *about* a head holds an answer computed *at* it, and no field
    states how many checks a commit will eventually run. What the pipeline
    promises is only the safe direction: an answer it cannot corroborate leaves
    the stage not green, which costs one stage run that reads a real answer and
    stops.
    """

    fields = (
        "number,title,url,state,isDraft,mergeable,mergeStateStatus,headRefName,"
        "headRefOid,headRepositoryOwner,headRepository,baseRefName,"
        "statusCheckRollup"
    )
    payload: dict[str, Any] = {}
    mergeability: dict[str, Any] = {}
    previous_head = known_head_sha
    head_moved = False
    moved = False
    attempts = 0
    for delay in (*MERGEABLE_RETRY_DELAYS, None):
        raw = gh_json(
            [
                "pr",
                "view",
                target["pr_url"],
                "--repo",
                target["repo_name"],
                "--json",
                fields,
            ]
        )
        if not isinstance(raw, dict):
            raise WorkflowError("gh pr view did not return PR metadata")
        payload = raw
        attempts += 1
        observed_head = payload.get("headRefOid")
        moved = bool(previous_head) and observed_head != previous_head
        head_moved = head_moved or moved
        previous_head = observed_head
        mergeability = corroborate_mergeability(payload.get("mergeable"))
        if delay is None:
            break
        if moved:
            # The head changed under the pipeline. This response's mergeability
            # may predate the commit it arrived with, and its rollup may hold
            # only the checks that registered first. Ask again rather than
            # accept either on the first read after a push.
            time.sleep(delay)
            continue
        if mergeability["settled"]:
            break
        time.sleep(delay)

    head_sha = payload.get("headRefOid")
    if not isinstance(head_sha, str) or not head_sha:
        raise WorkflowError("resolved PR metadata has no head commit")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise WorkflowError("resolved PR metadata has no title")
    head_owner = payload.get("headRepositoryOwner")
    head_repository = payload.get("headRepository")
    if (
        not isinstance(head_owner, dict)
        or not isinstance(head_owner.get("login"), str)
        or not isinstance(head_repository, dict)
        or not isinstance(head_repository.get("name"), str)
    ):
        raise WorkflowError(
            "pull request head repository is unavailable; it may have been deleted"
        )
    base_branch = payload.get("baseRefName")
    if not isinstance(base_branch, str) or not base_branch:
        raise WorkflowError("resolved PR metadata has no base branch")
    base_sha = base_ref_tip(target["repo_name"], base_branch)
    return {
        "pr": {
            "number": target["number"],
            "title": title.strip(),
            "pr_url": target["pr_url"],
            "repo_name": target["repo_name"],
            "owner": target["owner"],
            "repo": target["repo"],
            "head_owner": head_owner["login"],
            "head_repo": head_repository["name"],
            "head_branch": payload.get("headRefName"),
            "base_branch": base_branch,
            "is_draft": bool(payload.get("isDraft")),
        },
        "state": payload.get("state"),
        "head_sha": head_sha,
        "base_sha": base_sha,
        "mergeable": payload.get("mergeable"),
        "merge_state_status": payload.get("mergeStateStatus"),
        "mergeability": mergeability,
        "checks": summarize_checks(payload.get("statusCheckRollup")),
        "reads": {
            "attempts": attempts,
            "head_moved": head_moved,
            "head_moved_on_last_read": moved,
        },
    }


def check_conclusion(node: Any) -> str:
    """Reduce one status check rollup node to a single upper-case state."""

    if not isinstance(node, dict):
        return "UNKNOWN"
    status = str(node.get("status") or "").upper()
    conclusion = str(node.get("conclusion") or "").upper()
    state = str(node.get("state") or "").upper()
    if status and status != "COMPLETED":
        return status
    if conclusion:
        return conclusion
    if state:
        return state
    if status:
        return status
    return "UNKNOWN"


def summarize_checks(rollup: Any) -> dict[str, Any]:
    """Turn the rollup into counts plus one overall state.

    An empty rollup is reported as ``none`` rather than as success. A repository
    with no applicable checks must never look like a passing pipeline.

    The names are kept because a rollup can be complete in shape and incomplete
    in coverage. Right after a push GitHub may have registered only the quickest
    workflows, and a rollup holding two passing entries with nothing failing and
    nothing pending is indistinguishable here from a finished green one. Judging
    that needs to know which checks the branch *declares*, which comes from the
    repository's rulesets rather than from this response.
    ``judge_check_coverage`` applies it.
    """

    nodes = rollup if isinstance(rollup, list) else []
    counts: dict[str, int] = {}
    failing: list[dict[str, str]] = []
    pending: list[dict[str, str]] = []
    names: set[str] = set()
    for node in nodes:
        conclusion = check_conclusion(node)
        counts[conclusion] = counts.get(conclusion, 0) + 1
        name = ""
        if isinstance(node, dict):
            name = str(node.get("name") or node.get("context") or "")
        if name:
            names.add(name)
        if conclusion in CHECK_FAILURE_STATES:
            failing.append({"name": name, "state": conclusion})
        elif conclusion not in CHECK_SUCCESS_STATES:
            pending.append({"name": name, "state": conclusion})

    if not nodes:
        overall = "none"
    elif failing:
        overall = "failing"
    elif pending:
        overall = "pending"
    else:
        overall = "success"
    return {
        "state": overall,
        "total": len(nodes),
        "counts": counts,
        "failing": failing,
        "pending": pending,
        "names": sorted(names),
        "coverage": {
            "state": "unsatisfied",
            "source": "none",
            "reason": "not_judged",
            "missing": [],
            "declared": [],
        },
        "action_required": [
            entry for entry in failing if entry["state"] == "ACTION_REQUIRED"
        ],
    }


def required_contexts(target: dict[str, Any], base_branch: Any) -> dict[str, Any]:
    """Read which status checks the base branch *declares* as required.

    Absence of a check name only means "has not arrived yet" for a check the
    repository said would run. An inferred expectation cannot carry that
    meaning: neither the base commit's checks nor the pull request's previous
    head tell you what this head is supposed to produce, so a name missing from
    either is indistinguishable from a name that was never coming.

    The ruleset endpoint is the one that answers the declared question. It reads
    with plain read access, and it returns the active rules from every ruleset
    that applies to the branch, so the required contexts are the union across
    every ``required_status_checks`` rule rather than the first one found.

    The classic branch-protection endpoint is deliberately not used. A
    repository governed by rulesets rather than by classic protection answers it
    with ``404``, which is indistinguishable from an unprotected branch, so it
    fails quietly and wrongly on exactly the repositories this pipeline runs
    against most.

    Nothing declared is a normal answer rather than a fault. A private
    repository on a free plan answers ``403``, a branch with no rules answers
    ``404`` or an empty list, and a branch with rules may declare no required
    checks at all. Every one of those leaves the answer unavailable, and
    coverage falls back to waiting for the head to settle.
    """

    if not isinstance(base_branch, str) or not base_branch:
        return {"available": False, "reason": "no_base_branch", "contexts": []}
    try:
        response = gh_json(
            [
                "api",
                f"repos/{target['repo_name']}/rules/branches/{base_branch}",
            ]
        )
    except WorkflowError as error:
        detail = str(error)
        reason = "not_available_here" if "403" in detail else "lookup_failed"
        if "404" in detail:
            reason = "no_rules"
        return {
            "available": False,
            "reason": reason,
            "detail": detail,
            "contexts": [],
        }

    contexts: set[str] = set()
    for rule in response if isinstance(response, list) else []:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        parameters = rule.get("parameters")
        if not isinstance(parameters, dict):
            continue
        for check in parameters.get("required_status_checks") or []:
            if not isinstance(check, dict):
                continue
            context = check.get("context")
            if isinstance(context, str) and context:
                contexts.add(context)
    if not contexts:
        return {"available": False, "reason": "none_declared", "contexts": []}
    return {"available": True, "reason": "declared", "contexts": sorted(contexts)}


def judge_check_coverage(
    names: set[str],
    required: Any,
    *,
    head_age_seconds: float | None,
    grace_seconds: int = CHECK_SETTLE_GRACE_SECONDS,
    deadline_seconds: int = CHECK_COVERAGE_DEADLINE_SECONDS,
) -> dict[str, Any]:
    """Judge whether the head's rollup is complete enough to be believed.

    A rollup can be complete in shape and incomplete in coverage. Right after a
    push GitHub may have registered only the quickest workflows, and a rollup
    holding two passing entries with nothing failing and nothing pending looks
    exactly like a finished green one.

    Where the base branch declares required contexts, coverage is answered
    exactly: every declared context must appear in the rollup. A declared
    context that is missing has not registered yet, and its absence is
    meaningful because the repository said it would be there.

    Where nothing is declared, coverage degrades to a question about time: the
    head must have been under observation for ``grace_seconds`` before a passing
    rollup is believed. Time always passes, so this fallback cannot hold a stage
    forever.

    Comparison against an inferred set is not used in either path, and the
    absence of a check nobody declared never holds the pipeline. A check that is
    *present* still counts wherever it came from, so a failing check outside the
    declared set routes to the check stage as it always did.

    The declared path is bounded too. A repository that skips its checks on
    draft pull requests would otherwise wait for a context that is never coming,
    and this pipeline works exclusively on drafts. After
    ``deadline_seconds`` the missing contexts stop being a wait and become an
    escalation that names them.
    """

    declared: set[str] = set()
    if isinstance(required, dict) and required.get("available"):
        declared = {name for name in required.get("contexts") or [] if name}

    if declared:
        missing = sorted(declared - names)
        if not missing:
            return {
                "state": "satisfied",
                "source": "declared",
                "reason": "required_contexts_present",
                "missing": [],
                "declared": sorted(declared),
            }
        age = None if head_age_seconds is None else float(head_age_seconds)
        if age is not None and age >= deadline_seconds:
            return {
                "state": "overdue",
                "source": "declared",
                "reason": "required_contexts_never_registered",
                "missing": missing,
                "declared": sorted(declared),
                "head_age_seconds": age,
                "deadline_seconds": deadline_seconds,
            }
        return {
            "state": "unsatisfied",
            "source": "declared",
            "reason": "required_contexts_missing",
            "missing": missing,
            "declared": sorted(declared),
            "head_age_seconds": age,
            "deadline_seconds": deadline_seconds,
        }

    reason = "none_declared"
    if isinstance(required, dict) and required.get("reason"):
        reason = str(required["reason"])
    if head_age_seconds is None:
        return {
            "state": "satisfied",
            "source": "age",
            "reason": "age_not_measurable",
            "missing": [],
            "declared": [],
            "required_reason": reason,
        }
    age = float(head_age_seconds)
    state = "satisfied" if age >= grace_seconds else "unsatisfied"
    return {
        "state": state,
        "source": "age",
        "reason": "head_settled" if state == "satisfied" else "head_too_new",
        "missing": [],
        "declared": [],
        "required_reason": reason,
        "head_age_seconds": age,
        "grace_seconds": grace_seconds,
    }


def apply_check_coverage(
    state: dict[str, Any], observation: dict[str, Any], required: Any
) -> dict[str, Any]:
    """Judge the rollup's coverage and fold the answer into the observation.

    ``checks_watch`` records when this head first came under observation. That
    timestamp is what the fallback grace and the declared deadline are both
    measured from, so a head that has only just arrived is never mistaken for
    one whose checks have finished registering.
    """

    head_sha = observation["head_sha"]
    checks = observation.setdefault("checks", {})
    names = {name for name in checks.get("names") or [] if name}
    now = utc_now()

    watch = state.get("checks_watch")
    if not isinstance(watch, dict) or watch.get("head_sha") != head_sha:
        watch = {"head_sha": head_sha, "first_seen_at": now}
    state["checks_watch"] = watch

    coverage = judge_check_coverage(
        names,
        required,
        head_age_seconds=elapsed_seconds(watch.get("first_seen_at"), now),
    )
    checks["coverage"] = coverage
    if checks.get("state") == "success" and coverage["state"] != "satisfied":
        checks["state"] = "pending"
    return coverage


def cached_required_contexts(
    state: dict[str, Any], target: dict[str, Any], base_branch: Any
) -> dict[str, Any]:
    """Read the declared contexts once per base branch and reuse the answer.

    Which checks a branch requires is configuration. It does not change while a
    pipeline runs, so it must not become a call per poll.
    """

    cached = state.get("required_contexts")
    if isinstance(cached, dict) and cached.get("base_branch") == base_branch:
        return cached
    answer = required_contexts(target, base_branch)
    answer = {**answer, "base_branch": base_branch, "read_at": utc_now()}
    state["required_contexts"] = answer
    return answer


def run_stage_status(
    entry: dict[str, Any], target: dict[str, Any]
) -> dict[str, Any]:
    """Run one stage helper's ``status`` and return its envelope.

    Every stage has a helper, whatever kind of evidence makes the stage green.
    Locating and running that helper is therefore separate from reading
    greenness: the conflict and check stages answer to GitHub for greenness and
    still have a helper that can say how their own run ended.
    """

    script = stage_script_path(entry)
    state_path = stage_state_path(entry["plugin"], target)
    result: dict[str, Any] = {
        "installed": script.is_file(),
        "script": str(script),
        "state": str(state_path),
        "payload": None,
    }
    if not result["installed"]:
        return {**result, "ok": False, "reason": "helper_missing"}
    if not state_path.is_file():
        return {**result, "ok": False, "reason": "no_state"}
    try:
        process = run(
            [sys.executable, str(script), "status", "--state", str(state_path)],
            check=False,
            timeout=30,
        )
    except WorkflowError as error:
        return {
            **result,
            "ok": False,
            "reason": "status_timeout",
            "detail": str(error),
        }
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "no output"
        return {**result, "ok": False, "reason": "status_failed", "detail": detail}
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        return {
            **result,
            "ok": False,
            "reason": "invalid_status_json",
            "detail": str(error),
        }
    return {**result, "ok": True, "payload": payload}


def read_stage_marker(entry: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """Read one stage's clean-at-head record from its own helper.

    The pipeline never reads a stage's prose report. A stage whose result is a
    judgment leaves the only durable record of that judgment in its own state
    file, so the helper that owns the file is the only thing that may interpret
    it.

    Every marker carries ``installed``, including the markers of the stages whose
    greenness GitHub already states. Those stages need no helper lookup, but they
    still need their plugin present before the pipeline may launch them.
    """

    script = stage_script_path(entry)
    installed = script.is_file()

    if entry["evidence"] != "helper":
        return {
            "source": "github",
            "available": True,
            "installed": installed,
            "script": str(script),
            "clean_at_head_sha": None,
        }

    status = run_stage_status(entry, target)
    if not status.get("ok") and status.get("reason") == "no_state":
        return {
            "source": "helper",
            "available": True,
            "installed": True,
            "reason": "no_state",
            "state": status["state"],
            "clean_at_head_sha": None,
        }
    if not status.get("ok"):
        return {
            "source": "helper",
            "available": False,
            "installed": status["installed"],
            "reason": status["reason"],
            "script": status["script"],
            "state": status["state"],
            "detail": status.get("detail"),
            "clean_at_head_sha": None,
        }
    payload = status["payload"]
    return {
        "source": "helper",
        "available": True,
        "installed": True,
        "state": status["state"],
        "clean_at_head_sha": extract_clean_at_head_sha(entry["stage"], payload),
        "status_result": payload.get("result") if isinstance(payload, dict) else None,
    }


def extract_clean_at_head_sha(stage: str, payload: Any) -> str | None:
    """Pull the clean-at-head SHA out of one stage helper's status envelope.

    Each stage names the field differently because each one was built on its own.
    The pipeline keeps that translation in one place so a stage's own wording
    never leaks into a decision.
    """

    if not isinstance(payload, dict) or payload.get("result") != "ready":
        return None
    if stage == STAGE_SELF_REVIEW:
        review = payload.get("review")
        if not isinstance(review, dict):
            return None
        if review.get("outcome") != "clean":
            return None
        return sha_or_none(review.get("clean_at_head_sha"))
    if stage == STAGE_COPILOT_REVIEW:
        direct = sha_or_none(payload.get("clean_at_head_sha"))
        if direct:
            return direct
        for key in ("queue", "monitoring", "review"):
            section = payload.get(key)
            if isinstance(section, dict):
                nested = sha_or_none(section.get("clean_at_head_sha"))
                if nested:
                    return nested
        return None
    if stage == STAGE_DESCRIPTION:
        return sha_or_none(payload.get("validated_head_sha"))
    return None


def extract_stage_outcome(payload: Any) -> str | None:
    """Pull a stage's own name for how its run ended out of its status envelope.

    A stage that reports ``stage_outcome`` speaks the pipeline's vocabulary
    directly, which removes the last place a model's reading of prose decided
    anything. The field counts only on a ready status: an envelope that says a
    stage has no state cannot describe a run, and a stage that cleaned up after
    clearing must not be read as having done nothing.

    This says how a run ended. It never says whether a stage is green. Greenness
    stays where it was: live GitHub for the conflict and check stages, the
    clean-at-head marker for the other three.
    """

    if not isinstance(payload, dict) or payload.get("result") != "ready":
        return None
    outcome = payload.get("stage_outcome")
    if isinstance(outcome, str) and outcome.strip() in STAGE_OUTCOMES:
        return outcome.strip()
    return None


def read_stage_outcome(
    entry: dict[str, Any], target: dict[str, Any]
) -> dict[str, Any]:
    """Ask one stage's helper how its run ended.

    Only some stages report this. A stage that does not is not a failure: the
    caller falls back to reading the stage's report, which is what the pipeline
    did for every stage before any of them could answer mechanically.
    """

    status = run_stage_status(entry, target)
    common = {
        "stage": entry["stage"],
        "installed": status["installed"],
        "script": status["script"],
        "state": status["state"],
        "evidence": entry["evidence"],
        "clean_at_head_sha": (
            extract_clean_at_head_sha(entry["stage"], status["payload"])
            if status.get("ok")
            else None
        ),
    }
    if not status.get("ok"):
        return {
            **common,
            "available": False,
            "outcome": None,
            "reason": status["reason"],
            "detail": status.get("detail"),
        }
    outcome = extract_stage_outcome(status["payload"])
    if outcome is None:
        return {
            **common,
            "available": False,
            "outcome": None,
            "reason": "not_reported",
            "status_result": (
                status["payload"].get("result")
                if isinstance(status["payload"], dict)
                else None
            ),
        }
    return {**common, "available": True, "outcome": outcome, "source": "stage_status"}


def sha_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def stage_green(
    entry: dict[str, Any],
    *,
    head_sha: str,
    cleared: dict[str, Any],
    marker: dict[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any]:
    """Decide whether one stage is green at the current head.

    A stage whose truth lives on GitHub can be green without ever running,
    because GitHub already states the fact the stage exists to establish. GitHub
    is also the only thing that may retract it, so a recorded clearance never
    speaks for such a stage: checks that pass and then fail again at the same
    head must show through.

    A stage whose truth is a judgment can only be green when its own helper
    recorded that judgment at this exact head. The pipeline's own record stands
    in for the helper there, so a stage stays green after it cleans up its state.
    """

    recorded = sha_or_none((cleared or {}).get(entry["stage"]))

    if entry["evidence"] == "helper":
        if recorded == head_sha:
            return {
                "green": True,
                "evidence": "recorded",
                "clean_at_head_sha": recorded,
            }
        if not marker.get("available"):
            return {
                "green": False,
                "evidence": "helper_unavailable",
                "reason": marker.get("reason"),
                "detail": marker.get("detail"),
            }
        clean = sha_or_none(marker.get("clean_at_head_sha"))
        if clean == head_sha:
            return {"green": True, "evidence": "helper", "clean_at_head_sha": clean}
        return {"green": False, "evidence": "helper", "clean_at_head_sha": clean}

    fresh = observation.get("reads") or {}
    stale_read = bool(fresh.get("head_moved_on_last_read"))

    if entry["stage"] == STAGE_CONFLICT:
        mergeability = observation.get("mergeability")
        if not isinstance(mergeability, dict):
            mergeability = {
                "state": "unsettled",
                "settled": False,
                "reason": "not_observed",
                "mergeable": observation.get("mergeable"),
            }
        green = mergeability.get("settled") and mergeability.get("state") == "mergeable"
        reason = mergeability.get("reason")
        if green and stale_read:
            green = False
            reason = "head_moved"
        return {
            "green": bool(green),
            "evidence": "github",
            "mergeable": mergeability.get("mergeable"),
            # Recorded for the history, and read by nothing. It is a second view
            # of the same computation ``mergeable`` comes from, so it goes stale
            # in step with it and can corroborate nothing.
            "merge_state_status": observation.get("merge_state_status"),
            "mergeability": mergeability.get("state"),
            "settled": bool(mergeability.get("settled")),
            "reason": None if green else reason,
            "recorded_at_head_sha": recorded,
        }

    if entry["stage"] == STAGE_CI:
        checks = observation.get("checks") or {}
        coverage = checks.get("coverage") or {}
        green = checks.get("state") == "success" and coverage.get("state") == "satisfied"
        reason = None if green else checks.get("state")
        if not green and coverage.get("state") != "satisfied":
            reason = coverage.get("reason") or "coverage_unsatisfied"
        if green and stale_read:
            green = False
            reason = "head_moved"
        return {
            "green": bool(green),
            "evidence": "github",
            "checks": checks.get("state"),
            "coverage": coverage.get("state"),
            "coverage_source": coverage.get("source"),
            "missing_contexts": coverage.get("missing") or [],
            "reason": reason,
            "recorded_at_head_sha": recorded,
        }

    return {"green": False, "evidence": "unknown"}


def projected_iteration(state: dict[str, Any], stage: str) -> dict[str, Any]:
    """Work out which pipeline iteration running ``stage`` next would belong to.

    An iteration is one pass down the stage order. A stage at or after the
    furthest stage this pass already started belongs to the same pass, whatever
    pushed in between. A stage before it is only ever chosen once the rest of the
    pass is green, so it means the pass finished and the pipeline is going round
    again for a clearance that went stale.
    """

    iteration = int(state.get("iteration") or 1)
    high_water = state.get("stage_high_water")
    index = STAGE_INDEX[stage]
    if not isinstance(high_water, int):
        return {"iteration": iteration, "loop_back": False, "high_water": index}
    if index >= high_water:
        return {
            "iteration": iteration,
            "loop_back": False,
            "high_water": max(index, high_water),
        }
    return {"iteration": iteration + 1, "loop_back": True, "high_water": index}


def no_progress_streak(state: dict[str, Any], stage: str) -> int:
    streaks = state.get("no_progress") or {}
    entry = streaks.get(stage)
    if isinstance(entry, dict):
        return int(entry.get("count") or 0)
    return 0


def decide_next(state: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    """Choose what the pipeline does next. This is the whole control flow.

    The result is one of ``escalate``, ``complete``, ``incomplete``, or
    ``run_stage``. Nothing here reads a stage's prose, and nothing here looks at
    the base branch: base movement deliberately triggers no re-review and no
    fresh check wait.
    """

    head_sha = observation["head_sha"]
    max_iterations = int(state.get("max_iterations") or DEFAULT_MAX_ITERATIONS)

    escalation = state.get("escalation")
    if isinstance(escalation, dict) and escalation:
        return {
            "result": "escalate",
            "stage": escalation.get("stage"),
            "reason": escalation.get("reason"),
            "detail": escalation.get("detail"),
            "next_action": escalation.get("next_action"),
            "head_sha": head_sha,
            "recorded": True,
        }

    if observation.get("state") not in (None, "OPEN"):
        return {
            "result": "escalate",
            "stage": None,
            "reason": "pr_not_open",
            "detail": (
                f"the pull request is {observation.get('state')}, so the pipeline "
                "has nothing to drive"
            ),
            "next_action": ESCALATION_ACTIONS["pr_not_open"],
            "head_sha": head_sha,
            "recorded": False,
        }

    cleared = state.get("cleared") or {}
    markers = observation.get("stage_markers") or {}
    stage_states: dict[str, Any] = {}
    for entry in STAGES:
        stage_states[entry["stage"]] = stage_green(
            entry,
            head_sha=head_sha,
            cleared=cleared,
            marker=markers.get(entry["stage"]) or {},
            observation=observation,
        )

    # A pass flows forward and only loops at its end. The floor is the furthest
    # stage this pass has started, so a commit pushed by a later stage never drags
    # the pipeline backwards mid-pass; it goes on to the next stage that still
    # needs running. Only once every stage from the floor onward is green does the
    # pass look behind it, and a stage whose clearance went stale there begins a
    # new pass, which is the one move that costs an outer iteration.
    #
    # Charging the backward hop instead made `max_iterations` mean "one backward
    # jump, ever": a push by any stage stole a whole pass, so the per-stage
    # budgets could never be spent.
    high_water = state.get("stage_high_water")
    floor = high_water if isinstance(high_water, int) else 0
    next_entry = next(
        (
            entry
            for entry in STAGES[floor:]
            if not stage_states[entry["stage"]]["green"]
        ),
        None,
    ) or next(
        (
            entry
            for entry in STAGES[:floor]
            if not stage_states[entry["stage"]]["green"]
        ),
        None,
    )

    if next_entry is None:
        return {
            "result": "complete",
            "head_sha": head_sha,
            "iteration": int(state.get("iteration") or 1),
            "max_iterations": max_iterations,
            "stage_states": stage_states,
        }

    stage = next_entry["stage"]
    verdict = stage_states[stage]
    marker = markers.get(stage) or {}

    # Installation is checked for every stage, whatever its evidence kind. A
    # stage green from GitHub never gets this far, so the check costs nothing
    # when the plugin is absent but unneeded.
    if marker.get("installed") is False:
        return {
            "result": "escalate",
            "stage": stage,
            "reason": "helper_missing",
            "detail": (
                f"the {next_entry['plugin']} plugin is not installed, so the "
                f"pipeline cannot launch {next_entry['agent']}"
            ),
            "next_action": ESCALATION_ACTIONS["helper_missing"],
            "head_sha": head_sha,
            "recorded": False,
        }

    # A declared context that never registers is not a wait, it is a fault. The
    # pipeline works exclusively on drafts, so a repository that skips its
    # checks on a draft would otherwise wait for something that is not coming.
    if verdict.get("coverage") == "overdue":
        missing = verdict.get("missing_contexts") or []
        have = "they have" if len(missing) > 1 else "it has"
        return {
            "result": "escalate",
            "stage": stage,
            "reason": "checks_never_registered",
            "detail": (
                f"the base branch requires {', '.join(missing)} but {have} "
                f"not registered on {head_sha}"
            ),
            "next_action": ESCALATION_ACTIONS["checks_never_registered"],
            "missing_contexts": missing,
            "head_sha": head_sha,
            "recorded": False,
        }

    if verdict.get("evidence") == "helper_unavailable":
        reason = verdict.get("reason") or "helper_missing"
        detail = (
            f"the {stage} helper could not report its state: "
            f"{verdict.get('detail') or reason}"
        )
        return {
            "result": "escalate",
            "stage": stage,
            "reason": "helper_missing",
            "detail": detail,
            "next_action": ESCALATION_ACTIONS["helper_missing"],
            "head_sha": head_sha,
            "recorded": False,
        }

    streak = no_progress_streak(state, stage)
    if streak >= NO_PROGRESS_LIMIT:
        return {
            "result": "escalate",
            "stage": stage,
            "reason": "no_progress",
            "detail": (
                f"{stage} ran {streak} times in a row without changing anything"
            ),
            "next_action": ESCALATION_ACTIONS["no_progress"],
            "head_sha": head_sha,
            "recorded": False,
        }

    projection = projected_iteration(state, stage)
    if projection["iteration"] > max_iterations:
        carried = state.get("carried") or {}
        uncleared: list[dict[str, Any]] = []
        for entry in STAGES:
            name = entry["stage"]
            if stage_states[name]["green"]:
                continue
            record = carried.get(name)
            if record:
                uncleared.append(
                    {
                        "stage": name,
                        "reason": record.get("reason"),
                        "head_sha": record.get("head_sha"),
                        "carried": True,
                    }
                )
            else:
                uncleared.append(
                    {
                        "stage": name,
                        "reason": "never_cleared",
                        "head_sha": head_sha,
                        "carried": False,
                    }
                )
        named = ", ".join(
            f"{item['stage']} ({item['reason']}, last at "
            f"{item['head_sha'] or 'an unknown head'})"
            for item in uncleared
        )
        return {
            "result": "incomplete",
            "reason": "stages_never_cleared",
            "detail": (
                f"the pipeline spent its {max_iterations} iterations and these "
                f"stages never cleared: {named}"
            ),
            "head_sha": head_sha,
            "iteration": int(state.get("iteration") or 1),
            "max_iterations": max_iterations,
            "uncleared": uncleared,
            "stage_states": stage_states,
        }

    return {
        "result": "run_stage",
        "stage": stage,
        "stage_index": STAGE_INDEX[stage],
        "summary": next_entry["summary"],
        "head_sha": head_sha,
        "iteration": projection["iteration"],
        "loop_back": projection["loop_back"],
        "max_iterations": max_iterations,
        "stage_states": stage_states,
    }


def stage_default_model(entry: dict[str, Any]) -> str:
    model = entry.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return DEFAULT_STAGE_MODEL


def default_stage_models() -> dict[str, str]:
    return {entry["stage"]: stage_default_model(entry) for entry in STAGES}


def stage_models(state: dict[str, Any]) -> dict[str, str]:
    configured = state.get("stage_models")
    models = default_stage_models()
    if isinstance(configured, dict):
        for stage, model in configured.items():
            if stage in models and isinstance(model, str) and model.strip():
                models[stage] = model.strip()
    return models


def model_family(model: str) -> str:
    name = (model or "").strip().lower()
    if name.startswith("claude"):
        return CLAUDE_FAMILY
    if name.startswith("gpt") or name.startswith("o1") or name.startswith("o3"):
        return "gpt"
    if name.startswith("gemini"):
        return "gemini"
    if name.startswith("grok"):
        return "grok"
    return "other"


def gate_stage_models(models: dict[str, str], *, can_pin: bool) -> dict[str, Any]:
    """Check every stage against the model family it requires.

    ``self-review-loop`` runs a fixed GPT evaluator and refuses to grade its own
    findings, so it has to run on a Claude model. No frontmatter key sets a
    model, so the launcher pins one. When the launcher cannot pin a model the
    stage inherits the pipeline's, and that gate can fail.
    """

    stages = []
    blocked = []
    for entry in STAGES:
        model = models[entry["stage"]]
        required = entry["requires_family"]
        satisfied = required is None or model_family(model) == required
        stages.append(
            {
                "stage": entry["stage"],
                "agent": entry["agent"],
                "model": model,
                "requires_family": required,
                "satisfied": satisfied,
                "pinned": can_pin,
            }
        )
        if not satisfied:
            blocked.append(entry["stage"])
    return {
        "result": "blocked" if blocked else "ready",
        "stages": stages,
        "blocked": blocked,
        "can_pin": can_pin,
    }


def pipeline_run_id(state: dict[str, Any]) -> str | None:
    """Return the token that identifies this pipeline run to a stage.

    A stage that resets a budget when the pipeline advances needs to tell one
    run from the next, because the iteration number alone cannot: a fresh run on
    the same pull request starts counting at one again, so a stage comparing
    iteration numbers across runs would see the count go backwards and would
    never reset again. Pairing the number with a per-run token makes a new run
    distinguishable from a replayed iteration.

    A state file written before this token existed falls back to its creation
    time, which is written once and never changes, so it identifies the run just
    as well. The token is opaque: nothing may parse it.
    """

    run_id = state.get("run_id")
    if isinstance(run_id, str) and run_id:
        return run_id
    created_at = state.get("created_at")
    if isinstance(created_at, str) and created_at:
        return created_at
    return None


def pipeline_position_arguments(
    entry: dict[str, Any],
    *,
    run_id: str | None,
    iteration: int,
    max_iterations: int,
) -> list[str]:
    """Build the arguments that tell a stage where the pipeline has got to.

    Both halves of the position go together or neither does. A run with no
    iteration says nothing about whether the pipeline advanced, and an iteration
    with no run cannot be told apart from the same number in a different run, so
    a stage receiving one alone would have to guess.
    """

    if not run_id or not stage_accepts_pipeline_position(entry):
        return []
    return [
        PIPELINE_RUN_FLAG,
        run_id,
        PIPELINE_ITERATION_FLAG,
        str(iteration),
        PIPELINE_MAX_ITERATIONS_FLAG,
        str(max_iterations),
    ]


def position_line(arguments: list[str]) -> str:
    """Render the pipeline's position as the keyed line a stage looks for.

    A stage triggers on a line of keyed values rather than on the flags, so the
    flags alone would leave its instruction waiting for something that never
    arrives and its budget quietly unscoped.
    """

    pairs = zip(arguments[::2], arguments[1::2])
    return " ".join(f"{flag.lstrip('-')}: {value}" for flag, value in pairs)


def launch_prompt(target: str, arguments: list[str]) -> str:
    """Build the prompt that launches one stage.

    The pipeline's position reaches a stage through the prompt because that is
    the only channel both launch paths share: a child session gets no
    environment of its own.

    It goes in both spellings a stage may key on: the keyed line one watches
    for, and the flags exactly as they must be typed. Sending only the spelling
    a particular stage does not read would drop the position silently, leaving
    that stage on its own budget while every report still says the stage ran.
    """

    if not arguments:
        return target
    return (
        f"{target}\n\n"
        f"{position_line(arguments)}\n\n"
        "Add these arguments to your preflight command, exactly as written, "
        f"and change nothing else about how you run: {' '.join(arguments)}"
    )


def launch_plan(
    state: dict[str, Any], stage: str, *, effort: str = DEFAULT_EFFORT
) -> dict[str, Any]:
    """Build the exact launch instructions for one stage.

    The plugin-qualified agent reference is built here rather than typed by the
    model. A bare basename silently resolves to the default agent and reports no
    error, so the reference is never left to a judgment call.

    The plan also carries the pipeline's own position, which a stage needs to
    scope a budget to one pass of the pipeline rather than to the pull request.
    The iteration is projected rather than read from the state, because a plan
    is built before ``start`` advances the count, so the stored number is still
    the previous pass's.
    """

    entry = STAGE_BY_NAME[stage]
    model = stage_models(state)[stage]
    pr = state["pr"]
    target = f"{pr['repo_name']}#{pr['number']}"
    projection = projected_iteration(state, stage)
    max_iterations = int(state.get("max_iterations") or DEFAULT_MAX_ITERATIONS)
    run_id = pipeline_run_id(state)
    arguments = pipeline_position_arguments(
        entry,
        run_id=run_id,
        iteration=projection["iteration"],
        max_iterations=max_iterations,
    )
    prompt = launch_prompt(target, arguments)
    log_path = stage_log_path(
        {"owner": pr["owner"], "repo": pr["repo"], "number": pr["number"]},
        stage,
        projection["iteration"],
    )
    return {
        "stage": stage,
        "plugin": entry["plugin"],
        "agent": entry["agent"],
        "model": model,
        "effort": effort,
        "target": target,
        "prompt": prompt,
        "pipeline_run": run_id,
        "pipeline_iteration": projection["iteration"],
        "pipeline_max_iterations": max_iterations,
        "pipeline_arguments": arguments,
        "session_name": f"PR Pipeline {stage}: {pr['number']} - {pr['title']}",
        "log_path": str(log_path),
        "command": [
            "copilot",
            "-p",
            prompt,
            "--agent",
            entry["agent"],
            "--model",
            model,
            "--effort",
            effort,
            *STAGE_AUTOPILOT_FLAGS,
            *STAGE_PERMISSION_FLAGS,
        ],
    }


def new_state(
    target: dict[str, Any],
    observation: dict[str, Any],
    repo_root: Path,
    max_iterations: int,
) -> dict[str, Any]:
    run_id = uuid.uuid4().hex[:12]
    created_at = utc_now()
    return {
        "version": STATE_VERSION,
        "created_at": created_at,
        "run_id": run_id,
        "runs": [
            {
                "run_id": run_id,
                "previous_run_id": None,
                "at": created_at,
                "abandoned_stage": None,
            }
        ],
        "repo_root": str(repo_root),
        "pr": observation["pr"],
        "max_iterations": max_iterations,
        "iteration": 1,
        "stage_high_water": None,
        "stage_models": default_stage_models(),
        "cleared": {},
        "carried": {},
        "no_progress": {},
        "running": None,
        "history": [],
        "escalation": None,
        "completed": None,
        "observed_head_sha": observation.get("head_sha"),
    }


def begin_run(state: dict[str, Any]) -> dict[str, Any]:
    """Start a new pipeline run over a state file that already exists.

    ``preflight`` is the only way into the pipeline, and the loop never returns
    to it, so reaching here means someone outside the loop asked for a run. That
    is what makes this reset safe: the loop cannot cause it.

    Everything that bounds or stops a single run is cleared, because carrying it
    forward would mean the previous run's ending decides this one's. A stored
    escalation is read before anything live, so a pull request that escalated
    once would replay that escalation for ever, and the only escape would be
    deleting the state file along with the history of why it escalated. The
    iteration count, the stage high-water mark, the carried set, and the
    no-progress streaks are the same shape: all of them bound one unattended
    pass through the stages, and none of them is a budget for the pull request's
    whole life.

    The streaks have to go for a sharper reason than that. A streak is what
    produces a no-progress escalation, and ``decide_next`` escalates on a streak
    at the limit before it launches anything. So a streak that outlived its run
    would rebuild the stored escalation on the first look, with no run in
    between: clearing the escalation without clearing the streak leaves the pull
    request exactly as stuck, by a longer route.

    The clearances survive, because each one names the commit it was recorded
    at and stops counting by itself when the head moves. The history survives,
    because it is the report.

    Clearing at the start rather than at the end is deliberate. A run that dies
    never gets to tidy up after itself, and that is exactly the run that leaves
    state behind, so the tidying has to happen where a dead run cannot skip it.
    """

    unterminated = state.get("unterminated_process")
    if isinstance(unterminated, dict):
        try:
            process_id = int(str(unterminated.get("process_id")))
            process_created = float(unterminated.get("process_create_time"))
        except (TypeError, ValueError):
            raise WorkflowError(
                "the previous run could not confirm that its inactive process tree "
                "terminated; inspect it before starting another run"
            )
        activity = unterminated.get("activity")
        if stage_process_tree_alive(
            process_id,
            process_created,
            activity if isinstance(activity, dict) else None,
        ):
            raise WorkflowError(
                "the previous run's inactive stage process tree may still be alive; "
                "do not start another run in this worktree"
            )
        state.pop("unterminated_process", None)

    abandoned = state.get("running")
    if not isinstance(abandoned, dict) or not abandoned:
        abandoned = None
    previous_run = pipeline_run_id(state)
    if abandoned is not None:
        # A stage recorded as running when a new run begins never reported an
        # ending. Saying so in the history keeps the report honest about a run
        # that stopped rather than finished.
        state.setdefault("history", []).append(
            {
                "stage": abandoned.get("stage"),
                "outcome": "abandoned",
                "run_id": state.get("run_id"),
                "outcome_source": "pipeline",
                "outcome_reason": "run_restarted",
                "iteration": abandoned.get("iteration"),
                "started_head_sha": abandoned.get("head_sha"),
                "head_sha": abandoned.get("head_sha"),
                "started_at": abandoned.get("started_at"),
                "ended_at": utc_now(),
                "session_id": abandoned.get("session_id"),
                "process_id": abandoned.get("process_id"),
                "launch": abandoned.get("launch"),
                "model": abandoned.get("model"),
                "detail": (
                    f"{abandoned.get('stage')} was still recorded as running "
                    "when a new pipeline run began, so it never reported how "
                    "it ended"
                ),
                "repeat": False,
            }
        )
    state["run_id"] = uuid.uuid4().hex[:12]
    state["iteration"] = 1
    state["stage_high_water"] = None
    state["escalation"] = None
    state["completed"] = None
    state["carried"] = {}
    state["no_progress"] = {}
    state["running"] = None
    started = {
        "run_id": state["run_id"],
        "previous_run_id": previous_run,
        "at": utc_now(),
        "abandoned_stage": abandoned.get("stage") if abandoned else None,
    }
    state.setdefault("runs", []).append(started)
    return started


def collect_observation(
    target: dict[str, Any],
    *,
    with_markers: bool = True,
    known_head_sha: str | None = None,
) -> dict[str, Any]:
    observation = observe_pull_request(target, known_head_sha=known_head_sha)
    markers: dict[str, Any] = {}
    if with_markers:
        for entry in STAGES:
            markers[entry["stage"]] = read_stage_marker(entry, target)
    observation["stage_markers"] = markers
    return observation


def sync_cleared(state: dict[str, Any], decision: dict[str, Any]) -> None:
    """Record every stage the decision found green at the current head.

    Live GitHub evidence clears a stage without running it. A merge-clean pull
    request needs no conflict run, and a green check rollup needs no check run.
    """

    head_sha = decision.get("head_sha")
    stage_states = decision.get("stage_states") or {}
    cleared = state.setdefault("cleared", {})
    for stage, verdict in stage_states.items():
        if verdict.get("green") and head_sha:
            cleared[stage] = head_sha


def record_escalation(
    state: dict[str, Any], decision: dict[str, Any]
) -> dict[str, Any]:
    escalation = {
        "stage": decision.get("stage"),
        "reason": decision.get("reason"),
        "detail": decision.get("detail"),
        "next_action": decision.get("next_action"),
        "head_sha": decision.get("head_sha"),
        "at": utc_now(),
    }
    state["escalation"] = escalation
    state["running"] = None
    return escalation


def command_preflight(args: argparse.Namespace) -> None:
    require_tools()
    repo_root = resolve_repo_root()
    target = resolve_target(args.target, repo_root)
    path = cli_path(args.state) if args.state else default_state_path(target)
    max_iterations = max(1, int(args.max_iterations))

    existing = load_state(path) if path.is_file() else None
    observation = collect_observation(
        target,
        known_head_sha=(existing or {}).get("observed_head_sha"),
    )
    if observation.get("state") != "OPEN":
        raise WorkflowError(
            f"pull request {target['pr_url']} is {observation.get('state')}; "
            "the pipeline only drives an open pull request"
        )

    if existing is not None:
        state = existing
        if state["pr"]["pr_url"] != observation["pr"]["pr_url"]:
            raise WorkflowError(
                f"state file {path} belongs to {state['pr']['pr_url']}"
            )
        state["pr"] = observation["pr"]
        state["repo_root"] = str(repo_root)
        state["max_iterations"] = max_iterations
        restarted = begin_run(state)
        resumed = True
    else:
        state = new_state(target, observation, repo_root, max_iterations)
        restarted = None
        resumed = False
    state["observed_head_sha"] = observation["head_sha"]
    apply_check_coverage(
        state,
        observation,
        cached_required_contexts(state, target, observation["pr"].get("base_branch")),
    )

    for assignment in args.stage_model or []:
        stage, separator, model = assignment.partition("=")
        if not separator or stage not in STAGE_BY_NAME or not model.strip():
            raise WorkflowError(
                f"--stage-model expects <stage>=<model> for a known stage: {assignment}"
            )
        state.setdefault("stage_models", {})[stage] = model.strip()

    save_state(path, state)
    gate = gate_stage_models(stage_models(state), can_pin=not args.no_pin)
    # Reported rather than fatal. A stage that is green from GitHub never
    # launches, so a missing plugin only stops the run once that stage is the
    # one to run, and ``next`` escalates there.
    missing = [
        entry["stage"] for entry in STAGES if not stage_installed(entry)
    ]
    emit(
        {
            "result": "blocked" if gate["result"] == "blocked" else "ready",
            "resumed": resumed,
            "state": str(path),
            "repo_root": str(repo_root),
            "pr": state["pr"],
            "head_sha": observation["head_sha"],
            "is_draft": state["pr"]["is_draft"],
            "iteration": state["iteration"],
            "max_iterations": state["max_iterations"],
            "run_id": state.get("run_id"),
            "restarted": restarted,
            "run_count": len(state.get("runs") or []),
            "cleared": state.get("cleared") or {},
            "model_gate": gate,
            "stages": list(STAGE_NAMES),
            "missing_plugins": missing,
        }
    )


def probe_running_stage(state: dict[str, Any]) -> dict[str, Any] | None:
    """Say what became of the process this state records as running.

    ``wait`` is the only other liveness probe, and it belongs to the agent that
    launched the stage. When that agent stops -- because the stage died, because
    it exhausted its own cap without reporting, or because the host running it
    went away -- nothing else in the loop ever looks again, and the state goes on
    saying ``running`` under a pid that no longer exists. So the probe has to sit
    in the command every later caller passes through, rather than in the one that
    already stopped.

    The whole process tree must be gone before a recorded result is called
    finished. A stage writes its outcome before it finishes publishing and
    summarizing, so the result alone never permits another stage into the shared
    worktree.

    Identity is the pid **and** its creation time, never the pid alone. Pids are
    recycled, so a bare existence check reads whichever program later inherited
    the number as the stage, and a stall would be laundered into a green for as
    long as that program lived. Without a recorded creation time the probe
    therefore declines to answer and names the fact it is missing, because a
    probe that can be fooled is worse than none.
    """

    running = state.get("running")
    if not isinstance(running, dict) or not running:
        return None
    stage = running.get("stage")
    probe: dict[str, Any] = {
        "stage": stage,
        "pid": running.get("process_id"),
        "process_create_time": running.get("process_create_time"),
        "log_path": running.get("log_path"),
        "started_at": running.get("started_at"),
        "iteration": running.get("iteration"),
        "head_sha": running.get("head_sha"),
    }

    try:
        pid = int(str(probe["pid"]))
    except (TypeError, ValueError):
        probe["verdict"] = RUNNING_STAGE_UNVERIFIABLE
        probe["reason"] = "no_process_id_recorded"
        return probe
    if probe["process_create_time"] is None:
        probe["verdict"] = RUNNING_STAGE_UNVERIFIABLE
        probe["reason"] = "no_process_create_time_recorded"
        return probe

    create_time = float(probe["process_create_time"])
    entry = STAGE_BY_NAME.get(stage) if isinstance(stage, str) else None
    if process_exited(pid, create_time) and entry is not None:
        reading = read_stage_outcome(
            entry,
            build_target(
                state["pr"]["owner"], state["pr"]["repo"], state["pr"]["number"]
            ),
        )
        if reading.get("available"):
            probe["verdict"] = RUNNING_STAGE_FINISHED
            probe["outcome"] = reading.get("outcome")
            return probe

    if stage_process_tree_alive(
        pid,
        create_time,
        running.get("activity") if isinstance(running.get("activity"), dict) else None,
    ):
        probe["verdict"] = RUNNING_STAGE_ALIVE
        return probe

    if entry is not None:
        reading = read_stage_outcome(
            entry,
            build_target(
                state["pr"]["owner"], state["pr"]["repo"], state["pr"]["number"]
            ),
        )
        if reading.get("available"):
            probe["verdict"] = RUNNING_STAGE_FINISHED
            probe["outcome"] = reading.get("outcome")
            return probe
    probe["verdict"] = RUNNING_STAGE_ABANDONED
    return probe


def record_abandoned_stage(
    state: dict[str, Any], probe: dict[str, Any]
) -> dict[str, Any]:
    """Write down that a stage stopped without saying how, and escalate.

    Abandoned is not an outcome. Nothing here guesses what the stage would have
    reported, nothing clears a stage, and the pipeline does not advance: the
    history records that the process is gone, that the result is unknown, and
    where the log is, which is all anyone can honestly say about it.
    """

    stage = probe.get("stage")
    detail = (
        f"{stage} was recorded as running under pid {probe.get('pid')}, which is "
        "gone, and no terminal result was ever recorded, so how far it got and "
        "whether its work is sound are both unknown"
    )
    log_path = probe.get("log_path")
    if log_path:
        detail = f"{detail}; its log is {log_path}"
    state.setdefault("history", []).append(
        {
            "stage": stage,
            "outcome": "abandoned",
            "run_id": state.get("run_id"),
            "outcome_source": "pipeline",
            "outcome_reason": "process_gone",
            "iteration": probe.get("iteration"),
            "started_head_sha": probe.get("head_sha"),
            "head_sha": probe.get("head_sha"),
            "started_at": probe.get("started_at"),
            "ended_at": utc_now(),
            "process_id": probe.get("pid"),
            "log_path": log_path,
            "detail": detail,
            "repeat": False,
        }
    )
    return record_escalation(
        state,
        {
            "stage": stage,
            "reason": "stage_abandoned",
            "detail": detail,
            "next_action": ESCALATION_ACTIONS["stage_abandoned"],
            "head_sha": probe.get("head_sha"),
        },
    )


def command_next(args: argparse.Namespace) -> None:
    require_tools()
    path = cli_path(args.state)
    state = load_state(path)
    probe = probe_running_stage(state)
    if probe is not None:
        if probe["verdict"] == RUNNING_STAGE_ABANDONED:
            escalation = record_abandoned_stage(state, probe)
            save_state(path, state)
            emit(
                {
                    "result": "escalate",
                    "recorded": True,
                    "stage": probe.get("stage"),
                    "reason": escalation["reason"],
                    "detail": escalation["detail"],
                    "next_action": escalation["next_action"],
                    "head_sha": escalation["head_sha"],
                    "running": probe,
                    "state": str(path),
                }
            )
            return
        emit(
            {
                "result": "stage_running",
                "verdict": probe["verdict"],
                "stage": probe.get("stage"),
                "running": probe,
                "state": str(path),
                "detail": RUNNING_STAGE_DETAILS[probe["verdict"]],
            }
        )
        return
    target = build_target(
        state["pr"]["owner"], state["pr"]["repo"], state["pr"]["number"]
    )
    observation = collect_observation(
        target, known_head_sha=state.get("observed_head_sha")
    )
    state["observed_head_sha"] = observation["head_sha"]
    apply_check_coverage(
        state,
        observation,
        cached_required_contexts(state, target, observation["pr"].get("base_branch")),
    )
    decision = decide_next(state, observation)

    if decision["result"] == "run_stage":
        sync_cleared(state, decision)
        save_state(path, state)
        plan = launch_plan(state, decision["stage"], effort=args.effort)
        emit(
            {
                **{key: value for key, value in decision.items() if key != "stage_states"},
                "state": str(path),
                "plan": plan,
                "stage_states": decision["stage_states"],
                "checks": observation["checks"],
                "mergeable": observation["mergeable"],
                "mergeability": observation.get("mergeability"),
                "cleared": state.get("cleared") or {},
            }
        )
        return

    if decision["result"] == "complete":
        sync_cleared(state, decision)
        state["completed"] = {"at": utc_now(), "head_sha": decision["head_sha"]}
        state["running"] = None
        save_state(path, state)
        emit(
            {
                **decision,
                "state": str(path),
                "checks": observation["checks"],
                "mergeable": observation["mergeable"],
                "mergeability": observation.get("mergeability"),
                "cleared": state.get("cleared") or {},
                "reminder": (
                    "The pipeline never marks a pull request ready for review and "
                    "never touches approval. Leaving the draft is the user's call."
                ),
            }
        )
        return

    if decision["result"] == "incomplete":
        sync_cleared(state, decision)
        state["running"] = None
        save_state(path, state)
        emit(
            {
                **{
                    key: value
                    for key, value in decision.items()
                    if key != "stage_states"
                },
                "state": str(path),
                "stage_states": decision["stage_states"],
                "checks": observation["checks"],
                "mergeable": observation["mergeable"],
                "mergeability": observation.get("mergeability"),
                "cleared": state.get("cleared") or {},
                "reminder": (
                    "The pipeline reached its iteration limit with stages still "
                    "not green. It never marks a pull request ready for review."
                ),
            }
        )
        return

    if not decision.get("recorded"):
        record_escalation(state, decision)
    save_state(path, state)
    emit(
        {
            **decision,
            "state": str(path),
            "checks": observation["checks"],
            "mergeable": observation["mergeable"],
        }
    )


def launched_process_create_time(args: argparse.Namespace) -> float | None:
    """The creation time that identifies the launched process, or ``None``.

    ``launch`` reports the value, and the caller passes it straight back. When
    it does not, the time is read from the live process here, which is accurate
    because ``start`` runs immediately after the launch, while the process is
    still there.

    ``None`` means the platform would not answer. It is recorded as ``None``
    rather than dropped, so a later probe refuses to judge liveness instead of
    trusting a pid that another program may have inherited.
    """

    supplied = getattr(args, "process_create_time", None)
    if supplied is not None:
        return float(supplied)
    try:
        pid = int(str(getattr(args, "process", None)))
    except (TypeError, ValueError):
        return None
    return process_create_time(pid)


def command_start(args: argparse.Namespace) -> None:
    """Record that a stage is starting, and charge it to an iteration."""

    path = cli_path(args.state)
    state = load_state(path)
    stage = args.stage
    if stage not in STAGE_BY_NAME:
        raise WorkflowError(f"unknown stage: {stage}")
    if state.get("escalation"):
        raise WorkflowError("the pipeline already escalated; it cannot start a stage")
    running = state.get("running")
    if isinstance(running, dict) and running:
        raise WorkflowError(
            f"stage {running.get('stage')} is already recorded as running; "
            "finish it before starting another"
        )

    projection = projected_iteration(state, stage)
    max_iterations = int(state.get("max_iterations") or DEFAULT_MAX_ITERATIONS)
    if projection["iteration"] > max_iterations:
        decision = {
            "stage": stage,
            "reason": "max_iterations_reached",
            "detail": (
                f"running {stage} again would start iteration "
                f"{projection['iteration']} of a maximum of {max_iterations}"
            ),
            "next_action": ESCALATION_ACTIONS["max_iterations_reached"],
            "head_sha": args.head,
        }
        escalation = record_escalation(state, decision)
        save_state(path, state)
        emit({"result": "escalated", "state": str(path), "escalation": escalation})
        return

    state["iteration"] = projection["iteration"]
    state["stage_high_water"] = projection["high_water"]
    running_entry = {
        "stage": stage,
        "head_sha": args.head,
        "iteration": projection["iteration"],
        "launch": args.launch,
        "session_id": args.session,
        "process_id": args.process,
        "process_create_time": launched_process_create_time(args),
        "model": stage_models(state)[stage],
        "started_at": utc_now(),
    }
    if getattr(args, "log", None):
        running_entry["log_path"] = args.log
    state["running"] = running_entry
    save_state(path, state)
    emit(
        {
            "result": "started",
            "state": str(path),
            "stage": stage,
            "iteration": state["iteration"],
            "max_iterations": max_iterations,
            "loop_back": projection["loop_back"],
            "running": state["running"],
        }
    )


def ensure_clean_worktree_for_launch(
    state: dict[str, Any], repo_root: Path
) -> dict[str, Any]:
    """Guarantee a clean tree before a stage launches, by provenance.

    Four of the five stage preflights refuse any non-empty ``git status``, so a
    shared worktree has to be clean before each stage. Cleanliness is a
    precondition of launching rather than a courtesy the previous stage performs,
    because a stage that crashed never gets to clean up after itself.

    Provenance decides what may be reset, scoped to the current run. Dirt
    present before this run has launched any stage is the user's own uncommitted
    work, so the pipeline refuses rather than destroys it. This holds on a
    resumed run too: resuming mints a fresh ``run_id`` while the prior run's
    history survives, so the question is not "has a stage ever run for this pull
    request" but "has a stage run in this run". A stage that finished earlier in
    this run, or one still recorded as running, means the dirt belongs to a
    stage, so the pipeline resets it. An old history entry from before run
    stamping never matches the current run, so it reads as "not this run" and the
    user is asked rather than overruled. The reset drops uncommitted tracked
    changes with ``reset --hard HEAD`` and removes untracked files with
    ``clean -fd``; it never resets to a recorded sha, which would discard the
    stage's own commits, and it never uses ``-x``, so a gitignored ``build/``
    survives.
    """

    dirt = worktree_dirt(repo_root)
    current_run = state.get("run_id")
    a_stage_has_run = bool(state.get("running")) or (
        current_run is not None
        and any(
            isinstance(past, dict) and past.get("run_id") == current_run
            for past in state.get("history") or []
        )
    )

    if dirt and not a_stage_has_run:
        return {
            "result": "escalate",
            "reason": "dirty_worktree_before_run",
            "detail": (
                "the pipeline worktree had uncommitted changes before any stage "
                f"ran, so they are yours:\n{dirt}"
            ),
        }

    if dirt:
        git(repo_root, "reset", "--hard", "HEAD")
        git(repo_root, "clean", "-fd")
        leftover = worktree_dirt(repo_root)
        if leftover:
            return {
                "result": "escalate",
                "reason": "worktree_reset_failed",
                "detail": (
                    "the worktree was still not clean after reset --hard and "
                    f"clean -fd:\n{leftover}"
                ),
                "reset": True,
            }
        return {"result": "reset", "was_dirty": True}

    return {"result": "clean", "was_dirty": False}


def command_launch(args: argparse.Namespace) -> None:
    """Spawn one stage as a detached subprocess writing to its log.

    Python owns the process so the agent never writes shell. It opens the log,
    starts the stage detached, records the real pid and its creation time, and
    returns at once. The combined output goes to the log and never to the
    pipeline's context, because a stage can emit thousands of lines the pipeline
    decides nothing from.

    The stage runs in the worktree this run recorded, never in whatever
    directory the pipeline agent happened to invoke the helper from. That is
    what makes the guards on that worktree mean anything: the tree `reset` puts
    on the pull request head, and the tree `finish` checks for an unpushed
    commit, is provably the tree the stage wrote in.
    """

    if not args.command:
        raise WorkflowError("launch needs the stage command after --")
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise WorkflowError("launch needs the stage command after --")
    command[0] = resolve_launch_program(command[0])
    state = load_state(cli_path(args.state))
    repo_root = recorded_repo_root(state)
    log_path = cli_path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(log_path, "w", encoding="utf-8")
    try:
        popen_kwargs: dict[str, Any] = {
            "cwd": str(repo_root),
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
        }
        if IS_WINDOWS:
            # CREATE_NO_WINDOW rather than DETACHED_PROCESS. Both keep the stage
            # off this console, but DETACHED_PROCESS leaves it with no console at
            # all, and Windows then gives every console program the stage runs a
            # new visible one. CREATE_NO_WINDOW gives the stage an invisible
            # console its children inherit instead. The two are mutually
            # exclusive: CREATE_NO_WINDOW is ignored when DETACHED_PROCESS is
            # also set.
            popen_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            ) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(list(command), **popen_kwargs)
    finally:
        log_handle.close()
    emit(
        {
            "result": "launched",
            "pid": process.pid,
            "process_create_time": process_create_time(process.pid),
            "log_path": str(log_path),
            "repo_root": str(repo_root),
        }
    )


def observe_stage_activity(
    entry: dict[str, Any],
    target: dict[str, Any],
    log_path: Path | None,
    pid: int,
    create_time: float | None,
    known_processes: Iterable[str] = (),
) -> dict[str, Any]:
    helper_stamp = None
    reading = run_stage_status(entry, target)
    payload = reading.get("payload") if reading.get("ok") else None
    if isinstance(payload, dict):
        stamp = payload.get("last_helper_activity")
        helper_stamp = stamp if isinstance(stamp, str) and stamp else None

    log_size = None
    if log_path is not None:
        try:
            log_size = log_path.stat().st_size
        except FileNotFoundError:
            log_size = 0
        except OSError:
            log_size = None

    process_snapshot = process_tree_snapshot(pid, create_time) or {}
    known_snapshot = known_process_snapshot(known_processes)
    process_tree = sorted(
        {
            *(process_snapshot.get("process_tree") or []),
            *(known_snapshot.get("process_tree") or []),
        }
    )
    process_cpu = {
        **(process_snapshot.get("process_cpu") or {}),
        **(known_snapshot.get("process_cpu") or {}),
    }
    return {
        "helper_stamp": helper_stamp,
        "log_size": log_size,
        "process_tree": process_tree,
        "process_cpu": process_cpu,
        "cpu_seconds": (
            round(sum(process_cpu.values()), 6) if process_cpu else None
        ),
    }


def update_activity_tracker(
    previous: dict[str, Any],
    current: dict[str, Any],
    observed_at: str,
) -> tuple[dict[str, Any], list[str]]:
    signals: list[str] = []
    previous_helper = previous.get("helper_stamp")
    current_helper = current.get("helper_stamp")
    if current_helper is not None and current_helper != previous_helper:
        signals.append("helper_state")

    previous_log = previous.get("log_size")
    current_log = current.get("log_size")
    if (
        isinstance(current_log, int)
        and isinstance(previous_log, int)
        and current_log > previous_log
    ):
        signals.append("stage_log")

    previous_tree = previous.get("process_tree")
    current_tree = current.get("process_tree")
    if (
        isinstance(previous_tree, list)
        and isinstance(current_tree, list)
        and current_tree != previous_tree
    ):
        signals.append("process_tree")

    previous_process_cpu = previous.get("process_cpu")
    current_process_cpu = current.get("process_cpu")
    process_cpu_progress = (
        isinstance(previous_process_cpu, dict)
        and isinstance(current_process_cpu, dict)
        and any(
            isinstance(used_cpu, (int, float))
            and isinstance(previous_process_cpu.get(identity), (int, float))
            and used_cpu > previous_process_cpu[identity]
            for identity, used_cpu in current_process_cpu.items()
        )
    )
    previous_cpu = previous.get("cpu_seconds")
    current_cpu = current.get("cpu_seconds")
    aggregate_cpu_progress = (
        not isinstance(previous_process_cpu, dict)
        and isinstance(previous_cpu, (int, float))
        and isinstance(current_cpu, (int, float))
        and current_cpu > previous_cpu
    )
    if process_cpu_progress or aggregate_cpu_progress:
        signals.append("process_cpu")

    known_processes = sorted(
        {
            *(
                previous.get("known_processes")
                or previous.get("process_tree")
                or []
            ),
            *(current.get("process_tree") or []),
        }
    )
    tracker = {
        "last_activity_at": (
            observed_at if signals else previous.get("last_activity_at") or observed_at
        ),
        **current,
        "known_processes": known_processes,
    }
    if signals:
        tracker["last_signals"] = signals
    elif previous.get("last_signals"):
        tracker["last_signals"] = previous["last_signals"]
    return tracker, signals


def record_inactive_stage(
    state: dict[str, Any],
    running: dict[str, Any],
    *,
    pid: int,
    terminated_processes: list[int],
    silent_seconds: float,
    termination_error: str | None = None,
) -> dict[str, Any]:
    stage = running.get("stage")
    detail = (
        f"{stage} showed no helper-state change, log growth, process-tree change, "
        f"or root/descendant CPU progress for {int(silent_seconds)} seconds; "
        f"the pipeline terminated process tree {terminated_processes or [pid]}"
    )
    if termination_error:
        detail = (
            f"{detail}, but termination could not be confirmed: {termination_error}"
        )
    state.setdefault("history", []).append(
        {
            "stage": stage,
            "outcome": "escalated",
            "run_id": state.get("run_id"),
            "outcome_source": "pipeline",
            "outcome_reason": "stage_inactive",
            "iteration": running.get("iteration"),
            "started_head_sha": running.get("head_sha"),
            "head_sha": running.get("head_sha"),
            "started_at": running.get("started_at"),
            "ended_at": utc_now(),
            "process_id": pid,
            "log_path": running.get("log_path"),
            "detail": detail,
            "repeat": False,
        }
    )
    if termination_error:
        state["unterminated_process"] = dict(running)
    else:
        state.pop("unterminated_process", None)
    state["running"] = None
    return record_escalation(
        state,
        {
            "stage": stage,
            "reason": "stage_inactive",
            "detail": detail,
            "next_action": ESCALATION_ACTIONS["stage_inactive"],
            "head_sha": running.get("head_sha"),
        },
    )


def command_wait(args: argparse.Namespace) -> None:
    """Wait for one bounded slice, while active stages remain unbounded.

    The healthy path returns only after the process tree has **exited**. The stage
    helper writes its terminal outcome before the stage agent finishes
    summarizing and pushing, so returning the moment an outcome appears would let
    the next stage launch into the shared worktree while this one is still
    mutating it. The outcome is therefore read only after exit is observed, in an
    order that cannot misread a clean exit as a crash: observe exit, then read the
    outcome, then decide.
    """

    state = load_state(cli_path(args.state))
    if args.stage not in STAGE_BY_NAME:
        raise WorkflowError(f"unknown stage: {args.stage}")
    entry = STAGE_BY_NAME[args.stage]
    target = build_target(
        state["pr"]["owner"], state["pr"]["repo"], state["pr"]["number"]
    )
    pid = int(args.pid)
    create_time = (
        float(args.process_create_time)
        if args.process_create_time is not None
        else None
    )
    if create_time is None:
        raise WorkflowError(
            "wait requires --process-create-time so a recycled pid cannot be "
            "monitored or terminated as the stage"
        )
    wait_slice = min(
        float(args.timeout) if args.timeout is not None else STAGE_WAIT_SLICE_SECONDS,
        float(STAGE_WAIT_SLICE_SECONDS),
    )
    poll = float(args.poll) if args.poll is not None else STAGE_WAIT_POLL_SECONDS
    started = time.monotonic()
    deadline = started + max(0.0, wait_slice)
    running = state.get("running")
    running = running if isinstance(running, dict) else {}
    tracker = running.get("activity")
    if not isinstance(tracker, dict):
        tracker = {
            "last_activity_at": running.get("started_at") or utc_now(),
            "helper_stamp": None,
            "log_size": 0,
            "process_tree": None,
            "cpu_seconds": 0.0,
        }
    log_value = running.get("log_path")
    log_path = cli_path(log_value) if isinstance(log_value, str) and log_value else None

    while True:
        if time.monotonic() >= deadline:
            emit(
                {
                    "result": "still_running",
                    "stage": args.stage,
                    "pid": pid,
                    "activity_signals": [],
                    "last_activity_at": tracker.get("last_activity_at"),
                    "silent_for_seconds": elapsed_seconds(
                        tracker.get("last_activity_at"), utc_now()
                    ),
                    "next_action": "Run wait again while the stage process remains active.",
                    "waited_seconds": round(time.monotonic() - started, 3),
                }
            )
            return
        if process_exited(pid, create_time):
            reading = read_stage_outcome(entry, target)
            if reading.get("available"):
                emit(
                    {
                        "result": "finished",
                        "stage": args.stage,
                        "outcome": reading.get("outcome"),
                        "pid": pid,
                        "waited_seconds": round(time.monotonic() - started, 3),
                    }
                )
                return
        alive = stage_process_tree_alive(pid, create_time, tracker)
        if not alive:
            # Exit observed first. Only now read the outcome, so a stage that
            # wrote its outcome and exited within a poll window is not mistaken
            # for a process that died without one.
            reading = read_stage_outcome(entry, target)
            if reading.get("available"):
                emit(
                    {
                        "result": "finished",
                        "stage": args.stage,
                        "outcome": reading.get("outcome"),
                        "pid": pid,
                        "waited_seconds": round(time.monotonic() - started, 3),
                    }
                )
                return
            emit(
                {
                    "result": "carry",
                    "reason": "process_exited_without_outcome",
                    "stage": args.stage,
                    "pid": pid,
                    "detail": (
                        f"the {args.stage} process exited before its helper "
                        "recorded an outcome"
                    ),
                    "next_action": (
                        "Carry the stage: record it with finish --outcome carried "
                        "--carried-reason process_exited_without_outcome. The next "
                        "pass gives it the rest of its budget."
                    ),
                    "waited_seconds": round(time.monotonic() - started, 3),
                }
            )
            return

        observed_at = utc_now()
        observation = observe_stage_activity(
            entry,
            target,
            log_path,
            pid,
            create_time,
            tracker.get("known_processes") or tracker.get("process_tree") or [],
        )
        updated_tracker, signals = update_activity_tracker(
            tracker, observation, observed_at
        )
        if updated_tracker != tracker:
            tracker = updated_tracker
            if running:
                running["activity"] = tracker
                state["running"] = running
                save_state(cli_path(args.state), state)
        silent_seconds = elapsed_seconds(tracker.get("last_activity_at"), observed_at)
        if (
            silent_seconds is not None
            and silent_seconds >= STAGE_INACTIVITY_LIMIT_SECONDS
        ):
            termination_error = None
            try:
                terminated = terminate_process_tree(pid, create_time, tracker)
            except WorkflowError as error:
                terminated = []
                termination_error = str(error)
            if termination_error is None and terminated is None:
                continue
            escalation = record_inactive_stage(
                state,
                running,
                pid=pid,
                terminated_processes=terminated,
                silent_seconds=silent_seconds,
                termination_error=termination_error,
            )
            save_state(cli_path(args.state), state)
            emit(
                {
                    "result": "escalate",
                    "reason": "stage_inactive",
                    "stage": args.stage,
                    "pid": pid,
                    "detail": escalation["detail"],
                    "next_action": escalation["next_action"],
                    "terminated_processes": terminated,
                    "termination_error": termination_error,
                    "waited_seconds": round(time.monotonic() - started, 3),
                }
            )
            return
        if time.monotonic() - started >= wait_slice:
            emit(
                {
                    "result": "still_running",
                    "stage": args.stage,
                    "pid": pid,
                    "activity_signals": signals,
                    "last_activity_at": tracker.get("last_activity_at"),
                    "silent_for_seconds": silent_seconds,
                    "next_action": "Run wait again while the stage process remains active.",
                    "waited_seconds": round(time.monotonic() - started, 3),
                }
            )
            return
        time.sleep(min(poll, max(0.0, deadline - time.monotonic())))


def command_reset(args: argparse.Namespace) -> None:
    """Put the shared worktree on the pull request head, clean, before a launch.

    Three things can be true of the worktree the pipeline runs in, and they need
    opposite treatment, so the diagnosis comes first and the repair second.

    A local head that holds the pull request head plus commits of its own is a
    stage that committed without pushing. That work must not be discarded, so the
    pipeline escalates. A worktree on the pull request's own branch whose commits
    are not the pull request's has diverged, which is a different fault and says
    so rather than claiming the branch is merely ahead. Anything else -- another
    branch, an unrelated history, or simply an older head -- is a session that
    has not been put on the pull request yet, and putting it there is a
    precondition rather than a failure.

    The provenance gate runs between the diagnosis and the checkout, and the
    order is load bearing. Uncommitted changes that predate every stage in this
    run are the user's, so the pipeline refuses instead of checking out over
    them.
    """

    path = cli_path(args.state)
    state = load_state(path)
    repo_root = recorded_repo_root(state)
    target = build_target(
        state["pr"]["owner"], state["pr"]["repo"], state["pr"]["number"]
    )

    diagnosis = diagnose_local_head(
        repo_root, target, head_branch=state["pr"].get("head_branch")
    )
    reason = LOCAL_HEAD_ESCALATIONS.get(diagnosis["verdict"])
    if reason is not None:
        decision = {
            "stage": args.stage,
            "reason": reason,
            "detail": diagnosis["detail"],
            "next_action": ESCALATION_ACTIONS[reason],
        }
        escalation = record_escalation(state, decision)
        save_state(path, state)
        emit({"result": "escalated", "state": str(path), "escalation": escalation})
        return

    outcome = ensure_clean_worktree_for_launch(state, repo_root)
    if outcome["result"] == "escalate":
        decision = {
            "stage": args.stage,
            "reason": outcome["reason"],
            "detail": outcome["detail"],
            "next_action": ESCALATION_ACTIONS[outcome["reason"]],
        }
        escalation = record_escalation(state, decision)
        save_state(path, state)
        emit({"result": "escalated", "state": str(path), "escalation": escalation})
        return

    checked_out = False
    if diagnosis["verdict"] == LOCAL_HEAD_NEEDS_CHECKOUT:
        checkout = checkout_pr_head(repo_root, target, diagnosis["pr_head"])
        if checkout["result"] == "escalate":
            decision = {
                "stage": args.stage,
                "reason": checkout["reason"],
                "detail": checkout["detail"],
                "next_action": ESCALATION_ACTIONS[checkout["reason"]],
            }
            escalation = record_escalation(state, decision)
            save_state(path, state)
            emit({"result": "escalated", "state": str(path), "escalation": escalation})
            return
        checked_out = True

    emit(
        {
            "result": "ready",
            "state": str(path),
            "repo_root": str(repo_root),
            "local_head": diagnosis["verdict"],
            "checked_out": checked_out,
            "head_sha": diagnosis["pr_head"],
            "was_dirty": outcome.get("was_dirty", False),
            "reset": outcome["result"] == "reset",
        }
    )


def git_succeeds(repo_root: Path, *arguments: str) -> bool:
    return (
        run(["git", "-C", str(repo_root), *arguments], check=False).returncode == 0
    )


def git_or_none(repo_root: Path, *arguments: str) -> str | None:
    try:
        return git(repo_root, *arguments)
    except WorkflowError:
        return None


def classify_local_head(
    *,
    local_head: str | None,
    pr_head: str | None,
    on_pr_branch: bool,
    descends_from_pr_head: bool,
    ahead_count: int,
    unreachable_count: int,
) -> str:
    """Say how the local head stands against the pull request head.

    A count of commits beyond the pull request head cannot tell a stage's extra
    commit from an unrelated branch, because both count above zero. Ancestry
    tells them apart: a head that contains the pull request head carries the
    pull request's work and something more, and nothing else does.

    ``descends_from_pr_head`` is read before the branch, so a worktree detached
    one commit past the pull request head still reads as a stage's unpushed
    work. The branch only decides between divergence and a checkout, because
    commits on a branch that is not the pull request's are that branch's own
    business.

    Ancestry alone is not enough, though, and ``unreachable_count`` is not
    redundant with the branch arm below it. **The question that decides whether
    moving HEAD is safe is reachability: is every commit under HEAD held by some
    ref other than HEAD itself?** ``git rev-list --count HEAD --not --branches
    --remotes --tags`` answers it directly. Zero means a branch, a remote-tracking
    ref, or a tag holds this work, and checking something else out loses nothing.
    Above zero means those commits exist only because HEAD points at them, and
    ``git checkout --detach`` would leave them unreferenced.

    That is the case ancestry misses. A stage commits on a detached head, the
    pull request head then moves underneath it -- a push from elsewhere, an
    amend, a force-push -- and the pull request head stops being an ancestor.
    The head is no longer ``ahead``; it is not on the pull request's branch
    either, so it is not ``diverged``; and detached HEAD is this pipeline's
    normal operating state, so this sits on the common path rather than at an
    edge. Without the reachability arm it would fall through to a checkout and
    the commits would become garbage.

    Reachability also answers the attached case for nothing: the checked-out
    branch is in ``--branches``, so a session sitting on its own branch, or
    detached at a commit ``origin/main`` still holds, counts zero and starts
    normally. The branch arm below stays because a diverged pull request branch
    is worth naming even when its commits are safe -- silently detaching away
    from it would hide a real disagreement.
    """

    if not local_head or not pr_head:
        return LOCAL_HEAD_UNKNOWN
    if local_head == pr_head:
        return LOCAL_HEAD_AT_PR_HEAD
    if descends_from_pr_head and ahead_count > 0:
        return LOCAL_HEAD_AHEAD
    if unreachable_count > 0:
        return LOCAL_HEAD_UNREACHABLE
    if on_pr_branch and ahead_count > 0:
        return LOCAL_HEAD_DIVERGED
    return LOCAL_HEAD_NEEDS_CHECKOUT


def unreachable_commit_count(repo_root: Path) -> int:
    """Commits held by HEAD and by nothing else.

    ``--not --branches --remotes --tags`` subtracts everything any other ref can
    reach, so what remains exists only because HEAD points at it. Those are the
    commits a checkout would orphan.
    """

    counted = git_or_none(
        repo_root,
        "rev-list",
        "--count",
        "HEAD",
        "--not",
        "--branches",
        "--remotes",
        "--tags",
    )
    try:
        return int((counted or "0").strip())
    except ValueError:
        return 0


def remote_pull_request_head(repo_root: Path, target: dict[str, Any]) -> str | None:
    """The pull request head read straight off the ref, with no API in the way.

    ``gh pr view`` serves a cached ``headRefOid`` that lags a push by seconds,
    which is exactly the window a stage finishes in. ``ls-remote`` reads the ref
    itself, so it can say whether a commit the API has not caught up with is
    already published.
    """

    remote = pr_remote_name(repo_root, target)
    reference = f"refs/pull/{target['number']}/head"
    output = git_or_none(repo_root, "ls-remote", remote, reference)
    if not output:
        return None
    first = output.splitlines()[0].split()
    return first[0] if first else None


def diagnose_local_head(
    repo_root: Path, target: dict[str, Any], *, head_branch: str | None = None
) -> dict[str, Any]:
    """Read the facts one worktree presents about the pull request head.

    An ``ahead`` verdict is confirmed against the ref before it is returned. The
    API's head lags a push, and a stage pushes and then finishes at once, so the
    unconfirmed reading would halt a healthy pipeline on its normal cycle. A
    guard that stops working runs gets switched off, which costs more than the
    fault it catches.
    """

    diagnosis = read_local_head(repo_root, target, head_branch=head_branch)
    if diagnosis["verdict"] != LOCAL_HEAD_AHEAD:
        return diagnosis
    published = remote_pull_request_head(repo_root, target)
    if not published or published == diagnosis["pr_head"]:
        return diagnosis
    # The ref names a head the API had not caught up with. Re-derive from the
    # authoritative sha rather than trusting either reading on its own.
    confirmed = read_local_head(
        repo_root, target, head_branch=head_branch, pr_head=published
    )
    confirmed["pr_head_source"] = "ls-remote"
    confirmed["stale_pr_head"] = diagnosis["pr_head"]
    return confirmed


def read_local_head(
    repo_root: Path,
    target: dict[str, Any],
    *,
    head_branch: str | None = None,
    pr_head: str | None = None,
) -> dict[str, Any]:
    """One reading of the worktree against a given pull request head."""

    local_head = git_or_none(repo_root, "rev-parse", "HEAD")
    branch = git_or_none(repo_root, "branch", "--show-current") or ""
    if pr_head is None:
        pr_head = target_remote_head(target)
    ahead_count = 0
    behind_count = 0
    descends = False
    unreachable = 0
    if local_head and pr_head:
        descends = git_succeeds(
            repo_root, "merge-base", "--is-ancestor", pr_head, local_head
        )
        ahead_count = commit_count(repo_root, pr_head, local_head)
        behind_count = commit_count(repo_root, local_head, pr_head)
        unreachable = unreachable_commit_count(repo_root)
    verdict = classify_local_head(
        local_head=local_head,
        pr_head=pr_head,
        on_pr_branch=bool(branch) and branch == head_branch,
        descends_from_pr_head=descends,
        ahead_count=ahead_count,
        unreachable_count=unreachable,
    )
    where = f"branch {branch}" if branch else f"detached at {local_head}"
    details = {
        LOCAL_HEAD_UNKNOWN: (
            "the pipeline could not read both the local head and the pull "
            "request head, so it left the worktree alone"
        ),
        LOCAL_HEAD_AT_PR_HEAD: f"the worktree is on the pull request head {pr_head}",
        LOCAL_HEAD_AHEAD: (
            f"the local branch is {ahead_count} commit(s) ahead of the pull "
            f"request head {pr_head}; a stage committed without pushing"
        ),
        LOCAL_HEAD_UNREACHABLE: (
            f"the worktree is on {where}, and {unreachable} commit(s) under that "
            f"head are held by no branch, remote-tracking ref, or tag; checking "
            f"the pull request head {pr_head} out would leave them unreachable"
        ),
        LOCAL_HEAD_DIVERGED: (
            f"the worktree is on the pull request branch {branch}, but its head "
            f"{local_head} has diverged from the pull request head {pr_head}: "
            f"{ahead_count} commit(s) here are not on the pull request and "
            f"{behind_count} commit(s) on the pull request are not here"
        ),
        LOCAL_HEAD_NEEDS_CHECKOUT: (
            f"the worktree is on {where}, which is not the pull request head "
            f"{pr_head}, so the pipeline checks the pull request head out"
        ),
    }
    return {
        "verdict": verdict,
        "local_head": local_head,
        "pr_head": pr_head,
        "branch": branch,
        "head_branch": head_branch,
        "ahead_count": ahead_count,
        "behind_count": behind_count,
        "unreachable_count": unreachable,
        "pr_head_source": "api",
        "detail": details[verdict],
    }


def commit_count(repo_root: Path, start: str, end: str) -> int:
    counted = git_or_none(repo_root, "rev-list", "--count", f"{start}..{end}")
    try:
        return int((counted or "0").strip())
    except ValueError:
        return 0


def pr_remote_name(repo_root: Path, target: dict[str, Any]) -> str:
    """The remote that serves the pull request's own repository."""

    listing = git_or_none(repo_root, "remote", "-v") or ""
    wanted = str(target.get("repo_name") or "").lower()
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        named = github_repo_from_remote(fields[1])
        if named and named.lower() == wanted:
            return fields[0]
    return "origin"


def checkout_pr_head(
    repo_root: Path, target: dict[str, Any], pr_head: str | None
) -> dict[str, Any]:
    """Put the worktree on the pull request head, detached.

    The head is fetched through ``refs/pull/<number>/head`` because that ref
    exists on the pull request's own repository whatever fork the branch lives
    on. The checkout detaches on purpose: the pull request's branch is often
    already checked out in the session worktree that opened it, and git refuses
    to check the same branch out twice in one repository.
    """

    if not pr_head:
        return {
            "result": "escalate",
            "reason": "checkout_pr_head_failed",
            "detail": "the pipeline could not read the pull request head from GitHub",
        }
    remote = pr_remote_name(repo_root, target)
    reference = f"refs/pull/{target['number']}/head"
    try:
        git(repo_root, "fetch", "--quiet", remote, reference)
        git(repo_root, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    except WorkflowError as error:
        return {
            "result": "escalate",
            "reason": "checkout_pr_head_failed",
            "detail": (
                f"the pipeline could not check out {reference} from {remote}: {error}"
            ),
        }
    landed = git_or_none(repo_root, "rev-parse", "HEAD")
    if landed != pr_head:
        return {
            "result": "escalate",
            "reason": "checkout_pr_head_failed",
            "detail": (
                f"the worktree is on {landed} after checking out {reference} from "
                f"{remote}, which is not the pull request head {pr_head}"
            ),
        }
    return {"result": "checked_out", "head_sha": landed}


def target_remote_head(target: dict[str, Any]) -> str | None:
    try:
        payload = gh_json(
            [
                "pr",
                "view",
                str(target["number"]),
                "--repo",
                target["repo_name"],
                "--json",
                "headRefOid",
            ]
        )
    except WorkflowError:
        return None
    if isinstance(payload, dict):
        head = payload.get("headRefOid")
        if isinstance(head, str) and head.strip():
            return head.strip()
    return None


def resolve_finish_outcome(
    entry: dict[str, Any],
    target: dict[str, Any],
    requested: str,
    *,
    head_sha: str | None = None,
) -> dict[str, Any]:
    """Settle how a stage ended, preferring the stage's own answer.

    Both answers are evidence, and neither is a view of the run itself. The
    stage's word is read from a state file. The caller's word is the agent's
    reading of its own run. The stage's is preferred because the stage contract
    guarantees it is a record of an ending a command wrote, rather than a word
    inferred from the shape of that state, so it cannot be misread out of prose.
    The caller's answer is kept in the history either way, which is what makes a
    disagreement visible instead of silent.

    That precedence has one limit, and it is the reason a head is passed in. A
    stage whose greenness is a judgment records that judgment in a state file
    that outlives the run which wrote it, and its ``status`` reports ``cleared``
    from the presence of that record. A run that dies before it replaces an
    older record therefore answers ``cleared`` about a commit it never looked
    at. The word alone is not evidence about this run: a clearance is accepted
    only when the stage's own head-pinned marker names the head being recorded.
    When it does not, the run reached no clearance, and the disagreement is kept
    rather than quietly rewritten, because a stage answering from a record it
    did not write is worth seeing afterwards.

    A stage whose truth lives on GitHub is untouched by this. Its clearance is
    never read from the pipeline's record, so a stale marker cannot speak for
    it.

    A pipeline problem that is not the stage's fault, such as a launch that never
    produced a run, belongs in ``escalate`` rather than here. ``finish`` says how
    the stage ended.
    """

    reading = read_stage_outcome(entry, target)
    if not reading.get("available"):
        return {
            "outcome": requested,
            "requested_outcome": requested,
            "outcome_source": "reported",
            "outcome_reason": reading.get("reason"),
            "clean_at_head_sha": reading.get("clean_at_head_sha"),
        }
    outcome = reading["outcome"]
    marker = sha_or_none(reading.get("clean_at_head_sha"))
    if entry["evidence"] == "helper" and outcome == "cleared" and marker != head_sha:
        return {
            "outcome": "no_progress",
            "requested_outcome": requested,
            "outcome_source": "stage_status",
            "outcome_reason": "clean_marker_head_mismatch",
            "stage_outcome": outcome,
            "clean_at_head_sha": marker,
        }
    return {
        "outcome": outcome,
        "requested_outcome": requested,
        "outcome_source": "stage_status",
        "outcome_reason": None,
        "clean_at_head_sha": marker,
    }


def command_outcome(args: argparse.Namespace) -> None:
    """Report how the stage that just ran ended, in the pipeline's vocabulary."""

    state = load_state(cli_path(args.state))
    if args.stage not in STAGE_BY_NAME:
        raise WorkflowError(f"unknown stage: {args.stage}")
    entry = STAGE_BY_NAME[args.stage]
    target = build_target(
        state["pr"]["owner"], state["pr"]["repo"], state["pr"]["number"]
    )
    reading = read_stage_outcome(entry, target)
    payload = {
        **reading,
        "stage": args.stage,
        "outcome": reading.get("outcome"),
    }
    if reading.get("available"):
        emit({**payload, "result": "ready", "authoritative": True})
        return
    emit(
        {
            **payload,
            "result": "not_reported",
            "authoritative": False,
            "reason": reading.get("reason", "not_reported"),
            "next_action": (
                "This stage does not report its own outcome, so work it out from "
                "the stage's report and pass it to finish."
            ),
        }
    )


def command_finish(args: argparse.Namespace) -> None:
    """Record how a stage ended, and keep the durable history entry."""

    path = cli_path(args.state)
    state = load_state(path)
    stage = args.stage
    if stage not in STAGE_BY_NAME:
        raise WorkflowError(f"unknown stage: {stage}")
    running = state.get("running")
    if not isinstance(running, dict) or running.get("stage") != stage:
        raise WorkflowError(
            f"stage {stage} is not recorded as running; start it before finishing it"
        )

    head_sha = args.head or running.get("head_sha")
    # The stage log is never rolled into the pipeline's report, so this text is
    # the only human-readable account of a run that did not clear. An outcome and
    # a commit say what happened for a clearance; they say nothing about why a
    # stage stalled or gave up. Checked against the outcome the caller passed
    # rather than the one that gets recorded, because the caller cannot supply
    # detail for a reclassification that happens after the stage has already run.
    if args.outcome in DETAIL_REQUIRED_OUTCOMES and not (args.detail or "").strip():
        raise WorkflowError(
            f"--detail is required for {args.outcome}: say in one plain sentence "
            "what happened, because the stage log is not read into the report and "
            "the history is all that survives"
        )
    target = build_target(
        state["pr"]["owner"], state["pr"]["repo"], state["pr"]["number"]
    )
    # A stage that committed without pushing leaves its work only in the shared
    # worktree, which the next stage's `reset` discards. Recording an ending here
    # would seal that loss: the pipeline moves on, the worktree is reset, and the
    # commit becomes a dangling object. This fires wherever the commit lands, not
    # only on the specific ending the loss was first seen under, because the
    # hazard is the unpushed commit and not the word the stage chose for it.
    #
    # It reports rather than pushes. `finish` records how a stage ended; it is not
    # a publish path, and pushing here would guess a destination and a force
    # policy the pipeline never chose and could clobber a head another actor moved
    # underneath it. The reversible move is to stop and name the shas so a person,
    # or a deliberate re-run, pushes. `reset` already escalates on the same
    # condition rather than pushing.
    repo_root = recorded_repo_root(state)
    diagnosis = diagnose_local_head(
        repo_root, target, head_branch=state["pr"].get("head_branch")
    )
    reason = LOCAL_HEAD_ESCALATIONS.get(diagnosis["verdict"])
    if reason is not None:
        decision = {
            "stage": stage,
            "reason": reason,
            "detail": (
                f"refusing to record an ending for {stage}: {diagnosis['detail']}. "
                "Recording it would let the next stage reset the worktree and "
                "discard the commit."
            ),
            "next_action": ESCALATION_ACTIONS[reason],
            "head_sha": head_sha,
        }
        escalation = record_escalation(state, decision)
        save_state(path, state)
        emit(
            {
                "result": "escalated",
                "state": str(path),
                "stage": stage,
                "escalation": escalation,
            }
        )
        return
    resolution = resolve_finish_outcome(
        STAGE_BY_NAME[stage], target, args.outcome, head_sha=head_sha
    )
    outcome = resolution["outcome"]
    carried_reason = None
    if outcome == "carried":
        # A machine reading of a carried stage always means the stage spent its
        # own per-pass iteration cap. A reported carry names its own reason,
        # because a process that died before recording an outcome is carried the
        # same way without the stage ever getting to speak.
        if resolution["outcome_source"] == "stage_status":
            carried_reason = "max_iterations_reached"
        else:
            carried_reason = getattr(args, "carried_reason", None)
            if carried_reason not in CARRIED_REASONS:
                raise WorkflowError(
                    "--carried-reason is required for a reported carried outcome, "
                    f"one of {', '.join(CARRIED_REASONS)}: name why the stage was "
                    "set aside for the next pass"
                )
    detail = recorded_detail(args.detail, resolution)
    entry_detail = detail
    entry = {
        "stage": stage,
        "outcome": outcome,
        "carried_reason": carried_reason,
        "run_id": state.get("run_id"),
        "requested_outcome": resolution["requested_outcome"],
        "outcome_source": resolution["outcome_source"],
        "outcome_reason": resolution.get("outcome_reason"),
        "stage_outcome": resolution.get("stage_outcome"),
        "clean_at_head_sha": resolution.get("clean_at_head_sha"),
        "iteration": running.get("iteration"),
        "started_head_sha": running.get("head_sha"),
        "head_sha": head_sha,
        "started_at": running.get("started_at"),
        "ended_at": utc_now(),
        "session_id": args.session or running.get("session_id"),
        "process_id": args.process or running.get("process_id"),
        "log_path": running.get("log_path"),
        "launch": running.get("launch"),
        "model": running.get("model"),
        "detail": entry_detail,
    }
    # A stage repeating an answer it already gave at this head has told the
    # pipeline nothing new. Relaunching a stage that has run out of its own road
    # returns the same result immediately every time, so a repeat must not read
    # as fresh evidence and must not reset the no-progress streak that is the
    # only brake on relaunching the same stage forever.
    repeat = any(
        isinstance(past, dict)
        and past.get("stage") == stage
        and past.get("head_sha") == head_sha
        and past.get("outcome") == outcome
        for past in state.get("history") or []
    )
    entry["repeat"] = repeat
    state.setdefault("history", []).append(entry)
    state["running"] = None

    streaks = state.setdefault("no_progress", {})
    confirmation = confirm_clearance(
        STAGE_BY_NAME[stage], target, outcome, head_sha
    )
    entry["clearance_confirmed"] = confirmation.get("green")
    entry["clearance_reason"] = confirmation.get("reason")
    unconfirmed = outcome in CLEARING_OUTCOMES and confirmation.get("green") is not True
    effect = streak_effect(outcome, repeat=repeat, confirmed=confirmation.get("green"))
    stalled = effect == "charge"
    if effect == "charge":
        previous = streaks.get(stage)
        count = int(previous.get("count") or 0) + 1 if isinstance(previous, dict) else 1
        streaks[stage] = {"count": count, "head_sha": head_sha, "at": utc_now()}
    elif effect == "reset":
        streaks.pop(stage, None)

    escalation = None
    carried_map = state.setdefault("carried", {})
    if outcome in CLEARING_OUTCOMES and head_sha:
        state.setdefault("cleared", {})[stage] = head_sha
    if outcome == "carried":
        # Setting the floor past the carried stage takes it out of the running
        # for the rest of this pass. The end-of-pass look-behind finds it again
        # and starts a new pass, which is the move that spends an outer iteration
        # and hands the stage the rest of its absolute budget.
        carried_map[stage] = {
            "reason": carried_reason,
            "head_sha": head_sha,
            "iteration": running.get("iteration"),
            "at": utc_now(),
        }
        floor = state.get("stage_high_water")
        floor = floor if isinstance(floor, int) else 0
        state["stage_high_water"] = max(floor, STAGE_INDEX[stage] + 1)
    elif outcome in CLEARING_OUTCOMES:
        carried_map.pop(stage, None)
    if outcome == "escalated":
        escalation = record_escalation(
            state,
            {
                "stage": stage,
                "reason": "stage_escalated",
                "detail": entry_detail
                or f"{stage} stopped without clearing and asked for a person",
                "next_action": ESCALATION_ACTIONS["stage_escalated"],
                "head_sha": head_sha,
            },
        )
    elif stalled:
        count = int((streaks.get(stage) or {}).get("count") or 0)
        if count >= NO_PROGRESS_LIMIT:
            detail = f"{stage} ran {count} times in a row without changing anything"
            if repeat:
                detail = (
                    f"{stage} repeated its {outcome} answer at {head_sha} without "
                    "the pipeline being able to act on it"
                )
            elif unconfirmed:
                because = confirmation.get("reason") or "GitHub did not agree"
                detail = (
                    f"{stage} reported {outcome} {count} times in a row at a head "
                    f"the pipeline could not confirm it at ({because}). The stage "
                    "may well have done the work; the pipeline cannot see it, so "
                    "it will not keep relaunching on the stage's word alone"
                )
            escalation = record_escalation(
                state,
                {
                    "stage": stage,
                    "reason": "no_progress",
                    "detail": detail,
                    "next_action": ESCALATION_ACTIONS["no_progress"],
                    "head_sha": head_sha,
                },
            )

    save_state(path, state)
    emit(
        {
            "result": "escalated" if escalation else "recorded",
            "state": str(path),
            "stage": stage,
            "outcome": outcome,
            "requested_outcome": resolution["requested_outcome"],
            "outcome_source": resolution["outcome_source"],
            "outcome_reason": resolution.get("outcome_reason"),
            "entry": entry,
            "cleared": state.get("cleared") or {},
            "no_progress": state.get("no_progress") or {},
            "clearance_confirmed": confirmation.get("green"),
            "escalation": escalation,
        }
    )


def confirm_clearance(
    entry: dict[str, Any],
    target: dict[str, Any],
    outcome: str,
    head_sha: str | None,
) -> dict[str, Any]:
    """Ask GitHub whether a stage's clearing outcome is one the pipeline can see.

    A stage that clears on GitHub evidence establishes a fact GitHub states, and
    GitHub computes some of those facts asynchronously. So a stage can push a
    real merge, report that it cleared, and have GitHub still answer ``UNKNOWN``
    when the pipeline looks. The stage is not lying and the pipeline is not
    wrong; they are describing the same commit at different moments.

    The clearance is checked against live evidence alone, never against the
    pipeline's own ``cleared`` map. A record the pipeline wrote cannot be what
    confirms the record the pipeline is about to write.
    """

    if outcome not in CLEARING_OUTCOMES or not head_sha:
        return {"checked": False, "green": None, "reason": None}
    try:
        observation = collect_observation(target)
    except Exception as error:  # noqa: BLE001 - any failure is an unread answer
        return {"checked": False, "green": None, "reason": f"unread: {error}"}
    if observation.get("head_sha") != head_sha:
        return {"checked": True, "green": False, "reason": "head_moved"}
    verdict = stage_green(
        entry,
        head_sha=head_sha,
        cleared={},
        marker=(observation.get("stage_markers") or {}).get(entry["stage"]) or {},
        observation=observation,
    )
    return {
        "checked": True,
        "green": bool(verdict.get("green")),
        "reason": verdict.get("reason"),
        "evidence": verdict.get("evidence"),
    }


def streak_effect(outcome: str, *, repeat: bool, confirmed: bool | None) -> str:
    """Say what one finished run does to a stage's no-progress streak.

    The streak is the brake on relaunching a stage for ever, so what may reset it
    decides whether anything is bounded. Only a run that told the pipeline
    something it can act on resets it.

    A clearing outcome the pipeline cannot see is not something it can act on. It
    leaves the stage exactly where it was: still not green, still the next stage
    to pick, and now with a new commit that makes the run look different from the
    last one. Reset the streak there and the stage can be relaunched for ever, a
    push at a time, with nothing counting. So an unconfirmed clearance feeds the
    same streak a stalled run feeds, and two in a row stop the pipeline.

    Only a clearance GitHub confirmed resets the streak. ``confirmed`` is ``None``
    when the pipeline could not get an answer, which is not the same as a
    disagreement but is treated the same way, because an answer that was never
    read is not one the pipeline can act on either.

    A carried run holds the streak where it is. Being set aside for the next pass
    is neither progress the pipeline saw nor a stall, so it neither resets the
    streak nor charges it. The effect is one of ``charge``, ``reset``, or
    ``hold``.
    """

    if outcome == "carried":
        return "hold"
    if outcome == "no_progress" or repeat:
        return "charge"
    if outcome in CLEARING_OUTCOMES and confirmed is not True:
        return "charge"
    return "reset"


def recorded_detail(
    supplied: str | None, resolution: dict[str, Any]
) -> str | None:
    """Return the sentence the history keeps about how a stage ended.

    ``finish`` refuses ``no_progress`` and ``escalated`` without a sentence, so
    the caller has already supplied one whenever it asked for either. The gap is
    the other direction: a caller asks for ``cleared``, which needs no sentence,
    and the stage's own record disagrees, so what gets written down is an
    outcome that does need one and has none.

    Refusing there would be worse than useless. The stage has already run, and
    the caller cannot go back and observe a reason it did not have. What it can
    do is state the disagreement, which is the whole of what happened, so that
    is what gets written when nothing else was offered.
    """

    text = (supplied or "").strip()
    if text:
        return text
    outcome = resolution.get("outcome")
    if outcome not in DETAIL_REQUIRED_OUTCOMES:
        return supplied
    requested = resolution.get("requested_outcome")
    reason = resolution.get("outcome_reason") or "the stage disagreed"
    return (
        f"recorded as {outcome} rather than the {requested} the pipeline "
        f"reported, because {reason}"
    )


def command_escalate(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    if args.stage is not None and args.stage not in STAGE_BY_NAME:
        raise WorkflowError(f"unknown stage: {args.stage}")
    escalation = record_escalation(
        state,
        {
            "stage": args.stage,
            "reason": args.reason,
            "detail": args.detail,
            "next_action": args.next_action
            or ESCALATION_ACTIONS.get(args.reason)
            or "Read the tail of the kept stage log and decide what to do next.",
            "head_sha": args.head,
        },
    )
    save_state(path, state)
    emit({"result": "escalated", "state": str(path), "escalation": escalation})


def command_models(args: argparse.Namespace) -> None:
    if args.state:
        state = load_state(cli_path(args.state))
        models = stage_models(state)
    else:
        models = default_stage_models()
    gate = gate_stage_models(models, can_pin=not args.no_pin)
    payload = {**gate, "pipeline_model": args.pipeline_model}
    if args.pipeline_model:
        payload["pipeline_model_family"] = model_family(args.pipeline_model)
    if gate["result"] == "blocked":
        payload["next_action"] = ESCALATION_ACTIONS["model_gate"]
    emit(payload)


def command_plan(args: argparse.Namespace) -> None:
    state = load_state(cli_path(args.state))
    if args.stage not in STAGE_BY_NAME:
        raise WorkflowError(f"unknown stage: {args.stage}")
    entry = STAGE_BY_NAME[args.stage]
    if not stage_installed(entry):
        emit(
            {
                "result": "not_installed",
                "stage": args.stage,
                "plugin": entry["plugin"],
                "agent": entry["agent"],
                "script": str(stage_script_path(entry)),
                "detail": (
                    f"the {entry['plugin']} plugin is not installed, so the "
                    f"pipeline cannot launch {entry['agent']}"
                ),
                "next_action": ESCALATION_ACTIONS["helper_missing"],
            }
        )
        return
    emit({"result": "ready", **launch_plan(state, args.stage, effort=args.effort)})


def summarize_history(history: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in history:
        outcome = str(entry.get("outcome") or "unknown")
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


NOT_A_LIVENESS_PROBE = (
    "This is a timestamp view, not a probe. `wait` and `next` decide liveness "
    "from the recorded pid and its creation time; `status` does not, and reports "
    "only recorded timestamps. They separate a stage whose helper wrote something "
    "minutes ago from one that has been silent for an hour, which is the question "
    "a person asks before intervening."
)


def stage_activity(state: dict[str, Any]) -> dict[str, Any] | None:
    """How long the running stage has run, and how long its helper has been quiet.

    Only a stage recorded as running has an activity block, because only then is
    there a wait to judge. The stage's helper stamps its state on every write, so
    reading that stamp back says when the stage last did anything the pipeline
    can see.

    A stamp the helper cannot supply reads as ``None`` beside a ``reason``. Zero
    would claim the helper had just written, which is the opposite of what an
    unanswerable question means.
    """

    running = state.get("running")
    if not isinstance(running, dict) or not running:
        return None
    now = utc_now()
    stage = running.get("stage")
    activity: dict[str, Any] = {
        "stage": stage,
        "started_at": running.get("started_at"),
        "running_for_seconds": elapsed_seconds(running.get("started_at"), now),
        "last_helper_activity": None,
        "helper_silent_for_seconds": None,
        "note": NOT_A_LIVENESS_PROBE,
    }
    entry = STAGE_BY_NAME.get(stage) if isinstance(stage, str) else None
    if entry is None:
        activity["reason"] = "unknown_stage"
        return activity
    target = build_target(
        state["pr"]["owner"], state["pr"]["repo"], state["pr"]["number"]
    )
    reading = run_stage_status(entry, target)
    if not reading.get("ok"):
        activity["reason"] = reading.get("reason") or "status_unavailable"
        return activity
    payload = reading.get("payload")
    stamp = payload.get("last_helper_activity") if isinstance(payload, dict) else None
    if not isinstance(stamp, str) or not stamp:
        activity["reason"] = "not_reported"
        return activity
    activity["last_helper_activity"] = stamp
    activity["helper_silent_for_seconds"] = elapsed_seconds(stamp, now)
    return activity


def command_status(args: argparse.Namespace) -> None:
    if args.current:
        require_tools()
        repo_root = resolve_repo_root()
        target = current_pr_target(repo_root)
        path = default_state_path(target)
        if not path.is_file():
            emit(
                {
                    "result": "no_state",
                    "state": str(path),
                    "pr": {"number": target["number"], "url": target["pr_url"]},
                }
            )
            return
    else:
        path = cli_path(args.state)
    state = load_state(path)
    history = state.get("history") or []
    activity = stage_activity(state)
    payload = {
        "result": "ready",
        "state": str(path),
        "pr": state["pr"],
        "iteration": state.get("iteration"),
        "max_iterations": state.get("max_iterations"),
        "stage_high_water": state.get("stage_high_water"),
        "cleared": state.get("cleared") or {},
        "no_progress": state.get("no_progress") or {},
        "running": state.get("running"),
        "activity": activity,
        "escalation": state.get("escalation"),
        "completed": state.get("completed"),
        "stage_models": stage_models(state),
        "history": history,
    }
    status_path = status_path_for(path)
    write_result_file(status_path, payload, "status")
    emit(
        {
            "result": "ready",
            "state": str(path),
            "status_path": str(status_path),
            "pr": {
                "number": state["pr"]["number"],
                "title": state["pr"]["title"],
                "pr_url": state["pr"]["pr_url"],
                "repo_name": state["pr"]["repo_name"],
                "head_branch": state["pr"]["head_branch"],
                "base_branch": state["pr"]["base_branch"],
            },
            "iteration": state.get("iteration"),
            "max_iterations": state.get("max_iterations"),
            "cleared": state.get("cleared") or {},
            "running": state.get("running"),
            "activity": activity,
            "escalation": state.get("escalation"),
            "completed": state.get("completed"),
            "counts": {
                "history": len(history),
                "outcomes": summarize_history(history),
            },
        }
    )


def command_cleanup(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    running = state.get("running")
    if isinstance(running, dict) and running and not args.force:
        raise WorkflowError(
            f"stage {running.get('stage')} is still recorded as running; "
            "finish it or pass --force"
        )
    path.unlink()
    status_path_for(path).unlink(missing_ok=True)
    emit({"result": "cleaned_up", "state": str(path)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser(
        "preflight",
        help="resolve the pull request and open or resume the pipeline state",
    )
    preflight.add_argument(
        "target",
        nargs="?",
        help=(
            "PR URL, owner/repo#number, or a bare number when origin names the "
            "repository; omit only from a worktree attached to the PR's branch"
        ),
    )
    preflight.add_argument("--state")
    preflight.add_argument(
        "--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS
    )
    preflight.add_argument(
        "--stage-model",
        action="append",
        help="pin one stage's model as <stage>=<model>; repeatable",
    )
    preflight.add_argument(
        "--no-pin",
        action="store_true",
        help="the launcher cannot pin a model, so stages inherit the pipeline's",
    )
    preflight.set_defaults(function=command_preflight)

    next_command = subparsers.add_parser(
        "next", help="decide the next stage from live GitHub state and stage helpers"
    )
    next_command.add_argument("--state", required=True)
    next_command.add_argument("--effort", default=DEFAULT_EFFORT)
    next_command.set_defaults(function=command_next)

    start = subparsers.add_parser("start", help="record that a stage is starting")
    start.add_argument("--state", required=True)
    start.add_argument("--stage", required=True, choices=list(STAGE_NAMES))
    start.add_argument("--head", required=True)
    start.add_argument("--launch", choices=["session", "subprocess"], required=True)
    start.add_argument("--session")
    start.add_argument("--process")
    start.add_argument("--process-create-time", type=float)
    start.add_argument("--log")
    start.set_defaults(function=command_start)

    finish = subparsers.add_parser("finish", help="record how a stage ended")
    finish.add_argument("--state", required=True)
    finish.add_argument("--stage", required=True, choices=list(STAGE_NAMES))
    finish.add_argument("--outcome", required=True, choices=list(STAGE_OUTCOMES))
    finish.add_argument("--carried-reason", choices=list(CARRIED_REASONS))
    finish.add_argument("--head")
    finish.add_argument("--session")
    finish.add_argument("--process")
    finish.add_argument("--detail")
    finish.set_defaults(function=command_finish)

    escalate = subparsers.add_parser("escalate", help="stop the pipeline and say why")
    escalate.add_argument("--state", required=True)
    escalate.add_argument("--stage")
    escalate.add_argument("--reason", required=True)
    escalate.add_argument("--detail", required=True)
    escalate.add_argument("--next-action", dest="next_action")
    escalate.add_argument("--head")
    escalate.set_defaults(function=command_escalate)

    models = subparsers.add_parser(
        "models", help="report the pinned per-stage models and check their gates"
    )
    models.add_argument("--state")
    models.add_argument("--pipeline-model")
    models.add_argument("--no-pin", action="store_true")
    models.set_defaults(function=command_models)

    plan = subparsers.add_parser(
        "plan", help="print the exact launch instructions for one stage"
    )
    plan.add_argument("--state", required=True)
    plan.add_argument("--stage", required=True, choices=list(STAGE_NAMES))
    plan.add_argument("--effort", default=DEFAULT_EFFORT)
    plan.set_defaults(function=command_plan)

    outcome = subparsers.add_parser(
        "outcome", help="ask a stage's own helper how its run ended"
    )
    outcome.add_argument("--state", required=True)
    outcome.add_argument("--stage", required=True, choices=list(STAGE_NAMES))
    outcome.set_defaults(function=command_outcome)

    status = subparsers.add_parser("status", help="print the pipeline state")
    status_source = status.add_mutually_exclusive_group(required=True)
    status_source.add_argument("--state")
    status_source.add_argument("--current", action="store_true")
    status.set_defaults(function=command_status)

    cleanup = subparsers.add_parser("cleanup", help="delete the pipeline state")
    cleanup.add_argument("--state", required=True)
    cleanup.add_argument("--force", action="store_true")
    cleanup.set_defaults(function=command_cleanup)

    reset = subparsers.add_parser(
        "reset", help="ensure the shared worktree is clean before a stage launches"
    )
    reset.add_argument("--state", required=True)
    reset.add_argument("--stage", choices=list(STAGE_NAMES))
    reset.set_defaults(function=command_reset)

    launch = subparsers.add_parser(
        "launch", help="spawn a stage subprocess writing to its log"
    )
    launch.add_argument("--state", required=True)
    launch.add_argument("--log", required=True)
    launch.add_argument("command", nargs=argparse.REMAINDER)
    launch.set_defaults(function=command_launch)

    wait = subparsers.add_parser(
        "wait", help="block until a stage process exits, then report its outcome"
    )
    wait.add_argument("--state", required=True)
    wait.add_argument("--stage", required=True, choices=list(STAGE_NAMES))
    wait.add_argument("--pid", required=True, type=int)
    wait.add_argument("--process-create-time", type=float)
    wait.add_argument("--timeout", type=float)
    wait.add_argument("--poll", type=float)
    wait.set_defaults(function=command_wait)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.function(args)
        return 0
    except (WorkflowError, json.JSONDecodeError, OSError) as error:
        emit({"result": "error", "error": str(error)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
