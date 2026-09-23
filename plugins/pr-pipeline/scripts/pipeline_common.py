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
COORDINATOR_MODEL_ARGUMENTS = {
    "gpt-5.6-sol": "sol",
    "gpt-5.6-luna": "luna",
    "gpt-5.6-terra": "terra",
    "gpt-6-astra": "astra",
}
CONFLICT_STRATEGIES = ("auto", "merge", "rebase")
SELF_REVIEW_MODEL = "gpt-5.6-sol"
SELF_REVIEW_EFFORT = "high"
SOURCE_ONLY_POLICY_SKIP_RESULT = "source_only_review_not_applicable"
SOURCE_ONLY_POLICY_SKIP_REASON = "review_request_forbidden"
SOURCE_ONLY_POLICY_SKIP_DETAIL = (
    "Copilot Review is not applicable under source-only policy because no "
    "Copilot review exists at the frozen head and requesting one is forbidden"
)
IS_WINDOWS = os.name == "nt"
_EXECUTION = None

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
        "required_model": DEFAULT_STAGE_MODEL,
    },
    {
        "stage": STAGE_COPILOT_REVIEW,
        "plugin": STAGE_COPILOT_REVIEW,
        "agent": f"{STAGE_COPILOT_REVIEW}:{STAGE_COPILOT_REVIEW}",
        "module": "copilot_review_loop",
        "marker": ("clean_at_head_sha",),
        "base_marker": ("clean_at_base_sha",),
        "skip_marker": ("policy_skip", "head_sha"),
        "model": DEFAULT_STAGE_MODEL,
        "required_model": DEFAULT_STAGE_MODEL,
        "required_effort": DEFAULT_EFFORT,
        "github_mutation_policy": True,
    },
    {
        "stage": STAGE_SELF_REVIEW,
        "plugin": STAGE_SELF_REVIEW,
        "agent": f"{STAGE_SELF_REVIEW}:{STAGE_SELF_REVIEW}",
        "module": "self_review_loop",
        "marker": ("review", "clean_at_head_sha"),
        "base_marker": ("review", "clean_at_base_sha"),
        "model": SELF_REVIEW_MODEL,
        "required_model": SELF_REVIEW_MODEL,
        "required_effort": SELF_REVIEW_EFFORT,
        "github_mutation_policy": True,
    },
    {
        "stage": STAGE_CI,
        "plugin": STAGE_CI,
        "agent": f"{STAGE_CI}:{STAGE_CI}",
        "module": "ci_fix_loop",
        "marker": ("clean_at_head_sha",),
        "base_marker": ("clean_at_base_sha",),
        "model": DEFAULT_STAGE_MODEL,
        "required_model": DEFAULT_STAGE_MODEL,
        "github_mutation_policy": True,
    },
    {
        "stage": STAGE_DESCRIPTION,
        "plugin": STAGE_DESCRIPTION,
        "agent": f"{STAGE_DESCRIPTION}:{STAGE_DESCRIPTION}",
        "module": "pr_description",
        "marker": ("validated_head_sha",),
        "base_marker": ("pr", "base", "sha"),
        "model": DEFAULT_STAGE_MODEL,
        "github_mutation_policy": True,
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

    def __init__(
        self, process: subprocess.Popen[Any], owner: Any = None,
        launch_receipt: dict[str, Any] | None = None,
    ) -> None:
        self.process = process
        self.owner = owner
        self.launch_receipt = launch_receipt

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
    if _EXECUTION is not None and hasattr(process, "terminate_tree"):
        return process.terminate_tree(timeout=timeout)
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
        process = (_EXECUTION.run if _EXECUTION else subprocess.run)(
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
    if _EXECUTION is not None:
        _EXECUTION.emit(payload)
    print(json.dumps(payload, sort_keys=True), flush=True)


def serialized_size(payload: Any) -> int:
    return len((json.dumps(payload, sort_keys=True) + os.linesep).encode("utf-8"))


def clipped_text(value: Any, limit: int = 512) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def bounded_value(
    value: Any,
    *,
    text_limit: int = 512,
    collection_limit: int = 12,
    depth: int = 0,
) -> tuple[Any, bool]:
    """Return a deterministic preview while leaving canonical data untouched."""
    if isinstance(value, str):
        return (
            value[:text_limit] + "..." if len(value) > text_limit else value,
            len(value) > text_limit,
        )
    if not isinstance(value, (dict, list)):
        return value, False
    if depth >= 5:
        return None, bool(value)
    items = list(value.items()) if isinstance(value, dict) else list(enumerate(value))
    limit = 32 if isinstance(value, dict) else collection_limit
    truncated = len(items) > limit
    result: Any = {} if isinstance(value, dict) else []
    for key, item in items[:limit]:
        preview, shortened = bounded_value(
            item,
            text_limit=text_limit,
            collection_limit=collection_limit,
            depth=depth + 1,
        )
        truncated |= shortened
        if isinstance(result, dict):
            if len(key) > text_limit:
                truncated = True
                continue
            result[key] = preview
        else:
            result.append(preview)
    return result, truncated


def canonical_terminal_payload(
    payload: dict[str, Any],
    result_path: Path,
    *,
    envelope_key: str | None = None,
) -> tuple[dict[str, Any], str]:
    path = result_path.resolve()
    raw = path.read_bytes()
    persisted = json.loads(raw)
    canonical = persisted.get(envelope_key) if envelope_key else persisted
    if canonical != payload:
        raise WorkflowError(
            f"terminal result does not match canonical artifact: {path}"
        )
    return canonical, hashlib.sha256(raw).hexdigest()


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
        if _EXECUTION is not None:
            raise


class ForegroundProgressReporter:
    """Emit controller events through the active foreground execution."""

    def __init__(
        self,
        *,
        output: Callable[[dict[str, Any]], None] = emit,
    ) -> None:
        self.output = output

    def __call__(self, payload: dict[str, Any]) -> None:
        try:
            self.output(payload)
        except (OSError, TypeError, ValueError):
            if _EXECUTION is not None:
                raise


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise WorkflowError("run-id must be 32 lowercase hexadecimal characters")
    return run_id


def popen_with_windows_breakaway_fallback(
    command: list[str],
    options: dict[str, Any],
    *,
    operation: str,
) -> tuple[subprocess.Popen[Any], bool]:
    """Start once, retrying only a rejected Windows job breakaway request."""
    try:
        return subprocess.Popen(command, **options), True
    except OSError as error:
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        if (
            not IS_WINDOWS
            or getattr(error, "winerror", None) != 5
            or not options.get("creationflags", 0) & breakaway
        ):
            raise
    fallback_options = dict(options)
    fallback_options["creationflags"] &= ~breakaway
    try:
        return subprocess.Popen(command, **fallback_options), False
    except OSError as error:
        raise WorkflowError(
            f"{operation} launch failed for {command[0]} after Windows denied "
            f"CREATE_BREAKAWAY_FROM_JOB: {error}"
        ) from error


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
            "number,title,url,state,isDraft,headRefName,baseRefName,headRefOid"
            + (",headRepository,headRepositoryOwner" if _EXECUTION is not None else ""),
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
        **({"head_repository": (
            payload["headRepositoryOwner"]["login"] + "/" + payload["headRepository"]["name"]
        )} if _EXECUTION is not None else {}),
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


def stage_state_path(
    entry: dict[str, Any], target: dict[str, Any], run_id: str | None = None
) -> Path:
    name = f"{target['owner']}--{target['repo']}--{target['number']}.json"
    if run_id is not None:
        digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
        name = f"{name[:-5]}--invocation-{digest}.json"
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


def hosted_task_progress(*, observed_after: float) -> dict[str, Any] | None:
    if _EXECUTION is None or not hasattr(_EXECUTION, "latest_dispatch_progress"):
        return None
    event = _EXECUTION.latest_dispatch_progress(observed_after=observed_after)
    if not isinstance(event, dict):
        return None
    task = event.get("task")
    state = task.get("state") if isinstance(task, dict) else None
    if not isinstance(state, str) or not state:
        state = event.get("remote_status")
    if not isinstance(state, str) or not state:
        return None
    progress = {
        "phase": "hosted_task",
        "hosted_task_state": state,
        "observed_at": event.get("observed_at"),
        "elapsed_seconds": event.get("elapsed_seconds"),
    }
    if isinstance(task, dict) and isinstance(task.get("id"), str):
        progress["hosted_task_id"] = task["id"]
    return progress


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
    command = [sys.executable, str(script), "status", "--state", str(state)]
    if entry["stage"] == STAGE_CI:
        command.append("--verify-clearance-snapshot")
    if entry["stage"] == STAGE_DESCRIPTION:
        command.append("--verify-clearance-snapshot")
    try:
        process = run(
            command,
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
    reported_state = payload.get("state")
    try:
        state_matches = (
            isinstance(reported_state, str)
            and bool(reported_state)
            and os.path.normcase(str(Path(reported_state).resolve()))
            == os.path.normcase(str(state.resolve()))
        )
    except OSError:
        state_matches = False
    if not state_matches:
        return {
            **common,
            "ok": False,
            "reason": "status_state_mismatch",
            "detail": (
                "stage status named a different state file: "
                f"{payload.get('state')!r}"
            ),
            "payload": None,
        }
    pr = payload.get("pr")
    coordinator = payload.get("coordinator")
    escalation = payload.get("escalation")
    pre_identity_coordinator_error = (
        entry["stage"] == STAGE_CI
        and pr is None
        and isinstance(coordinator, dict)
        and coordinator.get("status") == "blocked"
        and isinstance(escalation, dict)
        and escalation.get("reason") == "coordinator_error"
    )
    if not pre_identity_coordinator_error and (
        not isinstance(pr, dict)
        or pr.get("number") != target["number"]
        or str(pr.get("repo_name") or "").casefold()
        != str(target["repo_name"]).casefold()
    ):
        return {
            **common,
            "ok": False,
            "reason": "status_identity_mismatch",
            "detail": "stage status named a different pull request",
            "payload": None,
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
    "clean_at_head_sha",
    "clean_at_base_sha",
    "agent_task",
    "attempt",
    "budget_scope",
    "ci_warnings",
    "clearance_verification",
    "coordinator",
    "escalation",
    "github_mutation_policy",
    "history",
    "iterations",
    "last_result",
    "last_helper_activity",
    "managed_task_history",
    "mergeable_at_head_sha",
    "monitoring",
    "native_stack_clearance",
    "outcome",
    "pipeline_budget",
    "pipeline_run",
    "pipeline_iteration",
    "pipeline_max_iterations",
    "policy_skip",
    "proposal",
    "proposal_count",
    "progress",
    "queue",
    "review",
    "run",
    "run_id",
    "skip_note",
    "terminal_exit",
    "thread_mutations",
    "validated_head_sha",
    "warning_at_head_sha",
    "warning_at_base_sha",
    "warning_verification",
)

ACTIVE_TASK_STATES = frozenset(
    {
        "preparing",
        "dispatching",
        "running",
        "resuming",
        "validated",
        "publishing",
        "published",
    }
)
RECOVERY_TASK_STATES = frozenset(
    {
        "failed",
        "failed_after_mutation",
        "failed_after_publication",
        "interrupted",
        "normalization_required",
    }
)
UNAVAILABLE_STATUS_REASONS = frozenset(
    {
        "status_timeout",
        "status_failed",
        "invalid_status_json",
        "status_not_ready",
        "status_state_mismatch",
        "status_identity_mismatch",
    }
)


def stage_status_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {key: payload[key] for key in STAGE_STATUS_FIELDS if key in payload}


def proven_source_drift(
    payload: Any,
    *,
    head_sha: str,
    pipeline_run: str | None,
) -> dict[str, Any] | None:
    """Return bounded evidence that a completed candidate lost its source lease."""
    if not isinstance(payload, dict) or not pipeline_run:
        return None
    task = payload.get("agent_task")
    drift = task.get("source_drift") if isinstance(task, dict) else None
    iteration = payload.get("pipeline_iteration")
    maximum = payload.get("pipeline_max_iterations")
    if (
        not isinstance(task, dict)
        or task.get("status") != "head_changed"
        or not isinstance(task.get("task"), dict)
        or task["task"].get("state") != "completed"
        or not isinstance(drift, dict)
        or payload.get("pipeline_run") != pipeline_run
        or type(iteration) is not int
        or type(maximum) is not int
        or not 1 <= iteration <= maximum
        or drift.get("pipeline_iteration") != iteration
        or drift.get("pipeline_max_iterations") != maximum
        or drift.get("consumed_allowance") != 1
        or drift.get("remaining_allowance") != maximum - iteration
        or drift.get("observed_head_sha") != head_sha
        or drift.get("expected_head_sha") == head_sha
    ):
        return None
    expected = drift.get("expected_head_sha")
    observed = drift.get("observed_head_sha")
    if not all(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value)
        for value in (expected, observed)
    ):
        return None
    mutation_guards = (
        "mutation_performed",
        "source_mutation_performed",
        "review_mutation_performed",
        "rebase_performed",
        "publication_performed",
    )
    adoption_guards = ("recommendation_adopted", "adoption_performed")
    if (
        not any(drift.get(key) is False for key in mutation_guards[:2])
        or not any(drift.get(key) is False for key in adoption_guards)
        or drift.get("publication_performed") is not False
        or any(drift.get(key) is not False for key in mutation_guards if key in drift)
        or any(drift.get(key) is not False for key in adoption_guards if key in drift)
    ):
        return None
    return {
        key: drift[key]
        for key in (
            "expected_head_sha",
            "observed_head_sha",
            "pipeline_iteration",
            "pipeline_max_iterations",
            "consumed_allowance",
            "remaining_allowance",
            *mutation_guards,
            *adoption_guards,
        )
        if key in drift
    }


def stage_failure_summary(stage_result: Any, *, text_limit: int = 512) -> dict[str, Any]:
    """Preview a retained task error without changing the controller's stop reason."""
    if not isinstance(stage_result, dict):
        return {}
    sealed = stage_result.get("sealed_terminal")
    if isinstance(sealed, dict):
        workflow = sealed.get("workflow_result")
        error = workflow.get("error") if isinstance(workflow, dict) else None
        if error is None:
            error = sealed.get("error")
        if isinstance(error, dict):
            error = ": ".join(
                value for key in ("code", "message")
                if isinstance(value := error.get(key), str) and value.strip()
            )
        if isinstance(error, str) and error.strip():
            return {
                "stage": stage_result.get("stage"),
                "error": error,
                "child_run_id": sealed.get("run_id"),
                "child_result_sha256": sealed.get("result_sha256"),
            }
    status = stage_result.get("status")
    sources = [stage_result, status] if isinstance(status, dict) else [stage_result]
    for source in sources:
        task = source.get("agent_task")
        error = task.get("error") if isinstance(task, dict) else None
        if isinstance(error, dict):
            error = ": ".join(
                value for key in ("code", "message")
                if isinstance(value := error.get(key), str) and value.strip()
            )
        if not isinstance(error, str) or not error.strip():
            continue
        result = {}
        for key, value in (("stage", stage_result.get("stage")), ("error", error)):
            if not isinstance(value, str) or not value.strip():
                continue
            result[key] = value[: text_limit - 3] + "..." if len(value) > text_limit else value
            if len(value) > text_limit:
                result[f"{key}_details_truncated"] = True
        return result
    return {}


def valid_ci_warnings(warnings: Any) -> bool:
    return (
        isinstance(warnings, list)
        and bool(warnings)
        and all(
            isinstance(warning, dict)
            and all(
                isinstance(warning.get(key), str) and bool(warning[key].strip())
                for key in ("check_key", "name", "reason")
            )
            and warning.get("diagnosis") in ("unrelated", "pre_existing")
            and isinstance(warning.get("evidence"), list)
            and bool(warning["evidence"])
            and all(
                isinstance(evidence, str) and bool(evidence.strip())
                for evidence in warning["evidence"]
            )
            for warning in warnings
        )
    )


def current_ci_warning_verification(verification: Any) -> bool:
    if not isinstance(verification, dict):
        return False
    expected = verification.get("expected_snapshot_sha256")
    return (
        verification.get("result") == "current"
        and verification.get("reason") == "ci_warning_snapshot_current"
        and isinstance(expected, str)
        and re.fullmatch(r"[0-9a-f]{64}", expected) is not None
        and verification.get("observed_snapshot_sha256") == expected
    )


def current_ci_clearance_verification(verification: Any) -> bool:
    if not isinstance(verification, dict):
        return False
    expected = verification.get("expected_snapshot_sha256")
    return (
        verification.get("result") == "current"
        and verification.get("reason") == "ci_snapshot_current"
        and isinstance(expected, str)
        and re.fullmatch(r"[0-9a-f]{64}", expected) is not None
        and verification.get("observed_snapshot_sha256") == expected
    )


def current_description_verification(payload: Any, pipeline_run: str | None) -> bool:
    if not isinstance(payload, dict):
        return False
    verification = payload.get("clearance_verification")
    task = payload.get("agent_task") or {}
    if not isinstance(verification, dict):
        return False
    expected = verification.get("expected_snapshot_sha256")
    return (
        verification.get("result") == "current"
        and verification.get("reason") == "description_snapshot_current"
        and isinstance(expected, str)
        and re.fullmatch(r"[0-9a-f]{64}", expected) is not None
        and verification.get("observed_snapshot_sha256") == expected
        and task.get("status") == "completed"
        and (task.get("task") or {}).get("state") == "completed"
        and (
            pipeline_run is None
            or (
                payload.get("pipeline_run") == pipeline_run
                and task.get("github_mutation_policy") == ACTIVE_GITHUB_MUTATION_POLICY
            )
        )
    )


def ci_warning_fields(stages: list[dict[str, Any]]) -> dict[str, Any]:
    warnings = [
        warning
        for stage in stages
        if stage.get("stage") == STAGE_CI
        and stage.get("clear") is True
        and stage.get("clearance_kind") == "ci_warning"
        for warning in stage.get("ci_warnings", [])
    ]
    return {"ci_warnings": warnings, "all_ci_passed": False} if warnings else {}


def stage_blocker(
    stage_result: dict[str, Any],
    *,
    after_launch: bool,
    conflict_strategy: str = "auto",
) -> tuple[str, str] | None:
    reason = stage_result.get("reason")
    state_path = stage_result.get("status_state")
    state_suffix = (
        f" Expected state path: {state_path}."
        if isinstance(state_path, str) and state_path
        else ""
    )
    if reason in UNAVAILABLE_STATUS_REASONS:
        return (
            "stage_status_unavailable",
            (
                stage_result.get("detail")
                or f"{stage_result['stage']} status could not be read: {reason}"
            )
            + state_suffix,
        )
    if after_launch and reason == "no_state":
        return (
            "stage_did_not_record_state",
            (
                f"{stage_result['stage']} returned without recording a stage state "
                f"at the expected path; clearance cannot be verified.{state_suffix}"
            ),
        )
    status = stage_result.get("status")
    task = status.get("agent_task") if isinstance(status, dict) else None
    task_state = task.get("status") if isinstance(task, dict) else None
    task_id = None
    if isinstance(task, dict):
        task_id = task.get("task_id") or task.get("id")
    task_identity = f" for task {task_id}" if task_id else ""
    if task_state in ACTIVE_TASK_STATES:
        return (
            "stage_still_active",
            (
                f"{stage_result['stage']} still records Agent Task state "
                f"{task_state}{task_identity}; a replacement must not be started"
            ),
        )
    if task_state in RECOVERY_TASK_STATES:
        detail = task.get("error")
        if isinstance(detail, dict):
            code = detail.get("code")
            message = detail.get("message")
            detail = ": ".join(
                str(value) for value in (code, message) if value
            )
        if not isinstance(detail, str) or not detail:
            detail = (
                f"{stage_result['stage']} records Agent Task state "
                f"{task_state}{task_identity}; "
                "the invocation is abandoned"
            )
        return "stage_invocation_abandoned", detail
    coordinator = status.get("coordinator") if isinstance(status, dict) else None
    escalation = status.get("escalation") if isinstance(status, dict) else None
    if (
        isinstance(coordinator, dict)
        and coordinator.get("status") == "blocked"
        and isinstance(escalation, dict)
        and escalation.get("reason") == "coordinator_error"
    ):
        detail = escalation.get("detail") or coordinator.get("detail")
        return (
            "stage_coordinator_error",
            detail
            if isinstance(detail, str) and detail
            else f"{stage_result['stage']} local coordinator failed",
        )
    return None


def inspect_stage(
    entry: dict[str, Any],
    target: dict[str, Any],
    head_sha: str,
    base_sha: str | None = None,
    *,
    pipeline_run: str | None = None,
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
    review_marker = (
        string_at(payload, entry["marker"]) if isinstance(payload, dict) else None
    )
    skip_marker_path = entry.get("skip_marker")
    skip_marker = (
        string_at(payload, skip_marker_path)
        if isinstance(payload, dict) and isinstance(skip_marker_path, tuple)
        else None
    )
    base_marker_path = entry.get("base_marker")
    base_marker = (
        string_at(payload, base_marker_path)
        if isinstance(payload, dict) and isinstance(base_marker_path, tuple)
        else None
    )
    outcome = payload.get("stage_outcome") if isinstance(payload, dict) else None
    source_drift = proven_source_drift(
        payload,
        head_sha=head_sha,
        pipeline_run=pipeline_run,
    )
    warning_verification = (
        payload.get("warning_verification") if isinstance(payload, dict) else None
    )
    marker = skip_marker if outcome == "skipped" and skip_marker_path else review_marker
    if outcome == "skipped" and skip_marker_path:
        base_marker_path = ("policy_skip", "base_sha")
        base_marker = string_at(payload, base_marker_path)
    warning_is_valid = False
    if outcome == "warning" and entry["stage"] == STAGE_CI:
        marker = string_at(payload, ("warning_at_head_sha",))
        base_marker_path = ("warning_at_base_sha",)
        base_marker = string_at(payload, base_marker_path)
        warning_is_valid = (
            "clean_at_head_sha" in payload
            and payload["clean_at_head_sha"] is None
            and payload.get("warning_at_head_sha") == marker
            and payload.get("warning_at_base_sha") == base_marker
            and valid_ci_warnings(payload.get("ci_warnings"))
            and current_ci_warning_verification(warning_verification)
        )
    head_is_clear = marker == head_sha
    base_is_clear = base_marker_path is None or (
        base_sha is not None and base_marker == base_sha
    )
    policy_skip_is_valid = True
    if outcome == "skipped" and skip_marker_path is not None:
        policy_skip = payload.get("policy_skip") if isinstance(payload, dict) else None
        coordinator = payload.get("coordinator") if isinstance(payload, dict) else None
        pr = payload.get("pr") if isinstance(payload, dict) else None
        pipeline_budget = (
            payload.get("pipeline_budget") if isinstance(payload, dict) else None
        )
        expected_policy_skip_keys = {
            "policy",
            "reason",
            "repo_name",
            "number",
            "head_repository",
            "head_branch",
            "head_sha",
            "base_sha",
            "viewer_login",
            "pipeline_run",
            "preflight_sha256",
            "observed_at",
        }
        policy_skip_is_valid = (
            ACTIVE_GITHUB_MUTATION_POLICY == "source-only"
            and isinstance(pipeline_run, str)
            and bool(pipeline_run)
            and isinstance(policy_skip, dict)
            and set(policy_skip) == expected_policy_skip_keys
            and policy_skip.get("policy") == "source-only"
            and policy_skip.get("reason") == SOURCE_ONLY_POLICY_SKIP_REASON
            and policy_skip.get("repo_name") == target["repo_name"]
            and policy_skip.get("number") == target["number"]
            and policy_skip.get("head_sha") == head_sha
            and base_sha is not None
            and policy_skip.get("base_sha") == base_sha
            and isinstance(policy_skip.get("head_repository"), str)
            and bool(policy_skip["head_repository"])
            and isinstance(policy_skip.get("head_branch"), str)
            and bool(policy_skip["head_branch"])
            and isinstance(policy_skip.get("viewer_login"), str)
            and bool(policy_skip["viewer_login"])
            and policy_skip.get("pipeline_run") == pipeline_run
            and isinstance(policy_skip.get("preflight_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", policy_skip["preflight_sha256"])
            is not None
            and isinstance(policy_skip.get("observed_at"), str)
            and bool(policy_skip["observed_at"])
            and isinstance(pr, dict)
            and pr.get("state") == "OPEN"
            and pr.get("repo_name") == target["repo_name"]
            and pr.get("number") == target["number"]
            and pr.get("head_sha") == head_sha
            and pr.get("base_sha") == base_sha
            and pr.get("head_repository")
            == policy_skip["head_repository"]
            and pr.get("head_branch") == policy_skip["head_branch"]
            and pr.get("head_repository")
            == f"{pr.get('head_owner')}/{pr.get('head_repo')}"
            and pr.get("cross_repository")
            == (
                str(pr.get("head_repository")).casefold()
                != str(pr.get("repo_name")).casefold()
            )
            and payload.get("github_mutation_policy") == "source-only"
            and payload.get("last_result") == SOURCE_ONLY_POLICY_SKIP_RESULT
            and payload.get("budget_scope") == "pipeline"
            and isinstance(pipeline_budget, dict)
            and pipeline_budget.get("run") == pipeline_run
            and payload.get("clean_at_head_sha") is None
            and payload.get("queue") is None
            and payload.get("monitoring") is None
            and payload.get("agent_task") is None
            and payload.get("escalation") is None
            and payload.get("terminal_exit") is None
            and payload.get("thread_mutations") is None
            and payload.get("history") == []
            and payload.get("managed_task_history") == []
            and payload.get("local_validation") == []
            and isinstance(coordinator, dict)
            and set(coordinator)
            == {"status", "detail", "head_sha", "observed_at"}
            and coordinator.get("status") == "not_applicable"
            and coordinator.get("detail") == SOURCE_ONLY_POLICY_SKIP_DETAIL
            and coordinator.get("head_sha") == head_sha
            and isinstance(coordinator.get("observed_at"), str)
            and bool(coordinator["observed_at"])
        )
    clear = (
        status.get("ok") is True
        and source_drift is None
        and head_is_clear
        and base_is_clear
        and (outcome in CLEARING_OUTCOMES or warning_is_valid)
        and policy_skip_is_valid
        and (
            entry["stage"] != STAGE_CI
            or warning_is_valid
            or current_ci_clearance_verification(payload.get("clearance_verification"))
        )
        and (
            entry["stage"] != STAGE_DESCRIPTION
            or current_description_verification(payload, pipeline_run)
        )
    )
    if clear and outcome == "warning":
        clear = stage_blocker(
            {"stage": entry["stage"], "status": stage_status_summary(payload)},
            after_launch=False,
        ) is None
    if clear:
        reason = None
    elif not status.get("ok"):
        reason = status.get("reason") or "status_unavailable"
    elif marker and not head_is_clear:
        reason = "clearance_is_for_an_older_head"
    elif (
        base_marker_path is not None
        and base_marker is not None
        and base_marker != base_sha
    ):
        reason = "clearance_is_for_an_older_base"
    elif outcome == "skipped" and not policy_skip_is_valid:
        reason = "policy_skip_not_verified"
    elif outcome == "warning":
        reason = "ci_warning_not_verified"
    elif entry["stage"] == STAGE_CI and outcome in CLEARING_OUTCOMES:
        reason = "ci_clearance_not_verified"
    elif entry["stage"] == STAGE_DESCRIPTION and outcome == "cleared":
        reason = "description_clearance_not_verified"
    elif (
        entry["stage"] == STAGE_CI
        and isinstance(warning_verification, dict)
        and warning_verification.get("result") == "stale"
    ):
        reason = "ci_warning_snapshot_changed"
    elif source_drift is not None:
        reason = "source_drift"
    else:
        reason = status.get("reason") or outcome or "not_cleared"
    return {
        "stage": entry["stage"],
        "clear": clear,
        "clear_at_head_sha": marker,
        "clear_at_base_sha": base_marker,
        "clearance_kind": (
            "ci_warning"
            if clear and outcome == "warning"
            else "policy_skip"
            if clear and outcome == "skipped" and skip_marker_path is not None
            else "stage_result" if clear else None
        ),
        "outcome": outcome,
        "reason": reason,
        "installed": status["installed"],
        "status_state": status["state"],
        "status": stage_status_summary(payload),
        **({"source_drift": source_drift} if source_drift is not None else {}),
        **(
            {"warning_verification": warning_verification}
            if entry["stage"] == STAGE_CI and isinstance(warning_verification, dict)
            else {}
        ),
        **(
            {"ci_warnings": payload["ci_warnings"], "all_ci_passed": False}
            if clear and outcome == "warning"
            else {}
        ),
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


def validate_stage_route(entry: dict[str, Any], model: str, effort: str) -> None:
    required_model = entry.get("required_model")
    if required_model and model != required_model:
        raise WorkflowError(
            f"{entry['stage']} requires exactly model {required_model}, not {model}"
        )
    if model not in COORDINATOR_MODEL_ARGUMENTS:
        raise WorkflowError(
            f"{entry['stage']} does not support model {model}; "
            f"choose one of {', '.join(COORDINATOR_MODEL_ARGUMENTS)}"
        )
    required_effort = entry.get("required_effort")
    if required_effort and effort != required_effort:
        raise WorkflowError(
            f"{entry['stage']} requires exactly reasoning effort "
            f"{required_effort}, not {effort}"
        )


def stage_models(
    overrides: list[str] | None, effort: str = DEFAULT_EFFORT
) -> dict[str, str]:
    """Resolve stage models and enforce stages with fixed model routes."""
    models = {entry["stage"]: entry["model"] for entry in STAGES}
    for assignment in overrides or []:
        stage, separator, model = assignment.partition("=")
        if not separator or stage not in STAGE_BY_NAME or not model.strip():
            raise WorkflowError(
                f"--stage-model expects <stage>=<model> for a known stage: {assignment}"
            )
        models[stage] = model.strip()
    for entry in STAGES:
        validate_stage_route(entry, models[entry["stage"]], effort)
    return models


def stage_accepts_pipeline_position(
    entry: dict[str, Any],
    *,
    script_for: Callable[[dict[str, Any]], Path] = stage_script_path,
) -> bool:
    if entry.get("pipeline_position") is False:
        return False
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
    """Bind every stage to this run without changing its iteration allowance."""
    return [
        PIPELINE_RUN_FLAG,
        run_id,
        PIPELINE_ITERATION_FLAG,
        str(iteration),
        PIPELINE_MAX_ITERATIONS_FLAG,
        str(max_iterations),
    ]


ACTIVE_GITHUB_MUTATION_POLICY = "allow"


def stage_prompt(target: dict[str, Any], arguments: list[str]) -> str:
    name = f"{target['repo_name']}#{target['number']}"
    if not arguments:
        return name
    pairs = zip(arguments[::2], arguments[1::2])
    position = " ".join(f"{flag.lstrip('-')}: {value}" for flag, value in pairs)
    return (
        f"{name}\n\n{position}\n\n"
        "Pass these arguments to the helper command that owns this stage run, "
        f"exactly as written: {' '.join(arguments)}\n"
        "This pipeline position replaces standalone invocation scope. Do not pass "
        "--new-invocation or --invocation-run with it."
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
    repo_root: Path | None = None,
) -> list[str]:
    validate_stage_route(entry, model, effort)
    stage_arguments = list(arguments)
    if entry.get("github_mutation_policy") is True:
        stage_arguments.extend(
            ["--github-mutation-policy", ACTIVE_GITHUB_MUTATION_POLICY]
        )
    if repo_root is not None:
        stage_arguments.extend(["--repo-root", str(repo_root)])
    return [
        sys.executable,
        str(stage_script_path(entry)),
        "pipeline",
        f"{target['repo_name']}#{target['number']}",
        "--model",
        COORDINATOR_MODEL_ARGUMENTS[model],
        *stage_arguments,
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
            process = (_EXECUTION.run if _EXECUTION else subprocess.run)(
                command, check=False, **launch_options(log, cwd),
                **({"require_execution": True} if _EXECUTION is not None else {}),
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
    if _EXECUTION is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("wb") as log:
            return _EXECUTION.start(
                command, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
                require_execution=True,
            )
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
        process, used_breakaway = popen_with_windows_breakaway_fallback(
            command,
            options,
            operation="background stage worker",
        )
        receipt = {
            "creationflags": options.get("creationflags", 0) & (
                ~getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
                if not used_breakaway else -1
            ),
            "breakaway_requested": IS_WINDOWS,
            "breakaway_accepted": bool(IS_WINDOWS and used_breakaway),
            "fallback_used": IS_WINDOWS and not used_breakaway,
            "process_identity": None,
        }
        owner = None
        try:
            if IS_WINDOWS:
                try:
                    owner = create_windows_kill_job(process.pid)
                except OSError as error:
                    if used_breakaway or getattr(error, "winerror", None) != 5:
                        raise
            if IS_WINDOWS:
                try:
                    receipt["process_identity"] = windows_process_identity(process.pid)
                except OSError as error:
                    receipt["process_identity_error"] = str(error)
                receipt["scheduler_owned_job"] = owner is not None
                resume_windows_process(process.pid)
        except BaseException:
            if owner is not None:
                owner.close()
            else:
                process.terminate()
            process.wait()
            raise
        return OwnedProcess(process, owner, receipt)
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
            if _EXECUTION is not None:
                _EXECUTION.check_cancel()
            sleep(interval)
            progress()
    except BaseException:
        if process.poll() is None:
            terminate_process_tree(process)
        raise
    result = {
        "returncode": process.wait(),
        "log_path": str(log_path),
        "started_at": started_at,
        "ended_at": utc_now(),
        "launch_receipt": process.launch_receipt if isinstance(process, OwnedProcess) else None,
    }
    terminal = getattr(process, "terminal_result", None)
    if isinstance(terminal, dict):
        result["child_terminal_result"] = {
            key: terminal[key]
            for key in (
                "run_id",
                "result_file",
                "result_sha256",
                "exit_code",
                "local_status",
                "error",
                "workflow_result",
                "finalization_errors",
            )
            if key in terminal
        }
    return result


def windows_process_identity(pid: int) -> dict[str, Any] | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:
            return None
        raise OSError(error, f"cannot identify process {pid}")
    try:
        creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                        ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise OSError(ctypes.get_last_error(), f"cannot read process generation {pid}")
        wait = kernel32.WaitForSingleObject(handle, 0)
        if wait not in (0, 258):
            raise OSError(ctypes.get_last_error(), f"cannot query process state {pid}")
        in_job = wintypes.BOOL()
        job_known = kernel32.IsProcessInJob(handle, None, ctypes.byref(in_job))
        return {
            "pid": pid,
            "creation_time": str((creation.dwHighDateTime << 32) | creation.dwLowDateTime),
            "running": wait == 258,
            "in_job": bool(in_job.value) if job_known else None,
            "job_query_error": None if job_known else ctypes.get_last_error(),
        }
    finally:
        kernel32.CloseHandle(handle)


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
    if _EXECUTION is not None and payload.get("run_id") == _EXECUTION.run_id:
        _EXECUTION.record_state(path, payload)
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
