#!/usr/bin/env python3
"""Reusable pieces shared by the single pull request and stack pipelines.

This module owns no stage policy. It carries the stage registry, model
selection, subprocess launching, marker inspection, worktree safety, and
logging that both pipeline helpers build on. Every function that calls
another overridable function accepts it as a keyword argument, so a caller
can substitute its own binding and a test can replace a single seam without
reaching inside an implementation.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import time
from typing import Any, Callable


DEFAULT_STAGE_MODEL = "gpt-5.6-sol"
DEFAULT_EFFORT = "high"
CLAUDE_FAMILY = "claude"
IS_WINDOWS = os.name == "nt"

PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
    r"/?(?:#\S*)?$"
)
SHORT_TARGET_PATTERN = re.compile(
    r"^(?P<owner>[^/\s]+)/(?P<repo>[^#/\s]+)#(?P<number>\d+)$"
)
BARE_NUMBER_PATTERN = re.compile(r"^#?(?P<number>\d+)$")
REPO_NAME_PATTERN = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)$")

STAGE_CONFLICT = "pr-conflict-resolver"
STAGE_SELF_REVIEW = "self-review-loop"
STAGE_COPILOT_REVIEW = "copilot-review-loop"
STAGE_CI = "ci-fix-loop"
STAGE_DESCRIPTION = "pr-description"

STAGES: tuple[dict[str, Any], ...] = (
    {
        "stage": STAGE_CONFLICT,
        "plugin": STAGE_CONFLICT,
        "agent": f"{STAGE_CONFLICT}:{STAGE_CONFLICT}",
        "module": "pr_conflict_resolver",
        "marker": ("mergeable_at_head_sha",),
        "base_marker": ("attempt", "base_sha"),
        "model": DEFAULT_STAGE_MODEL,
    },
    {
        "stage": STAGE_COPILOT_REVIEW,
        "plugin": STAGE_COPILOT_REVIEW,
        "agent": f"{STAGE_COPILOT_REVIEW}:{STAGE_COPILOT_REVIEW}",
        "module": "copilot_review_loop",
        "marker": ("clean_at_head_sha",),
        "model": DEFAULT_STAGE_MODEL,
    },
    {
        "stage": STAGE_SELF_REVIEW,
        "plugin": STAGE_SELF_REVIEW,
        "agent": f"{STAGE_SELF_REVIEW}:{STAGE_SELF_REVIEW}",
        "module": "self_review_loop",
        "marker": ("review", "clean_at_head_sha"),
        "model": "claude-opus-5",
        "requires_family": CLAUDE_FAMILY,
    },
    {
        "stage": STAGE_CI,
        "plugin": STAGE_CI,
        "agent": f"{STAGE_CI}:{STAGE_CI}",
        "module": "ci_fix_loop",
        "marker": ("clean_at_head_sha",),
        "model": DEFAULT_STAGE_MODEL,
    },
    {
        "stage": STAGE_DESCRIPTION,
        "plugin": STAGE_DESCRIPTION,
        "agent": f"{STAGE_DESCRIPTION}:{STAGE_DESCRIPTION}",
        "module": "pr_description",
        "marker": ("validated_head_sha",),
        "model": DEFAULT_STAGE_MODEL,
    },
)
STAGE_NAMES = tuple(entry["stage"] for entry in STAGES)
STAGE_BY_NAME = {entry["stage"]: entry for entry in STAGES}

STAGE_PERMISSION_FLAGS = ("--allow-all-tools", "--allow-all-paths")
STAGE_AUTOPILOT_FLAGS = ("--autopilot", "--max-autopilot-continues", "5")
PIPELINE_RUN_FLAG = "--pipeline-run"
PIPELINE_ITERATION_FLAG = "--pipeline-iteration"
PIPELINE_MAX_ITERATIONS_FLAG = "--pipeline-max-iterations"
CLEARING_OUTCOMES = frozenset({"cleared", "skipped"})
PROGRESS_EVENT = "pipeline_progress"
PROGRESS_UPDATE_EVENT = "pipeline_update"
PROGRESS_HEARTBEAT_INTERVAL = 300.0
PROGRESS_WATCH_POLL_INTERVAL = 1.0
PROGRESS_LIVENESS_INTERVAL = 15.0
RUN_ID_PATTERN = re.compile(r"[0-9a-f]{32}")

SHIM_SUFFIXES = (".cmd", ".bat")


class WorkflowError(RuntimeError):
    pass


class WindowsKillJob:
    """Own one Windows process tree through a kill-on-close Job Object."""

    def __init__(self, pid: int) -> None:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        process = None
        try:
            limits = ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = 0x00002000
            if not kernel32.SetInformationJobObject(
                job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process = kernel32.OpenProcess(0x0101, False, pid)
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel32.AssignProcessToJobObject(job, process):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            kernel32.CloseHandle(job)
            raise
        finally:
            if process:
                kernel32.CloseHandle(process)
        self._kernel32 = kernel32
        self._handle = job

    def terminate(self, exit_code: int = 1) -> None:
        if self._handle and not self._kernel32.TerminateJobObject(
            self._handle, exit_code
        ):
            import ctypes

            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def create_windows_kill_job(pid: int) -> WindowsKillJob:
    return WindowsKillJob(pid)


def resume_windows_process(pid: int) -> None:
    """Resume every initial thread of a process created in a suspended state."""
    import ctypes
    from ctypes import wintypes

    class ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ThreadEntry32),
    ]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ThreadEntry32),
    ]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    resumed = 0
    try:
        entry = ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        has_entry = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while has_entry:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                if not thread:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                    resumed += 1
                finally:
                    kernel32.CloseHandle(thread)
            has_entry = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if resumed == 0:
        raise OSError(f"could not find a thread to resume for process {pid}")


class OwnedProcess:
    """A subprocess whose descendants share its cancellable ownership scope."""

    def __init__(self, process: subprocess.Popen[Any], owner: Any = None) -> None:
        self.process = process
        self.owner = owner

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    def poll(self) -> int | None:
        return self.process.poll()

    def wait(self, timeout: float | None = None) -> int:
        try:
            return self.process.wait(timeout=timeout)
        finally:
            if self.process.poll() is not None and self.owner is not None:
                self.owner.close()
                self.owner = None

    def terminate_tree(self, timeout: float = 10.0) -> int:
        if self.poll() is not None:
            return self.wait()
        if self.owner is not None:
            try:
                self.owner.terminate()
            except OSError:
                self.owner.close()
                self.owner = None
        elif not IS_WINDOWS:
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            self.process.terminate()
        try:
            return self.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if not IS_WINDOWS:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                self.process.kill()
            return self.wait()


def terminate_process_tree(process: Any, *, timeout: float = 10.0) -> int:
    if isinstance(process, OwnedProcess):
        return process.terminate_tree(timeout=timeout)
    if process.poll() is None:
        process.terminate()
    try:
        return process.wait(timeout=timeout)
    except TypeError:
        return process.wait()
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait()


def windows_no_window_options() -> dict[str, int]:
    if not IS_WINDOWS:
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            **windows_no_window_options(),
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


def git_or_none(repo_root: Path, *arguments: str) -> str | None:
    result = run(
        ["git", "-C", str(repo_root), *arguments],
        check=False,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def git_succeeds(repo_root: Path, *arguments: str) -> bool:
    return (
        run(["git", "-C", str(repo_root), *arguments], check=False).returncode == 0
    )


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def report_event(
    report: Callable[[dict[str, Any]], None] | None,
    event: str,
    **fields: Any,
) -> None:
    if report is not None:
        report({"event": event, **fields})


def report_safely(
    report: Callable[[dict[str, Any]], None] | None,
    event: str,
    **fields: Any,
) -> None:
    try:
        report_event(report, event, **fields)
    except (OSError, TypeError, ValueError):
        pass


class ConversationProgressReporter:
    """Mirror raw events and journal concise updates for a parent agent."""

    def __init__(
        self,
        *,
        transition: Callable[[dict[str, Any]], dict[str, Any] | None],
        event_log: Path | None = None,
        output: Callable[[dict[str, Any]], None] = emit,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        self.transition = transition
        self.event_log = event_log
        self.output = output
        self.wall_time = wall_time
        self.last_wait_signature: str | None = None

    def __call__(self, payload: dict[str, Any]) -> None:
        try:
            self.output(payload)
        except (OSError, TypeError, ValueError):
            pass
        if self.event_log is None:
            return
        update = self.transition(payload)
        if update is None:
            return
        signature = json.dumps(update, sort_keys=True)
        if update.get("waiting") and signature == self.last_wait_signature:
            return
        self.last_wait_signature = signature if update.get("waiting") else None
        now = self.wall_time()
        update["reported_at_epoch"] = now
        if update.get("waiting"):
            update["wait_started_at_epoch"] = now
            update["wait_id"] = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
        try:
            self.event_log.parent.mkdir(parents=True, exist_ok=True)
            with self.event_log.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(update, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, TypeError, ValueError):
            pass


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise WorkflowError("run-id must be 32 lowercase hexadecimal characters")
    return run_id


def read_progress_log(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("event") == PROGRESS_EVENT:
            records.append(payload)
    return records


def heartbeat_update(progress: dict[str, Any], now: float) -> dict[str, Any]:
    started = progress.get("wait_started_at_epoch", progress.get("reported_at_epoch"))
    elapsed = max(0, int(now - started)) if isinstance(started, (int, float)) else 0
    minutes = max(1, elapsed // 60)
    message = progress.get("message", "The pipeline is still running.").rstrip(".")
    return {
        **{
            key: progress.get(key)
            for key in (
                "iteration",
                "pull_request_pass",
                "sweep",
                "stage",
                "pull_requests",
                "wait_id",
                "wait_reason",
                "next_action",
            )
            if progress.get(key) is not None
        },
        "event": PROGRESS_EVENT,
        "kind": "heartbeat",
        "message": f"{message} ({minutes} minutes elapsed).",
        "elapsed_seconds": elapsed,
        "waiting": True,
        "reported_at_epoch": now,
    }


def progress_update(
    records: list[dict[str, Any]],
    cursor: int,
    observer_path: Path,
    now: float,
) -> dict[str, Any]:
    updates = records[cursor:]
    latest = updates[-1]
    write_json_atomically(
        observer_path,
        {
            "cursor": len(records),
            "last_reported_at_epoch": now,
            "wait_id": latest.get("wait_id") if latest.get("waiting") else None,
        },
    )
    return {
        "event": PROGRESS_UPDATE_EVENT,
        "cursor": len(records),
        "updates": updates,
        "finished": any(update.get("terminal") for update in updates),
    }


def watch_progress(
    *,
    event_log: Path,
    launch_path: Path,
    observer_path: Path,
    cursor: int,
    wait_seconds: float,
    wall_time: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    alive: Callable[[int], bool] = lambda pid: process_is_alive(pid),
) -> dict[str, Any]:
    if cursor < 0:
        raise WorkflowError("cursor cannot be negative")
    if wait_seconds <= 0 or wait_seconds > PROGRESS_HEARTBEAT_INTERVAL:
        raise WorkflowError(
            f"wait-seconds must be greater than zero and at most "
            f"{int(PROGRESS_HEARTBEAT_INTERVAL)}"
        )
    deadline = monotonic() + wait_seconds
    last_liveness_check = float("-inf")
    while True:
        records = read_progress_log(event_log)
        cursor = min(cursor, len(records))
        if len(records) > cursor:
            return progress_update(records, cursor, observer_path, wall_time())
        latest = records[-1] if records else None
        if latest is not None and latest.get("terminal"):
            return {
                "event": PROGRESS_UPDATE_EVENT,
                "cursor": len(records),
                "updates": [],
                "finished": True,
            }

        launch = read_json(launch_path)
        if not isinstance(launch, dict):
            return {
                "event": PROGRESS_UPDATE_EVENT,
                "cursor": len(records),
                "updates": [],
                "finished": True,
                "monitor_failure": "launch_record_missing",
            }
        pid = launch.get("pid")
        current_monotonic = monotonic()
        if (
            isinstance(pid, int)
            and current_monotonic - last_liveness_check >= PROGRESS_LIVENESS_INTERVAL
        ):
            last_liveness_check = current_monotonic
            if not alive(pid):
                records = read_progress_log(event_log)
                cursor = min(cursor, len(records))
                if len(records) > cursor:
                    return progress_update(records, cursor, observer_path, wall_time())
                if records and records[-1].get("terminal"):
                    return {
                        "event": PROGRESS_UPDATE_EVENT,
                        "cursor": len(records),
                        "updates": [],
                        "finished": True,
                    }
                return {
                    "event": PROGRESS_UPDATE_EVENT,
                    "cursor": len(records),
                    "updates": [],
                    "finished": True,
                    "monitor_failure": "scheduler_exited_without_final_event",
                }

        observer = read_json(observer_path)
        last_reported = (
            observer.get("last_reported_at_epoch")
            if isinstance(observer, dict)
            else launch.get("started_at_epoch")
        )
        now = wall_time()
        if (
            latest is not None
            and isinstance(last_reported, (int, float))
            and now - last_reported >= PROGRESS_HEARTBEAT_INTERVAL
        ):
            heartbeat = heartbeat_update(latest, now)
            write_json_atomically(
                observer_path,
                {
                    "cursor": len(records),
                    "last_reported_at_epoch": now,
                    "wait_id": latest.get("wait_id"),
                },
            )
            return {
                "event": PROGRESS_UPDATE_EVENT,
                "cursor": len(records),
                "updates": [heartbeat],
                "finished": False,
            }

        remaining = deadline - monotonic()
        if remaining <= 0:
            return {
                "event": PROGRESS_UPDATE_EVENT,
                "cursor": len(records),
                "updates": [],
                "finished": False,
            }
        sleep(min(PROGRESS_WATCH_POLL_INTERVAL, remaining))


def start_detached(
    command: list[str], *, cwd: Path, log_path: Path
) -> subprocess.Popen[Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8", newline="\n")
    options: dict[str, Any] = {
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
    }
    if IS_WINDOWS:
        options["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        options["start_new_session"] = True
    try:
        return subprocess.Popen(command, **options)
    finally:
        log.close()


def gh_json(arguments: list[str]) -> Any:
    process = run(["gh", *arguments])
    try:
        return json.loads(process.stdout) if process.stdout.strip() else None
    except json.JSONDecodeError as error:
        raise WorkflowError(f"gh returned invalid JSON: {error}") from error


def graphql(
    query: str,
    variables: dict[str, Any],
    *,
    api: Callable[[list[str]], Any] = gh_json,
) -> Any:
    arguments = ["api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        flag = "-F" if isinstance(value, int) else "-f"
        arguments.extend([flag, f"{name}={value}"])
    payload = api(arguments)
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if errors:
        raise WorkflowError(f"GraphQL failed: {json.dumps(errors, sort_keys=True)}")
    return payload


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_cli_path(value: str, *, windows: bool) -> str:
    if windows:
        match = re.fullmatch(r"/([A-Za-z])(?:/(.*))?", value)
        if match:
            drive, remainder = match.groups()
            return f"{drive.upper()}:/{remainder or ''}"
    return value


def copilot_home() -> Path:
    value = os.environ.get("COPILOT_HOME", "").strip()
    return (
        Path(normalize_cli_path(value, windows=IS_WINDOWS))
        if value
        else Path.home() / ".copilot"
    )


def require_tools(names: tuple[str, ...] = ("git", "gh", "copilot")) -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise WorkflowError(f"required tools not found: {', '.join(missing)}")


def path_image(name: str) -> str | None:
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
    if not IS_WINDOWS:
        return name
    if os.sep in name or (os.altsep and os.altsep in name):
        if Path(name).suffix.lower() in SHIM_SUFFIXES:
            raise WorkflowError(f"the stage program {name} is a command shim")
        return name
    if Path(name).suffix:
        resolved = shutil.which(name)
        if resolved is None:
            raise WorkflowError(f"the stage program {name} is not on PATH")
        if Path(resolved).suffix.lower() in SHIM_SUFFIXES:
            raise WorkflowError(f"the stage program {name} resolves to a command shim")
        return resolved
    image = path_image(name)
    if image is not None:
        return image
    resolved = shutil.which(name)
    if resolved is None:
        raise WorkflowError(f"the stage program {name} is not on PATH")
    raise WorkflowError(
        f"the stage program {name} resolves only to the command shim {resolved}"
    )


def build_target(owner: str, repo: str, number: int) -> dict[str, Any]:
    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        "repo_name": f"{owner}/{repo}",
        "pr_url": f"https://github.com/{owner}/{repo}/pull/{number}",
    }


def target_for(repo_name: str, number: int) -> dict[str, Any]:
    match = REPO_NAME_PATTERN.fullmatch(repo_name.strip())
    if not match:
        raise WorkflowError(f"{repo_name!r} is not an owner/repo repository name")
    return build_target(match.group("owner"), match.group("repo"), number)


def parse_target(target: str, repo_name: str | None = None) -> dict[str, Any]:
    match = PR_URL_PATTERN.fullmatch(target) or SHORT_TARGET_PATTERN.fullmatch(target)
    if match:
        values = match.groupdict()
        return build_target(values["owner"], values["repo"], int(values["number"]))
    bare = BARE_NUMBER_PATTERN.fullmatch(target)
    if bare and repo_name:
        owner, separator, repo = repo_name.partition("/")
        if separator and owner and repo:
            return build_target(owner, repo, int(bare.group("number")))
    if bare:
        raise WorkflowError("a bare PR number requires repository context")
    raise WorkflowError(
        "target must be a GitHub PR URL, owner/repo#number, or bare PR number"
    )


def resolve_repo_root() -> Path:
    root = run(["git", "rev-parse", "--show-toplevel"]).stdout.strip()
    if not root:
        raise WorkflowError("the current directory is not in a git repository")
    return Path(root).resolve()


def github_repo_from_remote(url: str) -> str | None:
    match = re.search(
        r"(?:github\.com[/:])(?P<owner>[^/:\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?$",
        url.strip(),
        re.IGNORECASE,
    )
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def repo_name_for(repo_root: Path) -> str | None:
    result = run(
        ["gh", "repo", "view", "--json", "nameWithOwner"],
        cwd=repo_root,
        check=False,
    )
    if result.returncode == 0:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = None
        name = payload.get("nameWithOwner") if isinstance(payload, dict) else None
        if isinstance(name, str) and "/" in name:
            return name
    remote = git_or_none(repo_root, "remote", "get-url", "origin")
    return github_repo_from_remote(remote or "")


def resolve_target(
    value: str | None,
    repo_root: Path,
    *,
    api: Callable[[list[str]], Any] = gh_json,
) -> dict[str, Any]:
    if value:
        return parse_target(value, repo_name_for(repo_root))
    payload = api(["pr", "view", "--json", "url"])
    url = payload.get("url") if isinstance(payload, dict) else None
    if not isinstance(url, str):
        raise WorkflowError(
            "no pull request was named and the checked-out branch has no pull request"
        )
    return parse_target(url)


def base_ref_tip(
    repo_name: str,
    base_branch: str,
    *,
    api: Callable[[list[str]], Any] = gh_json,
) -> str:
    payload = api(["api", f"repos/{repo_name}/git/ref/heads/{base_branch}"])
    obj = payload.get("object") if isinstance(payload, dict) else None
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if not isinstance(sha, str) or not sha:
        raise WorkflowError(
            f"the tip of base branch {base_branch!r} in {repo_name} has no commit SHA"
        )
    return sha


def read_pull_request(
    target: dict[str, Any],
    *,
    api: Callable[[list[str]], Any] = gh_json,
    base_tip: Callable[..., str] = base_ref_tip,
) -> dict[str, Any]:
    payload = api(
        [
            "pr",
            "view",
            str(target["number"]),
            "--repo",
            target["repo_name"],
            "--json",
            "number,title,url,state,isDraft,headRefName,baseRefName,headRefOid",
        ]
    )
    if not isinstance(payload, dict):
        raise WorkflowError(f"could not read {target['pr_url']}")
    base_branch = payload.get("baseRefName")
    if not isinstance(base_branch, str) or not base_branch:
        raise WorkflowError(f"{target['pr_url']} has no base branch")
    return {
        "number": payload.get("number"),
        "title": payload.get("title"),
        "pr_url": payload.get("url") or target["pr_url"],
        "repo_name": target["repo_name"],
        "owner": target["owner"],
        "repo": target["repo"],
        "state": payload.get("state"),
        "is_draft": bool(payload.get("isDraft")),
        "head_branch": payload.get("headRefName"),
        "base_branch": base_branch,
        "base_sha": base_tip(target["repo_name"], base_branch),
        "head_sha": payload.get("headRefOid"),
    }


def commit_url(target: dict[str, Any], sha: str) -> str:
    return f"{target['pr_url']}/commits/{sha}"


def read_pr_commits(
    target: dict[str, Any],
    *,
    api: Callable[[list[str]], Any] = gh_json,
) -> list[dict[str, Any]]:
    payload = api(
        [
            "pr",
            "view",
            str(target["number"]),
            "--repo",
            target["repo_name"],
            "--json",
            "commits",
        ]
    )
    commits = payload.get("commits") if isinstance(payload, dict) else None
    if not isinstance(commits, list):
        raise WorkflowError(f"could not read commits for {target['pr_url']}")
    result = []
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        sha = commit.get("oid")
        if not isinstance(sha, str) or not sha:
            continue
        result.append(
            {
                "sha": sha,
                "title": commit.get("messageHeadline") or sha,
                "url": commit_url(target, sha),
            }
        )
    return result


def snapshot_pr_commits(
    target: dict[str, Any],
    *,
    read: Callable[..., list[dict[str, Any]]] = read_pr_commits,
) -> dict[str, Any]:
    try:
        return {"commits": read(target)}
    except WorkflowError as error:
        return {"commits": [], "error": str(error)}


def commits_added(
    before: dict[str, Any], after: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str], bool]:
    errors = [
        snapshot["error"]
        for snapshot in (before, after)
        if isinstance(snapshot.get("error"), str)
    ]
    if errors:
        return [], errors, False
    before_shas = {
        commit["sha"]
        for commit in before["commits"]
        if isinstance(commit.get("sha"), str)
    }
    before_head = before["commits"][-1].get("sha") if before["commits"] else None
    after_shas = {
        commit["sha"]
        for commit in after["commits"]
        if isinstance(commit.get("sha"), str)
    }
    history_rewritten = bool(before_head and before_head not in after_shas)
    return [
        commit for commit in after["commits"] if commit.get("sha") not in before_shas
    ], [], history_rewritten


def local_commits_between(
    repo_root: Path, base_sha: str | None, head_sha: str | None
) -> list[dict[str, str]]:
    if not base_sha or not head_sha or base_sha == head_sha:
        return []
    if not git_succeeds(repo_root, "merge-base", "--is-ancestor", base_sha, head_sha):
        base_sha = git_or_none(repo_root, "merge-base", base_sha, head_sha)
        if not base_sha:
            return []
    output = git_or_none(
        repo_root,
        "log",
        "--reverse",
        "--first-parent",
        "--format=%H%x09%s",
        f"{base_sha}..{head_sha}",
    )
    commits = []
    for line in (output or "").splitlines():
        sha, separator, title = line.partition("\t")
        if separator and sha:
            commits.append({"sha": sha, "title": title or sha})
    return commits


def target_remote(repo_root: Path, target: dict[str, Any]) -> str:
    wanted = target["repo_name"].lower()
    listing = git_or_none(repo_root, "remote", "-v") or ""
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            name = github_repo_from_remote(fields[1])
            if name and name.lower() == wanted:
                return fields[0]
    return f"https://github.com/{target['repo_name']}.git"


def worktree_dirt(repo_root: Path) -> str:
    return git(repo_root, "status", "--porcelain=v1")


def unreachable_commit_count(repo_root: Path) -> int:
    value = git_or_none(
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
        return int(value or "0")
    except ValueError:
        return 0


def fetch_pr_head(
    repo_root: Path,
    target: dict[str, Any],
    *,
    remote_for: Callable[[Path, dict[str, Any]], str] = target_remote,
) -> dict[str, Any]:
    remote = remote_for(repo_root, target)
    reference = f"refs/pull/{target['number']}/head"
    result = run(
        ["git", "-C", str(repo_root), "fetch", "--quiet", remote, reference],
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        return {
            "result": "blocked",
            "reason": "checkout_failed",
            "detail": f"could not fetch {reference}: {detail}",
        }
    landed = git_or_none(repo_root, "rev-parse", "FETCH_HEAD")
    if not landed:
        return {
            "result": "blocked",
            "reason": "checkout_failed",
            "detail": f"fetching {reference} did not produce FETCH_HEAD",
        }
    return {"result": "ready", "head_sha": landed}


def checkout_fetched_head(repo_root: Path, head_sha: str) -> dict[str, Any]:
    result = run(
        ["git", "-C", str(repo_root), "checkout", "--quiet", "--detach", head_sha],
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        return {
            "result": "blocked",
            "reason": "checkout_failed",
            "detail": f"could not check out pull request head {head_sha}: {detail}",
        }
    landed = git_or_none(repo_root, "rev-parse", "HEAD")
    if landed != head_sha:
        return {
            "result": "blocked",
            "reason": "checkout_failed",
            "detail": (
                f"the worktree is on {landed} after checking out pull request "
                f"head {head_sha}"
            ),
        }
    return {
        "result": "ready",
        "head_sha": landed,
    }


def sync_worktree(
    repo_root: Path,
    target: dict[str, Any],
    pr: dict[str, Any],
    *,
    known_safe_head: str | None,
    fetch: Callable[..., dict[str, Any]] = fetch_pr_head,
    checkout: Callable[..., dict[str, Any]] = checkout_fetched_head,
) -> dict[str, Any]:
    dirt = worktree_dirt(repo_root)
    if dirt:
        return {
            "result": "blocked",
            "reason": "dirty_worktree",
            "detail": f"the worktree has uncommitted changes:\n{dirt}",
        }
    fetched = fetch(repo_root, target)
    if fetched["result"] != "ready":
        return fetched
    desired = fetched["head_sha"]
    local = git(repo_root, "rev-parse", "HEAD")
    if local == desired:
        return {"result": "ready", "head_sha": local, "changed": False}

    branch = git_or_none(repo_root, "branch", "--show-current") or ""
    safe_to_move = local == known_safe_head
    safe_to_move = safe_to_move or git_succeeds(
        repo_root, "merge-base", "--is-ancestor", local, desired
    )
    if branch and branch != pr.get("head_branch"):
        safe_to_move = True
    if not safe_to_move and unreachable_commit_count(repo_root) == 0:
        safe_to_move = branch != pr.get("head_branch")
    if not safe_to_move:
        return {
            "result": "blocked",
            "reason": "local_head_not_published",
            "detail": (
                f"the worktree head {local} is not the pull request head {desired}; "
                "moving it could hide local commits"
            ),
        }
    checked_out = checkout(repo_root, desired)
    if checked_out["result"] != "ready":
        return checked_out
    return {
        **checked_out,
        "changed": True,
        "previous_head_sha": local,
    }


def settle_after_stage(
    repo_root: Path,
    target: dict[str, Any],
    *,
    started_head_sha: str,
    fetch: Callable[..., dict[str, Any]] = fetch_pr_head,
    checkout: Callable[..., dict[str, Any]] = checkout_fetched_head,
) -> dict[str, Any]:
    dirt = worktree_dirt(repo_root)
    if dirt:
        return {
            "result": "blocked",
            "reason": "stage_left_dirty_worktree",
            "detail": f"a stage left uncommitted changes:\n{dirt}",
        }
    local = git(repo_root, "rev-parse", "HEAD")
    fetched = fetch(repo_root, target)
    if fetched["result"] != "ready":
        return fetched
    remote = fetched["head_sha"]
    if local == remote:
        return {
            "result": "ready",
            "head_sha": remote,
            "local_head_sha": local,
            "pr_head_sha": remote,
            "changed": remote != started_head_sha,
        }

    published = git_succeeds(
        repo_root, "merge-base", "--is-ancestor", local, remote
    )
    if local != started_head_sha and not published:
        return {
            "result": "blocked",
            "reason": "stage_left_unpublished_commits",
            "local_head_sha": local,
            "pr_head_sha": remote,
            "detail": (
                f"a stage moved the local head from {started_head_sha} to {local}, "
                f"but the pull request head is {remote}"
            ),
        }
    checked_out = checkout(repo_root, remote)
    if checked_out["result"] != "ready":
        return checked_out
    return {
        **checked_out,
        "local_head_sha": local,
        "pr_head_sha": remote,
        "changed": checked_out["head_sha"] != started_head_sha,
        "previous_head_sha": local,
    }


def stage_script_path(entry: dict[str, Any]) -> Path:
    return (
        copilot_home()
        / "installed-plugins"
        / "trask-plugins"
        / entry["plugin"]
        / "scripts"
        / f"{entry['module']}.py"
    )


def stage_state_path(entry: dict[str, Any], target: dict[str, Any]) -> Path:
    name = f"{target['owner']}--{target['repo']}--{target['number']}.json"
    return Path.home() / ".copilot" / "run" / entry["plugin"] / name


def stage_live_progress(
    entry: dict[str, Any],
    target: dict[str, Any],
    *,
    state_for: Callable[[dict[str, Any], dict[str, Any]], Path] = stage_state_path,
) -> dict[str, Any] | None:
    payload = read_json(state_for(entry, target))
    structured = payload.get("stage_progress") if isinstance(payload, dict) else None
    if isinstance(structured, dict) and isinstance(structured.get("phase"), str):
        return {
            key: structured.get(key)
            for key in ("phase", "detail", "observed_at")
            if structured.get(key) is not None
        }
    run_state = payload.get("run") if isinstance(payload, dict) else None
    decision = run_state.get("decision") if isinstance(run_state, dict) else None
    if not isinstance(decision, dict):
        return None
    action = decision.get("action")
    phase = {
        "attribute": "diagnosing",
        "fix": "fixing",
        "rerun": "rerunning",
        "waiting": "waiting",
    }.get(action)
    if phase is None:
        return None
    return {
        "phase": phase,
        "action": action,
        "reason": decision.get("reason"),
        "action_checks": decision.get("action_checks") or decision.get("checks") or [],
        "pending_checks": decision.get("pending_checks") or [],
        "observed_at": decision.get("observed_at"),
        "head_sha": run_state.get("head_sha"),
    }


def read_stage_status(
    entry: dict[str, Any],
    target: dict[str, Any],
    *,
    script_for: Callable[[dict[str, Any]], Path] = stage_script_path,
    state_for: Callable[[dict[str, Any], dict[str, Any]], Path] = stage_state_path,
) -> dict[str, Any]:
    """Ask one stage helper for its own machine-readable state.

    Each stage owns its state file and prints a compact ``status`` envelope.
    Reading that envelope, rather than a stage's prose, is the only supported
    way to learn whether the stage finished and at which revisions.
    """
    script = script_for(entry)
    state = state_for(entry, target)
    common = {
        "installed": script.is_file(),
        "script": str(script),
        "state": str(state),
        "payload": None,
    }
    if not script.is_file():
        return {**common, "ok": False, "reason": "plugin_not_installed"}
    if not state.is_file():
        return {**common, "ok": False, "reason": "no_state"}
    try:
        process = run(
            [sys.executable, str(script), "status", "--state", str(state)],
            check=False,
            timeout=30,
        )
    except WorkflowError as error:
        return {
            **common,
            "ok": False,
            "reason": "status_timeout",
            "detail": str(error),
        }
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "no output"
        return {
            **common,
            "ok": False,
            "reason": "status_failed",
            "detail": detail,
        }
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        return {
            **common,
            "ok": False,
            "reason": "invalid_status_json",
            "detail": str(error),
        }
    if not isinstance(payload, dict) or payload.get("result") != "ready":
        return {
            **common,
            "ok": False,
            "reason": "status_not_ready",
            "payload": payload,
        }
    return {**common, "ok": True, "payload": payload}


def string_at(payload: dict[str, Any], path: tuple[str, ...]) -> str | None:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


STAGE_STATUS_FIELDS = (
    "attempt",
    "counts",
    "escalation",
    "iterations",
    "last_helper_activity",
    "local_validation",
    "mergeable_at_head_sha",
    "monitoring",
    "outcome",
    "proposal",
    "proposal_count",
    "progress",
    "queue",
    "review",
    "run",
    "skip_note",
    "validation",
    "validated_head_sha",
    "verdicts",
)


def stage_status_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {key: payload[key] for key in STAGE_STATUS_FIELDS if key in payload}


def inspect_stage(
    entry: dict[str, Any],
    target: dict[str, Any],
    head_sha: str,
    base_sha: str | None = None,
    *,
    read_status: Callable[..., dict[str, Any]] = read_stage_status,
) -> dict[str, Any]:
    """Decide whether one stage is clear for exactly these revisions.

    A stage is clear only when its own marker names the head being inspected,
    any base marker names the base being inspected, and its recorded outcome is
    one that clears the stage. A marker from an older head or base is reported
    as stale rather than as clearance, so a moved pull request never inherits
    an earlier stage's result.
    """
    status = read_status(entry, target)
    payload = status.get("payload")
    marker = (
        string_at(payload, entry["marker"]) if isinstance(payload, dict) else None
    )
    base_marker_path = entry.get("base_marker")
    base_marker = (
        string_at(payload, base_marker_path)
        if isinstance(payload, dict) and isinstance(base_marker_path, tuple)
        else None
    )
    outcome = payload.get("stage_outcome") if isinstance(payload, dict) else None
    head_is_clear = marker == head_sha
    base_is_clear = base_marker_path is None or (
        base_sha is not None and base_marker == base_sha
    )
    clear = head_is_clear and base_is_clear and outcome in CLEARING_OUTCOMES
    if clear:
        reason = None
    elif marker and not head_is_clear:
        reason = "clearance_is_for_an_older_head"
    elif base_marker_path is not None and base_marker != base_sha:
        reason = "clearance_is_for_an_older_base"
    else:
        reason = status.get("reason") or outcome or "not_cleared"
    return {
        "stage": entry["stage"],
        "clear": clear,
        "clear_at_head_sha": marker,
        "clear_at_base_sha": base_marker,
        "outcome": outcome,
        "reason": reason,
        "installed": status["installed"],
        "status_state": status["state"],
        "status": stage_status_summary(payload),
        **({"detail": status["detail"]} if status.get("detail") else {}),
    }


def inspect_stages(
    target: dict[str, Any],
    head_sha: str,
    base_sha: str,
    *,
    inspect: Callable[..., dict[str, Any]] = inspect_stage,
) -> list[dict[str, Any]]:
    return [inspect(entry, target, head_sha, base_sha) for entry in STAGES]


def stage_models(overrides: list[str] | None) -> dict[str, str]:
    """Resolve the model each stage runs on, honoring per-stage overrides.

    Self review is the one stage pinned to a Claude model, because it depends
    on that family's review behavior; an override that changes its family is
    rejected instead of silently accepted.
    """
    models = {entry["stage"]: entry["model"] for entry in STAGES}
    for assignment in overrides or []:
        stage, separator, model = assignment.partition("=")
        if not separator or stage not in STAGE_BY_NAME or not model.strip():
            raise WorkflowError(
                f"--stage-model expects <stage>=<model> for a known stage: {assignment}"
            )
        models[stage] = model.strip()
    for entry in STAGES:
        family = entry.get("requires_family")
        if family and family not in models[entry["stage"]].lower():
            raise WorkflowError(
                f"{entry['stage']} requires a {family} model, not "
                f"{models[entry['stage']]}"
            )
    return models


def stage_accepts_pipeline_position(
    entry: dict[str, Any],
    *,
    script_for: Callable[[dict[str, Any]], Path] = stage_script_path,
) -> bool:
    try:
        return PIPELINE_RUN_FLAG in script_for(entry).read_text(encoding="utf-8")
    except OSError:
        return False


def pipeline_arguments(
    entry: dict[str, Any],
    run_id: str,
    iteration: int,
    max_iterations: int,
    *,
    accepts: Callable[..., bool] = stage_accepts_pipeline_position,
) -> list[str]:
    """Give a stage its position in the pipeline so its own budget can shrink.

    A stage that does not understand these flags is left alone, so a stage
    helper that predates them keeps its standalone behavior.
    """
    if not accepts(entry):
        return []
    return [
        PIPELINE_RUN_FLAG,
        run_id,
        PIPELINE_ITERATION_FLAG,
        str(iteration),
        PIPELINE_MAX_ITERATIONS_FLAG,
        str(max_iterations),
    ]


def stage_prompt(target: dict[str, Any], arguments: list[str]) -> str:
    name = f"{target['repo_name']}#{target['number']}"
    if not arguments:
        return name
    pairs = zip(arguments[::2], arguments[1::2])
    position = " ".join(f"{flag.lstrip('-')}: {value}" for flag, value in pairs)
    return (
        f"{name}\n\n{position}\n\n"
        "Add these arguments to your preflight command, exactly as written, "
        f"and change nothing else about how you run: {' '.join(arguments)}"
    )


def stage_command(
    entry: dict[str, Any],
    target: dict[str, Any],
    *,
    model: str,
    effort: str,
    arguments: list[str],
    prompt: str | None = None,
    resolve_program: Callable[[str], str] = resolve_launch_program,
) -> list[str]:
    return [
        resolve_program("copilot"),
        "-p",
        prompt if prompt is not None else stage_prompt(target, arguments),
        "--agent",
        entry["agent"],
        "--model",
        model,
        "--effort",
        effort,
        *STAGE_AUTOPILOT_FLAGS,
        *STAGE_PERMISSION_FLAGS,
    ]


def launch_options(log: Any, cwd: Path) -> dict[str, Any]:
    options: dict[str, Any] = {
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
    }
    options.update(windows_no_window_options())
    return options


def run_foreground(
    command: list[str], *, cwd: Path, log_path: Path
) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    try:
        with open(log_path, "w", encoding="utf-8", newline="\n") as log:
            process = subprocess.run(
                command, check=False, **launch_options(log, cwd)
            )
        return {
            "returncode": process.returncode,
            "log_path": str(log_path),
            "started_at": started_at,
            "ended_at": utc_now(),
        }
    except OSError as error:
        return {
            "returncode": None,
            "log_path": str(log_path),
            "started_at": started_at,
            "ended_at": utc_now(),
            "error": str(error),
        }


def start_background(
    command: list[str], *, cwd: Path, log_path: Path
) -> OwnedProcess:
    """Start one stage process that keeps running after this call returns."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "w", encoding="utf-8", newline="\n")
    options = launch_options(log, cwd)
    if IS_WINDOWS:
        options["creationflags"] = (
            options.get("creationflags", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
            | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
        )
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **options)
        owner = None
        try:
            owner = create_windows_kill_job(process.pid) if IS_WINDOWS else None
            if IS_WINDOWS:
                resume_windows_process(process.pid)
        except BaseException:
            if owner is not None:
                owner.close()
            else:
                process.terminate()
            process.wait()
            raise
        return OwnedProcess(process, owner)
    finally:
        log.close()


def run_monitored(
    command: list[str],
    *,
    cwd: Path,
    log_path: Path,
    progress: Callable[[], None],
    interval: float = 5.0,
    start: Callable[..., Any] = start_background,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    started_at = utc_now()
    try:
        process = start(command, cwd=cwd, log_path=log_path)
    except OSError as error:
        return {
            "returncode": None,
            "log_path": str(log_path),
            "started_at": started_at,
            "ended_at": utc_now(),
            "error": str(error),
        }
    try:
        progress()
        while process.poll() is None:
            sleep(interval)
            progress()
    except BaseException:
        if process.poll() is None:
            terminate_process_tree(process)
        raise
    return {
        "returncode": process.wait(),
        "log_path": str(log_path),
        "started_at": started_at,
        "ended_at": utc_now(),
    }


def windows_process_is_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    error_access_denied = 5
    error_invalid_parameter = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == error_access_denied:
            return True
        if error == error_invalid_parameter:
            return False
        raise OSError(error, f"failed to query process {pid}")
    try:
        result = kernel32.WaitForSingleObject(handle, 0)
        if result == wait_timeout:
            return True
        if result == wait_object_0:
            return False
        error = ctypes.get_last_error()
        raise OSError(error, f"failed to query process {pid}")
    finally:
        kernel32.CloseHandle(handle)


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        return windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    """Replace a state file in one step so a crash never leaves a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
