"""Controller-owned foreground execution and optional file-based controls."""

from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable
import uuid


SCHEMA = "github.copilot.foreground-execution.v1"
PARENT_ENV = "TRASK_EXECUTION_PARENT"
SESSION_ENV = "COPILOT_AGENT_SESSION_ID"
IS_WINDOWS = os.name == "nt"
FORCED_DRAINAGE_ERROR = "owned Windows job required forced drainage"
PRESENTATION_INLINE_MAX_BYTES = 4096
REMOTE_TERMINAL_STATES = frozenset(
    {"completed", "failed", "timed_out", "cancelled", "waiting_for_user", "idle"}
)
REMOTE_ACTIVE_STATES = frozenset({"queued", "in_progress"})


class ExecutionError(RuntimeError):
    pass


class _OwnershipPending(ExecutionError):
    pass


class Cancelled(BaseException):
    pass


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _same_path(left: Path | str, right: Path | str) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _canonical_directory(path: Path, description: str) -> Path:
    absolute = Path(os.path.abspath(path.expanduser()))
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as failure:
        raise ExecutionError(f"{description} is unavailable: {absolute}") from failure
    if not resolved.is_dir() or not _same_path(absolute, resolved):
        raise ExecutionError(f"{description} must be a canonical directory: {absolute}")
    current = absolute
    while True:
        if current.is_symlink():
            raise ExecutionError(f"{description} must not contain symlinks: {absolute}")
        if current == current.parent:
            break
        current = current.parent
    return resolved


def _session_files() -> tuple[str, Path]:
    if SESSION_ENV not in os.environ:
        raise ExecutionError(f"{SESSION_ENV} is required for execution controls")
    session_text = os.environ[SESSION_ENV]
    try:
        session = uuid.UUID(session_text)
    except (AttributeError, ValueError) as failure:
        raise ExecutionError(f"{SESSION_ENV} must be a canonical UUID") from failure
    if session_text != str(session):
        raise ExecutionError(f"{SESSION_ENV} must be a canonical UUID")
    copilot_home = Path.home() / ".copilot"
    files = _canonical_directory(
        copilot_home / "session-state" / session_text / "files",
        "agent session files directory",
    )
    return session_text, files


def _helper_identity() -> dict[str, str]:
    source = Path(sys.argv[0])
    absolute = Path(os.path.abspath(source))
    try:
        helper = absolute.resolve(strict=True)
    except OSError as failure:
        raise ExecutionError(f"custom-agent helper is unavailable: {absolute}") from failure
    if (
        not helper.is_file()
        or absolute.is_symlink()
        or not _same_path(absolute, helper)
    ):
        raise ExecutionError(f"custom-agent helper must be a canonical regular file: {absolute}")
    return {
        "path": str(helper),
        "sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
    }


def _owned_prefix(helper: dict[str, str]) -> str:
    return f"execution-{digest(helper)[:16]}-"


def _validate_new_route(
    handle: Path, command: list[str], route: dict[str, Any]
) -> None:
    if len(command) < 3 or not all(isinstance(argument, str) for argument in command):
        raise ExecutionError("custom-agent command identity is invalid")
    session_id, files = _session_files()
    helper = _helper_identity()
    prefix = _owned_prefix(helper)
    if (
        route != {
            "session_id": session_id,
            "session_files": str(files),
            "helper": helper,
            "command": command[2],
            "command_sha256": digest(command),
        }
        or handle.parent != files
        or not handle.name.startswith(prefix)
        or not handle.name.endswith(".json")
        or handle.exists()
        or handle.with_name(handle.name + ".d").exists()
    ):
        raise ExecutionError("derived execution route is invalid or not fresh")
    generation = handle.name[len(prefix):-5]
    try:
        if uuid.UUID(hex=generation).hex != generation:
            raise ValueError
    except ValueError as failure:
        raise ExecutionError("derived execution generation is invalid") from failure


def _owned_route(command: list[str]) -> tuple[Path, dict[str, Any]]:
    session_id, files = _session_files()
    helper = _helper_identity()
    if (
        len(command) < 3
        or not all(isinstance(argument, str) for argument in command)
        or not _same_path(command[1], helper["path"])
    ):
        raise ExecutionError("custom-agent command identity is invalid")
    route = {
        "session_id": session_id,
        "session_files": str(files),
        "helper": helper,
        "command": command[2],
        "command_sha256": digest(command),
    }
    for _ in range(16):
        handle = files / f"{_owned_prefix(helper)}{uuid.uuid4().hex}.json"
        if not handle.exists() and not handle.with_name(handle.name + ".d").exists():
            return handle, route
    raise ExecutionError("could not derive a fresh execution handle")


def read(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ExecutionError(f"execution artifact is a symlink: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ExecutionError(f"execution artifact is not an object: {path}")
    return value


def write(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if exclusive:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return
    if path.is_symlink():
        raise ExecutionError(f"execution artifact is a symlink: {path}")
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def render_presentation(result: dict[str, Any]) -> str:
    title = result.get("session_title")
    if not isinstance(title, str) or not title.strip():
        title = "Workflow result"
    title = " ".join(title.split())[:240]
    outcome = str(result.get("result", result.get("status", "unknown"))).replace(
        "`", "'"
    )
    details = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    return "\n".join(
        [
            f"# {title}",
            "",
            f"Result: `{outcome}`",
            "",
            "Verified result:",
            "",
            *(f"    {line}" for line in details.splitlines()),
            "",
        ]
    )


def write_presentation(context: "Execution") -> dict[str, Any] | None:
    if context.root != context.handle or not isinstance(context.last_result, dict):
        return None
    text = render_presentation(context.last_result)
    data = text.encode("utf-8")
    presentation = {
        "media_type": "text/markdown",
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if len(data) <= PRESENTATION_INLINE_MAX_BYTES:
        return {**presentation, "text": text}
    path = context.directory / "presentation.md"
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return {**presentation, "path": str(path)}


@contextmanager
def guard(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if IS_WINDOWS:
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


def _windows_process_running(kernel: Any, handle: Any) -> bool:
    import ctypes

    state = kernel.WaitForSingleObject(handle, 0)
    if state == 0:
        return False
    if state == 258:
        return True
    if state == 0xFFFFFFFF:
        raise ctypes.WinError(ctypes.get_last_error())
    raise ExecutionError(f"unexpected Windows process wait state: {state}")


def _windows_process_observation(
    kernel: Any, handle: Any, pid: int,
    expected: dict[str, Any] | None = None,
    retry_image: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    import ctypes
    from ctypes import wintypes

    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(
        handle, *(ctypes.byref(value) for value in times)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    creation_time = str(
        (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    )
    if expected is not None and (
        expected["pid"] != pid or expected["creation_time"] != creation_time
    ):
        raise ExecutionError("Windows process generation changed")
    running = _windows_process_running(kernel, handle)
    if not running:
        return {
            "pid": pid,
            "creation_time": creation_time,
            "image": expected["image"] if expected is not None else None,
            "running": False,
            "image_provenance": (
                "cached_binding"
                if expected is not None else
                "unavailable_after_exit"
            ),
        }
    while True:
        image = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(image))
        if kernel.QueryFullProcessImageNameW(
            handle, 0, image, ctypes.byref(size)
        ):
            break
        image_failure = ctypes.WinError(ctypes.get_last_error())
        try:
            running = _windows_process_running(kernel, handle)
        except (OSError, ExecutionError) as state_failure:
            raise ExecutionError(
                f"{image_failure}; process state query failed: {state_failure}"
            ) from image_failure
        if running:
            if (
                getattr(image_failure, "winerror", None) == 5
                and retry_image is not None
                and retry_image()
            ):
                continue
            raise image_failure
        return {
            "pid": pid,
            "creation_time": creation_time,
            "image": None,
            "running": False,
            "image_provenance": "unavailable_after_exit",
        }
    running = _windows_process_running(kernel, handle)
    observed = {
        "pid": pid,
        "creation_time": creation_time,
        "image": os.path.normcase(image.value),
        "running": running,
        "image_provenance": "queried_live",
    }
    if expected is not None and not same_process(expected, observed):
        raise ExecutionError("Windows process identity changed")
    return observed


def process_identity(pid: int) -> dict[str, Any] | None:
    if pid <= 0:
        return None
    if not IS_WINDOWS:
        # Linux procfs supplies a generation without invoking a console utility.
        root = Path("/proc") / str(pid)
        try:
            fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
            return {
                "pid": pid, "creation_time": fields[19],
                "image": str((root / "exe").resolve(strict=True)),
                "running": fields[0] != "Z",
            }
        except FileNotFoundError:
            if Path("/proc").is_dir():
                return None
            raise ExecutionError("this host has no supported process-generation provider")
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00101000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:
            return None
        raise ctypes.WinError(error)
    try:
        observed = _windows_process_observation(kernel, handle, pid)
        if observed["image_provenance"] == "queried_live":
            observed.pop("image_provenance")
        return observed
    finally:
        kernel.CloseHandle(handle)


def same_process(expected: dict[str, Any], observed: dict[str, Any] | None) -> bool:
    if (
        not isinstance(expected, dict) or not isinstance(observed, dict)
        or type(expected.get("pid")) is not int or expected["pid"] <= 0
        or not isinstance(expected.get("creation_time"), str) or not expected["creation_time"]
        or not isinstance(expected.get("image"), str) or not expected["image"]
    ):
        return False
    return all(
        expected.get(key) == observed.get(key) for key in ("pid", "creation_time", "image")
    )


def require_owner(expected: dict[str, Any]) -> None:
    observed = process_identity(expected["pid"])
    if not same_process(expected, observed) or not observed["running"]:
        raise ExecutionError("execution owner generation is absent or changed")


class WindowsOwner:
    """Assign a suspended child to a kill-on-close job, without breakaway."""

    def __init__(self, process: subprocess.Popen[Any]) -> None:
        import ctypes
        from ctypes import wintypes

        class Limits(ctypes.Structure):
            _fields_ = [
                ("per_process", ctypes.c_int64), ("per_job", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t),
                ("maximum", ctypes.c_size_t), ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("basic", Limits), ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
        ]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.IsProcessInJob.argtypes = [
            wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL),
        ]
        kernel.IsProcessInJob.restype = wintypes.BOOL
        kernel.GetProcessTimes.argtypes = [
            wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4,
        ]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._binding_token = object()
        try:
            limits = Extended()
            limits.basic.flags = 0x2000
            if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def terminate(self) -> None:
        if not self.kernel.TerminateJobObject(self.handle, 1):
            import ctypes
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            if not self.kernel.CloseHandle(self.handle):
                import ctypes
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None
            self._binding_token = None

    def active_count(self) -> int:
        import ctypes
        from ctypes import wintypes

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("user", ctypes.c_int64), ("kernel", ctypes.c_int64),
                ("period_user", ctypes.c_int64), ("period_kernel", ctypes.c_int64),
                ("page_faults", wintypes.DWORD), ("total", wintypes.DWORD),
                ("active", wintypes.DWORD), ("terminated", wintypes.DWORD),
            ]

        accounting = Accounting()
        if not self.kernel.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return accounting.active

    def process_ids(
        self, deadline: float, check: Callable[[], None] | None = None
    ) -> tuple[int, ...]:
        import ctypes
        from ctypes import wintypes

        capacity = 8
        while True:
            if check is not None:
                check()
            class ProcessIds(ctypes.Structure):
                _fields_ = [
                    ("assigned", wintypes.DWORD),
                    ("listed", wintypes.DWORD),
                    ("ids", ctypes.c_size_t * capacity),
                ]

            processes = ProcessIds()
            complete = self.kernel.QueryInformationJobObject(
                self.handle, 3, ctypes.byref(processes), ctypes.sizeof(processes), None,
            )
            if complete and processes.listed == processes.assigned:
                return tuple(sorted(processes.ids[index] for index in range(processes.listed)))
            error = ctypes.get_last_error()
            if not complete and error != 234:
                raise ctypes.WinError(error)
            if time.monotonic() >= deadline:
                raise _OwnershipPending("owned Windows job process list did not stabilize")
            capacity = max(capacity * 2, processes.assigned, processes.listed + 1)

    def open_process(self, pid: int):
        import ctypes

        handle = self.kernel.OpenProcess(0x00101000, False, pid)
        if handle:
            return handle
        error = ctypes.get_last_error()
        if error == 87:
            return None
        raise ctypes.WinError(error)

    def process(
        self, handle, pid: int, binding: dict[str, Any] | None = None,
        *, retry_image: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        import ctypes
        from ctypes import wintypes

        expected = None
        if binding is None:
            member = wintypes.BOOL()
            if not self.kernel.IsProcessInJob(handle, self.handle, ctypes.byref(member)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not member.value:
                raise ExecutionError(
                    "observed process handle is not a member of the owned Windows job"
                )
            job_provenance = "verified_handle"
        else:
            expected = binding.get("identity")
            binding_token = getattr(self, "_binding_token", None)
            if (
                self.handle is None
                or binding_token is None
                or binding.get("owner_token") is not binding_token
                or binding.get("job_handle") != self.handle
                or binding.get("handle") is not handle
                or not same_process(expected, expected)
            ):
                raise ExecutionError("owned Windows process binding is invalid or changed")
            job_provenance = "cached_binding"
        observed = _windows_process_observation(
            self.kernel, handle, pid, expected, retry_image
        )
        observed["job_provenance"] = job_provenance
        return observed

    def bind_process(self, handle, pid: int) -> dict[str, Any]:
        observed = self.process(handle, pid)
        if not observed["running"] or observed["image_provenance"] != "queried_live":
            raise ExecutionError(
                "owned Windows process exited before live identity binding"
            )
        return {
            "handle": handle,
            "owner_token": self._binding_token,
            "job_handle": self.handle,
            "identity": {
                key: observed[key]
                for key in ("pid", "creation_time", "image", "running")
            },
            "job_provenance": "verified_live_handle",
            "image_provenance": "queried_live",
        }

    def processes(
        self, deadline: float, retained: dict[int, dict[str, Any]],
        check: Callable[[], None] | None = None,
    ) -> list[dict[str, Any]]:
        def snapshot() -> tuple[int, ...]:
            return (
                self.process_ids(deadline)
                if check is None else self.process_ids(deadline, check)
            )

        while True:
            if check is not None:
                check()
            pids = snapshot()

            def retry_image() -> bool:
                if snapshot() != pids:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(0.02, remaining))
                return True

            observed = []
            opened = []
            failure: BaseException | None = None
            try:
                for pid in pids:
                    binding = retained.get(pid)
                    if binding is None:
                        try:
                            handle = self.open_process(pid)
                        except OSError as inspection:
                            failure = inspection
                            break
                        if handle is None:
                            failure = ExecutionError(
                                "owned Windows job process identity is unavailable"
                            )
                            break
                        opened.append(handle)
                    else:
                        handle = binding["handle"]
                    try:
                        observed.append(
                            self.process(
                                handle, pid, binding, retry_image=retry_image
                            )
                        )
                    except (OSError, ExecutionError) as inspection:
                        failure = inspection
                        break
                confirmed = snapshot()
            finally:
                for handle in opened:
                    if not self.kernel.CloseHandle(handle):
                        import ctypes
                        raise ctypes.WinError(ctypes.get_last_error())
            if confirmed != pids:
                if time.monotonic() >= deadline:
                    raise _OwnershipPending("owned Windows job membership did not stabilize")
                continue
            if failure is not None:
                raise failure
            return observed

    def drain(
        self, deadline: float, check: Callable[[], None] | None = None
    ) -> None:
        while True:
            if check is not None:
                check()
            if not self.active_count():
                return
            if time.monotonic() >= deadline:
                raise _OwnershipPending("owned Windows job still has active processes")
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


def resume_process(pid: int) -> None:
    import ctypes
    from ctypes import wintypes

    class Thread(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD), ("usage", wintypes.DWORD),
            ("id", wintypes.DWORD), ("pid", wintypes.DWORD),
            ("base", wintypes.LONG), ("delta", wintypes.LONG), ("flags", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Thread32First", "Thread32Next"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(Thread)]
        function.restype = wintypes.BOOL
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    count = 0
    try:
        thread = Thread()
        thread.size = ctypes.sizeof(thread)
        more = kernel.Thread32First(snapshot, ctypes.byref(thread))
        while more:
            if thread.pid == pid:
                handle = kernel.OpenThread(2, False, thread.id)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if kernel.ResumeThread(handle) == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                    count += 1
                finally:
                    kernel.CloseHandle(handle)
            more = kernel.Thread32Next(snapshot, ctypes.byref(thread))
    finally:
        kernel.CloseHandle(snapshot)
    if not count:
        raise ExecutionError("suspended child has no verifiable initial thread")


class OwnedProcess:
    def __init__(
        self, process: subprocess.Popen[Any], owner: WindowsOwner | None,
        record: Path, streams: list[Any],
        process_binding: dict[str, Any] | None = None,
    ) -> None:
        self.process, self.owner, self.record, self.streams = process, owner, record, streams
        self.pid = process.pid
        self.launch_receipt = read(record)
        if owner is not None:
            if (
                process_binding is None
                or process_binding.get("handle") is not process._handle
                or not same_process(
                    self.launch_receipt.get("process_identity", {}),
                    process_binding.get("identity"),
                )
            ):
                raise ExecutionError("owned Windows process binding is absent or changed")
        elif process_binding is not None:
            raise ExecutionError("process binding cannot exist without a Windows owner")
        self.process_binding = process_binding
        self.exit_code: int | None = None
        self.drained = False
        self.drainage_error: str | None = None
        self.completion_error: str | None = None
        self.process_handle_closed = False
        self.stopping = False
        self.observation_complete = False
        self.owner_terminated = False
        self.observed_descendants: list[dict[str, Any]] = []

    @property
    def returncode(self):
        return self.exit_code if self.exit_code is not None else self.process.returncode

    def poll(self):
        if self.exit_code is None:
            code = self.process.poll()
            if code is None:
                return None
            self.exit_code = code
        complete = self._complete(time.monotonic(), blocking=False)
        if complete and self.completion_error is not None:
            raise ExecutionError(self.completion_error)
        if complete:
            self.verify_execution(self.exit_code)
        return self.exit_code if complete else None

    def terminate(self):
        return self.terminate_tree()

    def kill(self):
        return self.terminate_tree()

    def verify_execution(self, code: int) -> None:
        if self.launch_receipt.get("requires_execution_result"):
            handle = Path(self.launch_receipt["handle"])
            if not handle.is_file():
                receipt = read(self.record)
                failure = {
                    "result": "execution_bootstrap_failed",
                    "child_record": str(self.record),
                    "handle": str(handle),
                    "exit_code": code,
                    "command_sha256": receipt.get("command_sha256"),
                    "captured_output": receipt.get("captured_output", {}),
                }
                write(self.record, {
                    **receipt,
                    "lifecycle": "bootstrap_failed",
                    "bootstrap_failed_at": time.time(),
                    "bootstrap_failure": failure,
                })
                self.launch_receipt = read(self.record)
                raise ExecutionError(
                    "required child execution handle was not created"
                )
            result = status(handle)
            if (
                result.get("terminal") is not True or result.get("exit_code") != code
                or (
                    code == 0
                    and result.get("local_status") != "finished"
                )
                or result.get("local_children_drained") is not True
                or result.get("run_id") != self.launch_receipt["run_id"]
                or not same_process(result.get("owner", {}), self.launch_receipt["process_identity"])
            ):
                raise ExecutionError("child exit lacks a matching sealed execution result")

    def close_process_handle(self) -> None:
        if not self.process_handle_closed:
            self.process._handle.Close()
            self.process_handle_closed = True

    def wait(self, timeout=None):
        return self._wait(None if timeout is None else time.monotonic() + timeout)

    def _wait(
        self, deadline: float | None, check: Callable[[], None] | None = None
    ):
        if self.exit_code is None:
            self.exit_code = self.process.wait(
                timeout=None if deadline is None else max(0.0, deadline - time.monotonic())
            )
        if check is not None:
            check()
        code = self.exit_code
        if deadline is None:
            deadline = time.monotonic() + 10.0
        self._complete(deadline, blocking=True, check=check)
        if self.completion_error is not None:
            raise ExecutionError(self.completion_error)
        self.verify_execution(code)
        return code

    def _complete(
        self, deadline: float, *, blocking: bool,
        check: Callable[[], None] | None = None,
    ) -> bool:
        code = self.exit_code
        if code is None:
            return False
        if self.drainage_error is not None:
            raise ExecutionError(self.drainage_error)
        if self.drained:
            return True
        release_resources = False
        try:
            if self.owner:
                if not self.observation_complete:
                    handle = self.process._handle
                    direct = self.owner.process(
                        handle, self.pid, self.process_binding
                    )
                    if (
                        not same_process(self.launch_receipt["process_identity"], direct)
                        or direct["running"]
                    ):
                        raise ExecutionError(
                            "owned child generation or exit state changed before job drainage"
                        )
                    processes = (
                        self.owner.processes(
                            deadline, {self.pid: self.process_binding}
                        )
                        if check is None else
                        self.owner.processes(
                            deadline, {self.pid: self.process_binding}, check
                        )
                    )
                    self.observed_descendants = [
                        item for item in processes
                        if item["pid"] != self.pid and item["running"]
                    ]
                    self.observation_complete = True
                    self.close_process_handle()
                if blocking:
                    if check is None:
                        self.owner.drain(deadline)
                    else:
                        self.owner.drain(deadline, check)
                elif self.owner.active_count():
                    self._write_pending(code)
                    return False
            elif not IS_WINDOWS:
                try:
                    os.killpg(self.pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise ExecutionError("owned process group still has active descendants")
            self.drained = True
            release_resources = True
            result = {
                **read(self.record),
                "lifecycle": "drained",
                "drained_at": time.time(),
                "exit_code": code,
                "local_drained": True,
            }
            if self.observed_descendants:
                result["observed_descendants"] = self.observed_descendants
            if self.completion_error is not None:
                result["completion_error"] = self.completion_error
            write(self.record, result)
        except _OwnershipPending as pending:
            self._write_pending(code)
            if blocking:
                raise subprocess.TimeoutExpired(self.process.args, 0) from pending
            return False
        except (OSError, subprocess.SubprocessError, ExecutionError) as failure:
            self.drained = False
            release_resources = True
            message = str(failure)
            if self.completion_error is not None:
                message = f"{self.completion_error}; local drainage: {message}"
            self.drainage_error = message
            write(
                self.record,
                {
                    **read(self.record),
                    "lifecycle": "drain_failed",
                    "drain_failed_at": time.time(),
                    "exit_code": code,
                    "local_drained": False,
                    "drainage_error": message,
                    **({"completion_error": self.completion_error}
                       if self.completion_error is not None else {}),
                    **({"observed_descendants": self.observed_descendants}
                       if self.observed_descendants else {}),
                },
            )
            raise ExecutionError(message) from failure
        finally:
            if release_resources and self.owner:
                if self.exit_code is not None:
                    self.close_process_handle()
                self.owner.close()
                self.owner = None
            if release_resources:
                for stream in self.streams:
                    stream.close()
                self.streams.clear()
        return True

    def _write_pending(self, code: int) -> None:
        result = {
            **read(self.record),
            "lifecycle": "draining",
            "exit_code": code,
            "local_drained": False,
        }
        if self.observed_descendants:
            result["observed_descendants"] = self.observed_descendants
        if self.completion_error is not None:
            result["completion_error"] = self.completion_error
        write(self.record, result)

    def terminate_tree(self, timeout=10.0):
        if self.drained:
            return self._wait(time.monotonic())
        deadline = time.monotonic() + timeout
        self.stopping = True
        if self.owner:
            if not self.owner_terminated:
                if self.completion_error is None:
                    self.completion_error = FORCED_DRAINAGE_ERROR
                self.owner.terminate()
                self.owner_terminated = True
        elif not IS_WINDOWS:
            require_owner(self.launch_receipt["process_identity"])
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif self.poll() is None:
            self.process.terminate()
        try:
            return self._wait(deadline)
        except subprocess.TimeoutExpired:
            if not IS_WINDOWS:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif self.owner is None:
                self.process.kill()
            return self._wait(deadline)


class Execution:
    def __init__(self, handle: Path, *, command: list[str], parent: Path | None = None,
                 run_id: str | None = None, terminal_results: frozenset[str] = frozenset(),
                 route: dict[str, Any] | None = None) -> None:
        if not handle.is_absolute() or handle.is_symlink():
            raise ExecutionError("execution handle must be a fresh absolute regular path")
        if route is not None:
            _validate_new_route(handle, command, route)
        for index, argument in enumerate(command):
            target = None
            if argument == "--repo-root" and index + 1 < len(command):
                target = command[index + 1]
            elif argument.startswith("--repo-root="):
                target = argument.partition("=")[2]
            if target and handle.resolve().is_relative_to(Path(target).expanduser().resolve()):
                raise ExecutionError("execution artifacts must be outside the target repository")
        for directory in (Path.cwd(), *Path.cwd().parents):
            if (directory / ".git").exists() and handle.resolve().is_relative_to(directory.resolve()):
                raise ExecutionError("execution artifacts must be outside the target repository")
        self.handle = handle.resolve()
        self.directory = self.handle.with_name(self.handle.name + ".d")
        self.directory.mkdir(parents=True, exist_ok=False)
        self.children: list[OwnedProcess] = []
        self.launch_failures: list[dict[str, Any]] = []
        self.last_result: dict[str, Any] | None = None
        self.run_id = run_id or uuid.uuid4().hex
        self.terminal_results = terminal_results
        self.root = self.handle
        self.parent = None
        self.owner = process_identity(os.getpid())
        if self.owner is None:
            raise ExecutionError("controller generation could not be read")
        if parent is not None:
            request = read(parent)
            if request.get("schema") != SCHEMA or request.get("handle") != str(self.handle):
                raise ExecutionError("parent request does not bind this child handle")
            root = load_handle(Path(request["root"]))
            require_owner(root["owner"])
            self.parent = load_handle(Path(request["parent"]))
            if self.parent["root"] != str(request["root"]) or self.parent["run_id"] != root["run_id"]:
                raise ExecutionError("parent execution identity changed")
            require_owner(self.parent["owner"])
            if request["command_sha256"] != digest(command):
                raise ExecutionError("child command does not match its parent request")
            child_record = Path(request["child_record"])
            deadline = time.monotonic() + 10
            while not child_record.exists():
                require_owner(root["owner"])
                if time.monotonic() >= deadline:
                    raise ExecutionError("parent did not bind child generation")
                time.sleep(0.02)
            child = read(child_record)
            while (
                child.get("lifecycle") == "admitted"
                and child.get("process_identity") is None
            ):
                require_owner(root["owner"])
                if time.monotonic() >= deadline:
                    raise ExecutionError("parent did not bind child generation")
                time.sleep(0.02)
                child = read(child_record)
            if (
                child.get("schema") != SCHEMA or child.get("root") != request["root"]
                or child.get("run_id") != root["run_id"] or child.get("handle") != str(self.handle)
                or child.get("command_sha256") != request["command_sha256"]
                or child.get("lifecycle") != "bound"
            ):
                raise ExecutionError("parent child record identity changed")
            expected = child["process_identity"]
            if not same_process(expected, self.owner):
                raise ExecutionError("child process generation does not match its parent request")
            self.root = Path(request["root"])
            self.run_id = root["run_id"]
        self.record = {
            "schema": SCHEMA, "run_id": self.run_id, "handle": str(self.handle),
            "root": str(self.root), "parent_request": str(parent) if parent else None,
            "owner": self.owner, "mode": "foreground", "command_sha256": digest(command),
            "status": "starting", "started_at": time.time(),
            "result": str(self.directory / "result.json"),
            "stdout": str(self.directory / "stdout.log"),
            "stderr": str(self.directory / "stderr.log"),
            "progress": str(self.directory / "progress.jsonl"),
            "cancel": str(self.directory / "cancel.json"),
        }
        if route is not None:
            self.record["route"] = route
        write(self.handle, self.record, exclusive=True)

    def check_cancel(self) -> None:
        root = load_handle(self.root)
        if self.root != self.handle:
            require_owner(root["owner"])
        if self.parent is not None:
            require_owner(self.parent["owner"])
        cancel = Path(root["cancel"])
        if cancel.exists():
            request = read(cancel)
            if request.get("run_id") != self.run_id or request.get("owner") != root["owner"]:
                raise ExecutionError("cancellation identity does not match the owner")
            raise Cancelled("explicit local cancellation")

    def ready(self) -> None:
        self.check_cancel()
        self.record["status"] = "ready"
        write(self.handle, self.record)

    def emit(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise ExecutionError("controller event must be a JSON object")
        with Path(self.record["progress"]).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.last_result = payload

    def child_status(self, path: Path) -> dict[str, Any]:
        value = load_handle(path)
        if value["root"] != str(self.root) or value["run_id"] != self.run_id:
            raise ExecutionError("child readiness belongs to another execution")
        return status(path)

    def record_state(self, path: Path, state: dict[str, Any]) -> None:
        paths = self.record.setdefault("domain_states", [])
        if str(path.resolve()) not in paths:
            paths.append(str(path.resolve()))
            write(self.handle, self.record)

    def record_dispatch(self, result_path: Path, request_id: str, repository: str,
                        task: dict[str, Any] | None = None) -> None:
        path = result_path.with_name(result_path.name + ".dispatch.json")
        self.record_state(path, {})
        state = task.get("state") if isinstance(task, dict) else None
        remote_status = (
            "creating"
            if task is None else
            "terminal"
            if state in REMOTE_TERMINAL_STATES else
            "active"
            if state in REMOTE_ACTIVE_STATES else
            "unconfirmed"
        )
        event = {
            "observed_at": time.time(),
            "remote_status": remote_status,
            "task": task,
        }
        history = []
        if path.is_file():
            prior = read(path)
            if (
                prior.get("schema") != "github.copilot.dispatch-observation.v1"
                or prior.get("request_id") != request_id
                or prior.get("repository") != repository
                or not isinstance(prior.get("history"), list)
            ):
                raise ExecutionError("dispatch observation identity changed")
            history = prior["history"]
        history.append(event)
        write(path, {
            "schema": "github.copilot.dispatch-observation.v1",
            "request_id": request_id, "repository": repository,
            "status": "terminal" if remote_status == "terminal" else "observing",
            "task": task, "remote_status": remote_status,
            "history": history,
        })

    def start(self, command: list[str], *, require_execution: bool = False, **options: Any) -> OwnedProcess:
        self.check_cancel()
        sequence = uuid.uuid4().hex
        record = self.directory / f"child-{sequence}.json"
        request = self.directory / f"request-{sequence}.json"
        child_handle = self.directory / f"handle-{sequence}.json"
        supplied_environment = options.pop("env", None)
        env = dict(os.environ if supplied_environment is None else supplied_environment)
        env[PARENT_ENV] = str(request)
        result_file = (
            command[command.index("--result-file") + 1]
            if "--result-file" in command and command.index("--result-file") + 1 < len(command)
            else None
        )
        write(request, {
            "schema": SCHEMA, "root": str(self.root), "parent": str(self.handle),
            "child_record": str(record), "handle": str(child_handle),
            "command_sha256": digest(command),
        }, exclusive=True)
        write(record, {
            "schema": SCHEMA, "run_id": self.run_id,
            "lifecycle": "admitted", "admitted_at": time.time(),
            "process_identity": None, "handle": str(child_handle),
            "root": str(self.root), "command_sha256": digest(command),
            "exit_code": None, "local_drained": False,
            "requires_execution_result": require_execution,
            "breakaway_requested": False,
            "result_file": result_file,
        }, exclusive=True)
        streams = []
        try:
            for name in ("stdout", "stderr"):
                if name not in options:
                    stream = (self.directory / f"child-{sequence}-{name}.log").open("wb")
                    streams.append(stream)
                    options[name] = stream
            captured_output = {
                name: str(path)
                for name in ("stdout", "stderr")
                if isinstance(
                    path := getattr(options.get(name), "name", None),
                    str,
                )
            }
            write(record, {
                **read(record),
                "captured_output": captured_output,
            })
        except BaseException as failure:
            for stream in streams:
                stream.close()
            launch_error = f"{type(failure).__name__}: {failure}"
            write(record, {
                **read(record),
                "lifecycle": "launch_failed",
                "launch_failed_at": time.time(),
                "local_drained": True,
                "launch_error": launch_error,
                "cleanup_errors": [],
            })
            self.launch_failures.append({
                "error": launch_error,
                "pid": None,
                "local_drained": True,
                "cleanup_errors": [],
                "request": str(request),
            })
            raise
        options.setdefault("stdin", subprocess.DEVNULL)
        options["env"] = env
        if IS_WINDOWS:
            options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | 4
        else:
            options["start_new_session"] = True
        root = load_handle(self.root)
        owner = None
        process_binding = None
        process = None
        try:
            with guard(Path(root["cancel"]).with_suffix(".guard")):
                self.check_cancel()
                process = subprocess.Popen(command, **options)
                if IS_WINDOWS:
                    owner = WindowsOwner(process)
                    process_binding = owner.bind_process(
                        process._handle, process.pid
                    )
                    identity = dict(process_binding["identity"])
                else:
                    identity = process_identity(process.pid)
                if identity is None:
                    raise ExecutionError("child generation is unavailable")
                write(record, {
                    **read(record),
                    "lifecycle": "bound", "bound_at": time.time(),
                    "process_identity": identity,
                })
                if IS_WINDOWS:
                    resume_process(process.pid)
            owned = OwnedProcess(
                process, owner, record, streams, process_binding
            )
            self.children.append(owned)
            return owned
        except BaseException as failure:
            cleanup_errors = []
            if owner:
                cleanup_deadline = time.monotonic() + 10.0
                owner_terminated = False
                try:
                    owner.terminate()
                    owner_terminated = True
                except (OSError, ExecutionError) as cleanup:
                    cleanup_errors.append(str(cleanup))
                process_waited = False
                if process is not None:
                    try:
                        if not owner_terminated and process.poll() is None:
                            process.kill()
                        process.wait(
                            timeout=max(0.0, cleanup_deadline - time.monotonic())
                        )
                        process_waited = True
                    except (OSError, subprocess.SubprocessError) as cleanup:
                        cleanup_errors.append(str(cleanup))
                    if process_waited:
                        try:
                            process._handle.Close()
                        except OSError as cleanup:
                            cleanup_errors.append(str(cleanup))
                try:
                    owner.drain(cleanup_deadline)
                except (OSError, ExecutionError) as cleanup:
                    cleanup_errors.append(str(cleanup))
                try:
                    owner.close()
                except OSError as cleanup:
                    cleanup_errors.append(str(cleanup))
            elif process is not None:
                try:
                    if process.poll() is None:
                        if IS_WINDOWS:
                            process.kill()
                        else:
                            os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
                    if not IS_WINDOWS:
                        try:
                            os.killpg(process.pid, 0)
                        except ProcessLookupError:
                            pass
                        else:
                            cleanup_errors.append("failed launch has unconfirmed process-group drainage")
                except (OSError, subprocess.SubprocessError) as cleanup:
                    cleanup_errors.append(str(cleanup))
            for stream in streams:
                stream.close()
            write(record, {
                **read(record),
                "lifecycle": "launch_failed",
                "launch_failed_at": time.time(),
                "exit_code": (
                    process.returncode
                    if process is not None and isinstance(process.returncode, int)
                    else None
                ),
                "local_drained": not cleanup_errors,
                "launch_error": f"{type(failure).__name__}: {failure}",
                "cleanup_errors": cleanup_errors,
            })
            self.launch_failures.append({
                "error": f"{type(failure).__name__}: {failure}",
                "pid": process.pid if process is not None else None,
                "local_drained": not cleanup_errors, "cleanup_errors": cleanup_errors,
                "request": str(request),
            })
            if cleanup_errors:
                raise ExecutionError(
                    f"{type(failure).__name__}: {failure}; failed-launch drainage: {cleanup_errors}"
                ) from failure
            raise

    def run(self, command: list[str], **options: Any) -> subprocess.CompletedProcess[Any]:
        check = options.pop("check", False)
        timeout = options.pop("timeout", None)
        input_value = options.pop("input", None)
        if options.pop("capture_output", False):
            if "stdout" in options or "stderr" in options:
                raise ValueError("capture_output cannot be combined with stdout or stderr")
            options["stdout"] = options["stderr"] = subprocess.PIPE
        captured = {}
        for name in ("stdout", "stderr"):
            if options.get(name) == subprocess.PIPE:
                path = self.directory / f"capture-{uuid.uuid4().hex}-{name}.log"
                stream = path.open("wb")
                captured[name] = (path, stream)
                options[name] = stream
        if input_value is not None:
            options["stdin"] = subprocess.PIPE
        try:
            process = self.start(command, **options)
        except BaseException:
            for _, stream in captured.values():
                stream.close()
            raise
        write(process.record, {**read(process.record), "captured_output": {
            name: str(path) for name, (path, _) in captured.items()
        }})
        deadline = None if timeout is None else time.monotonic() + timeout
        sent = False
        try:
            while True:
                self.check_cancel()
                wait = 0.2
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, timeout)
                    wait = min(wait, remaining)
                try:
                    stdout, stderr = process.process.communicate(
                        input=None if sent else input_value, timeout=wait,
                    )
                    break
                except subprocess.TimeoutExpired:
                    sent = True
            code = process._wait(deadline, self.check_cancel)
            values = {}
            for name, (path, stream) in captured.items():
                stream.flush()
                values[name] = (
                    path.read_text(encoding=process.process.encoding, errors=process.process.errors)
                    if process.process.text_mode else path.read_bytes()
                )
            stdout, stderr = values.get("stdout", stdout), values.get("stderr", stderr)
            result = subprocess.CompletedProcess(command, code, stdout, stderr)
            if check:
                result.check_returncode()
            return result
        except BaseException as failure:
            try:
                process.terminate_tree()
            except (OSError, subprocess.SubprocessError, ExecutionError) as cleanup:
                if not (
                    process.drained
                    and process.completion_error == str(cleanup)
                ):
                    raise ExecutionError(
                        f"{type(failure).__name__}: {failure}; local drainage: {cleanup}"
                    ) from failure
            raise
        finally:
            for _, stream in captured.values():
                stream.close()

    def finish(self, code: int, error: str | None = None, *, cancelled=False) -> dict[str, Any]:
        if error and code == 0:
            code = 1
        drainage_errors = [
            str(failure) for failure in self.launch_failures if not failure["local_drained"]
        ]
        child_errors = []
        forced_cleanup_errors = []
        for child in self.children:
            poll_failed = False
            try:
                try:
                    state = child.poll()
                except (OSError, subprocess.SubprocessError, ExecutionError):
                    poll_failed = True
                    raise
                if state is None:
                    if child.exit_code is None:
                        child.terminate_tree()
                        if not cancelled:
                            drainage_errors.append("controller returned with a running child")
                    else:
                        child.wait()
                else:
                    child.wait()
            except (OSError, subprocess.SubprocessError, ExecutionError) as failure:
                if (
                    poll_failed and child.exit_code is not None
                    and not child.drained and child.owner is not None
                ):
                    try:
                        child.wait()
                    except (OSError, subprocess.SubprocessError, ExecutionError) as cleanup:
                        failure = cleanup
                if (
                    getattr(child, "drained", False) is True
                    and getattr(child, "completion_error", None) == str(failure)
                ):
                    if str(failure) == FORCED_DRAINAGE_ERROR:
                        forced_cleanup_errors.append(str(failure))
                    else:
                        child_errors.append(str(failure))
                else:
                    drainage_errors.append(str(failure))
        if not error and not cancelled and not (
            isinstance(self.last_result, dict)
            and (
                isinstance(self.last_result.get("result"), str)
                or self.root != self.handle and isinstance(self.last_result.get("status"), str)
            )
        ):
            error, code = "controller returned without a structured result", 1
        if drainage_errors or child_errors or forced_cleanup_errors:
            code = 1
        child_records = sorted(self.directory.rglob("child-*.json"))
        if not IS_WINDOWS:
            for source in child_records:
                child = read(source)
                if child.get("root") == str(self.root) and not child.get("local_drained"):
                    drainage_errors.append(f"descendant drainage is unconfirmed: {source}")
                    code = 1
        retained = []
        remote_tasks = []
        evidence_errors = list(child_errors)
        bootstrap_failures = []
        records = [self.record]
        child_executions = {}
        child_remote_uncertain = False
        for handle in self.directory.rglob("handle-*.json"):
            try:
                child = load_handle(handle)
                if child["root"] != str(self.root) or child["run_id"] != self.run_id:
                    raise ExecutionError("retained child evidence belongs to a different execution")
                records.append(child)
                terminal = status(handle)
                child_executions[str(handle)] = child
                if terminal.get("remote_work_may_continue") is True:
                    child_remote_uncertain = True
                if terminal.get("terminal") is True:
                    retained.append({
                        "path": terminal["result_file"], "sha256": terminal["result_sha256"],
                    })
                if (
                    terminal.get("terminal") is not True
                    or terminal.get("exit_code") != 0
                    or terminal.get("local_status") != "finished"
                    or terminal.get("local_children_drained") is not True
                    or terminal.get("remote_work_may_continue") is not False
                ):
                    raise ExecutionError("child execution is unfinished, failed, cancelled, or remotely unconfirmed")
            except (OSError, ValueError, KeyError, ExecutionError) as failure:
                evidence_errors.append(f"{handle}: {failure}")
        state_paths = sorted({path for record in records for path in record.get("domain_states", [])})
        state_remote_identity = False
        for path in state_paths:
            source = Path(path)
            if source.is_file():
                try:
                    retained.append({"path": path, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
                    value = read(source)
                    if value.get("schema") == "github.copilot.dispatch-observation.v1":
                        remote_tasks.append({**value, "evidence": path})
                    elif isinstance(value.get("task_id"), str) and value["task_id"]:
                        state_remote_identity = True
                except (OSError, ValueError, ExecutionError) as failure:
                    evidence_errors.append(f"{path}: {failure}")
            else:
                retained.append({"path": path, "missing": True})
                evidence_errors.append(f"recorded state is missing: {path}")
        bound_children = set()
        for source in child_records:
            try:
                record = read(source)
                lifecycle = record.get("lifecycle")
                if lifecycle == "bootstrap_failed":
                    if (
                        record.get("schema") != SCHEMA
                        or record.get("root") != str(self.root)
                        or record.get("run_id") != self.run_id
                        or not isinstance(record.get("handle"), str)
                        or not isinstance(record.get("command_sha256"), str)
                        or not isinstance(record.get("bootstrap_failure"), dict)
                    ):
                        raise ExecutionError(
                            "failed child bootstrap receipt belongs to a different execution"
                        )
                    retained.append({
                        "path": str(source),
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    })
                    for output in record.get("captured_output", {}).values():
                        output_path = Path(output)
                        if output_path.is_file():
                            retained.append({
                                "path": str(output_path),
                                "sha256": hashlib.sha256(
                                    output_path.read_bytes()
                                ).hexdigest(),
                            })
                    bootstrap_failures.append(record["bootstrap_failure"])
                    evidence_errors.append(
                        f"required child execution bootstrap failed: {source}"
                    )
                    if record.get("local_drained") is not True:
                        evidence_errors.append(
                            f"failed child bootstrap did not drain locally: {source}"
                        )
                    result_file = record.get("result_file")
                    if isinstance(result_file, str):
                        dispatch = Path(result_file + ".dispatch.json")
                        if dispatch.is_file():
                            observation = read(dispatch)
                            retained.append({
                                "path": str(dispatch),
                                "sha256": hashlib.sha256(
                                    dispatch.read_bytes()
                                ).hexdigest(),
                            })
                            remote_tasks.append({
                                **observation,
                                "evidence": str(dispatch),
                            })
                    continue
                if lifecycle == "launch_failed":
                    if (
                        record.get("schema") != SCHEMA
                        or record.get("root") != str(self.root)
                        or record.get("run_id") != self.run_id
                        or not isinstance(record.get("handle"), str)
                        or not isinstance(record.get("command_sha256"), str)
                    ):
                        raise ExecutionError(
                            "failed child launch receipt belongs to a different execution"
                        )
                    retained.append({
                        "path": str(source),
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    })
                    if record.get("local_drained") is not True:
                        evidence_errors.append(
                            f"failed child launch did not drain locally: {source}"
                        )
                    continue
                handle = record.get("handle")
                child = child_executions.get(handle) if isinstance(handle, str) else None
                if record.get("requires_execution_result") or child is not None:
                    if (
                        record.get("schema") != SCHEMA
                        or record.get("root") != str(self.root) or record.get("run_id") != self.run_id
                        or lifecycle not in {"bound", "draining", "drained", "drain_failed"}
                    ):
                        raise ExecutionError("child launch receipt belongs to a different execution")
                    if (
                        child is None or record.get("command_sha256") != child.get("command_sha256")
                        or not same_process(record.get("process_identity", {}), child.get("owner"))
                    ):
                        evidence_errors.append(f"required child execution evidence is absent or changed: {source}")
                    else:
                        bound_children.add(handle)
                result_file = record.get("result_file")
                if isinstance(result_file, str):
                    source = Path(result_file + ".dispatch.json")
                    if source.is_file():
                        observation = read(source)
                        retained.append({"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
                        remote_tasks.append({**observation, "evidence": str(source)})
            except (OSError, ValueError, ExecutionError) as failure:
                evidence_errors.append(f"{source}: {failure}")
        for handle in sorted(child_executions.keys() - bound_children):
            evidence_errors.append(f"child execution has no verified launch receipt: {handle}")
        if (
            bootstrap_failures
            and (self.last_result or {}).get("result") not in self.terminal_results
        ):
            self.emit({
                "result": "execution_bootstrap_failed",
                "session_title": "Execution bootstrap failed",
                "failures": bootstrap_failures,
            })

        def distinct_evidence(items: list[dict[str, Any]], path_key: str,
                              label: str) -> list[dict[str, Any]]:
            seen: dict[str, list[dict[str, Any]]] = {}
            distinct = []
            for item in items:
                path = item[path_key]
                versions = seen.setdefault(path, [])
                if item in versions:
                    continue
                if versions:
                    evidence_errors.append(f"conflicting {label}: {path}")
                versions.append(item)
                distinct.append(item)
            return distinct

        retained = distinct_evidence(retained, "path", "retained evidence")
        remote_tasks = distinct_evidence(remote_tasks, "evidence", "dispatch observations")
        presentation = None
        try:
            presentation = write_presentation(self)
            if presentation is not None and "path" in presentation:
                retained.append(
                    {
                        "path": presentation["path"],
                        "sha256": presentation["sha256"],
                    }
                )
        except OSError as failure:
            evidence_errors.append(
                f"terminal presentation could not be written: {failure}"
            )
        if evidence_errors or forced_cleanup_errors:
            code = 1
        outcome = (self.last_result or {}).get("result")
        confirmed = (
            not code and not error and not drainage_errors and not cancelled
            and (self.root != self.handle or outcome in self.terminal_results)
        )
        remote_observations_uncertain = any(
            task.get("remote_status") != "terminal" for task in remote_tasks
        )
        remote_work_may_continue = (
            child_remote_uncertain
            or remote_observations_uncertain
            or bool(drainage_errors or evidence_errors or forced_cleanup_errors)
            or (
                not confirmed
                and (
                    state_remote_identity
                    or isinstance((self.last_result or {}).get("task_id"), str)
                )
            )
        )
        remote_status = (
            "unconfirmed"
            if remote_work_may_continue else
            "terminal"
            if remote_tasks else
            "not_dispatched"
        )
        payload = {
            "schema": SCHEMA, "run_id": self.run_id, "owner": self.owner,
            "terminal": True, "exit_code": code, "error": error,
            "local_status": (
                "cancelled_local"
                if cancelled and not drainage_errors and not evidence_errors
                and not forced_cleanup_errors else
                "failed" if code or error or drainage_errors else "finished"
            ),
            "local_children_drained": not drainage_errors,
            "drainage_errors": drainage_errors,
            "finalization_errors": [*forced_cleanup_errors, *evidence_errors],
            "remote_status": remote_status,
            "remote_work_may_continue": remote_work_may_continue,
            "workflow_result": self.last_result,
            "presentation": presentation,
            "domain_states": state_paths,
            "child_records": [str(source) for source in child_records],
            "launch_failures": self.launch_failures,
            "retained_evidence": retained,
            "remote_tasks": remote_tasks,
            "finished_at": time.time(),
            "artifacts": {"progress": self.record["progress"], "stdout": self.record["stdout"],
                          "stderr": self.record["stderr"]},
        }
        root = load_handle(self.root)
        with guard(Path(root["cancel"]).with_suffix(".guard")):
            cancellation = Path(root["cancel"])
            if cancellation.exists():
                request = read(cancellation)
                if request.get("run_id") != self.run_id or request.get("owner") != root["owner"]:
                    raise ExecutionError("cancellation identity changed before terminal sealing")
                payload.update(
                    exit_code=130 if not drainage_errors and not evidence_errors else 1,
                    local_status="cancelled_local" if not drainage_errors and not evidence_errors else "failed",
                    remote_status="unconfirmed", remote_work_may_continue=True,
                )
                confirmed = False
            write(Path(self.record["result"]), payload, exclusive=True)
            self.record.update(
                status=payload["local_status"],
                result_sha256=hashlib.sha256(Path(self.record["result"]).read_bytes()).hexdigest(),
            )
            write(self.handle, self.record)
        return payload


def load_handle(path: Path) -> dict[str, Any]:
    if not path.is_absolute():
        raise ExecutionError("execution handle must be absolute")
    value = read(path)
    if value.get("schema") != SCHEMA or value.get("handle") != str(path.resolve()):
        raise ExecutionError("execution handle identity is invalid")
    return value


def _owned_root(
    path: Path,
    *,
    session_id: str,
    files: Path,
    helper: dict[str, str],
    commands: tuple[str, ...],
) -> dict[str, Any]:
    prefix = _owned_prefix(helper)
    if path.parent != files or not path.name.startswith(prefix) or not path.name.endswith(".json"):
        raise ExecutionError(f"foreign execution record: {path}")
    generation = path.name[len(prefix):-5]
    try:
        if uuid.UUID(hex=generation).hex != generation:
            raise ValueError
    except ValueError as failure:
        raise ExecutionError(f"malformed execution record name: {path}") from failure
    try:
        value = load_handle(path)
        route = value["route"]
        directory = path.with_name(path.name + ".d")
        if (
            path.is_symlink()
            or not _same_path(path.resolve(strict=True), path)
            or value.get("root") != str(path)
            or value.get("parent_request") is not None
            or route.get("session_id") != session_id
            or route.get("session_files") != str(files)
            or route.get("helper") != helper
        ):
            raise ExecutionError(f"foreign execution record: {path}")
        if (
            not isinstance(route.get("command"), str)
            or route["command"] not in commands
            or not isinstance(route.get("command_sha256"), str)
            or len(route["command_sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in route["command_sha256"]
            )
            or route["command_sha256"] != value.get("command_sha256")
        ):
            raise ExecutionError(f"execution command identity is invalid: {path}")
        if (
            not isinstance(value.get("run_id"), str)
            or uuid.UUID(hex=value["run_id"]).hex != value["run_id"]
            or not same_process(value.get("owner"), value.get("owner"))
            or not isinstance(value.get("started_at"), (int, float))
            or isinstance(value.get("started_at"), bool)
            or not math.isfinite(value["started_at"])
            or value["started_at"] <= 0
            or value.get("mode") != "foreground"
            or value.get("status")
            not in {"starting", "ready", "finished", "failed", "cancelled_local"}
        ):
            raise ExecutionError(f"execution generation identity is invalid: {path}")
        if (
            directory.is_symlink()
            or not _same_path(directory.resolve(strict=True), directory)
            or any(
                value.get(name) != str(directory / filename)
                for name, filename in {
                    "result": "result.json",
                    "stdout": "stdout.log",
                    "stderr": "stderr.log",
                    "progress": "progress.jsonl",
                    "cancel": "cancel.json",
                }.items()
            )
        ):
            raise ExecutionError(f"execution artifact identity is invalid: {path}")
        return value
    except ExecutionError:
        raise
    except (KeyError, OSError, TypeError, ValueError) as failure:
        raise ExecutionError(f"malformed execution record: {path}") from failure


def _owned_roots(commands: tuple[str, ...]) -> tuple[
    list[tuple[Path, dict[str, Any], dict[str, Any]]],
    list[tuple[Path, dict[str, Any], dict[str, Any]]],
]:
    session_id, files = _session_files()
    helper = _helper_identity()
    live = []
    terminal = []
    for path in sorted(files.glob(f"{_owned_prefix(helper)}*.json")):
        value = _owned_root(
            path, session_id=session_id, files=files, helper=helper, commands=commands
        )
        current = status(path)
        if current.get("terminal") is True:
            finished_at = current.get("finished_at")
            if (
                not isinstance(finished_at, (int, float))
                or isinstance(finished_at, bool)
                or not math.isfinite(finished_at)
                or finished_at < value["started_at"]
            ):
                raise ExecutionError(f"terminal execution timestamp is invalid: {path}")
            terminal.append((path, value, current))
            continue
        observed = process_identity(value["owner"]["pid"])
        if same_process(value["owner"], observed) and observed["running"]:
            live.append((path, value, current))
    return live, terminal


def _owned_control(control: str, commands: tuple[str, ...]) -> dict[str, Any]:
    live, terminal = _owned_roots(commands)
    if len(live) > 1:
        raise ExecutionError("multiple live executions belong to the current session and helper")
    if control == "execution-cancel":
        if len(live) != 1:
            raise ExecutionError(
                "execution-cancel requires exactly one live execution "
                "owned by the current session and helper"
            )
        return cancel(live[0][0])
    if live:
        return live[0][2]
    if not terminal:
        raise ExecutionError(
            "no live or terminal execution belongs to the current session and helper"
        )
    latest = max(item[2]["finished_at"] for item in terminal)
    selected = [item for item in terminal if item[2]["finished_at"] == latest]
    if len(selected) != 1:
        raise ExecutionError("latest terminal execution evidence is ambiguous")
    return selected[0][2]


def status(path: Path) -> dict[str, Any]:
    value = load_handle(path)
    result = Path(value["result"])
    if result.exists():
        if "result_sha256" not in value:
            return {"schema": SCHEMA, "run_id": value["run_id"], "terminal": False,
                    "status": "terminal_unsealed", "remote_status": "unconfirmed"}
        terminal = read(result)
        if (
            terminal.get("schema") != SCHEMA or terminal.get("run_id") != value["run_id"]
            or terminal.get("owner") != value["owner"] or terminal.get("terminal") is not True
            or hashlib.sha256(result.read_bytes()).hexdigest() != value.get("result_sha256")
        ):
            raise ExecutionError("terminal result identity or digest is invalid")
        return {**terminal, "result_file": str(result), "result_sha256": value["result_sha256"]}
    observed = process_identity(value["owner"]["pid"])
    root = load_handle(Path(value["root"]))
    cancellation = Path(root["cancel"])
    cancel_requested = cancellation.exists()
    if cancel_requested:
        request = read(cancellation)
        if request.get("run_id") != value["run_id"] or request.get("owner") != root["owner"]:
            raise ExecutionError("cancellation identity is invalid")
    records = [value]
    for child_path in path.with_name(path.name + ".d").rglob("handle-*.json"):
        child = load_handle(child_path)
        if child["root"] != value["root"] or child["run_id"] != value["run_id"]:
            raise ExecutionError("child evidence belongs to a different execution")
        records.append(child)
    state_paths = sorted({source for record in records for source in record.get("domain_states", [])})
    remote_tasks = []
    for source in state_paths:
        if source.endswith(".dispatch.json"):
            file = Path(source)
            remote_tasks.append({"evidence": source, **(
                read(file) if file.exists() else {"task": None, "remote_status": "unknown"}
            )})
    return {
        "schema": SCHEMA, "run_id": value["run_id"], "terminal": False,
        "status": (
            "cancel_requested" if cancel_requested else value["status"]
        ) if same_process(value["owner"], observed) and observed["running"] else "abandoned",
        "handle": str(path), "remote_status": "unconfirmed",
        "owner": value["owner"], "cancel_requested": cancel_requested,
        "domain_states": state_paths, "remote_tasks": remote_tasks,
        "artifacts": {name: value[name] for name in ("result", "stdout", "stderr", "progress")},
    }


def cancel(path: Path) -> dict[str, Any]:
    value = load_handle(path)
    if value["root"] != str(path.resolve()):
        raise ExecutionError("cancel requires the root execution handle")
    with guard(Path(value["cancel"]).with_suffix(".guard")):
        current = status(path)
        if current.get("terminal"):
            return {"result": "already_finished", "execution": current}
        require_owner(value["owner"])
        request = {"run_id": value["run_id"], "owner": value["owner"], "requested_at": time.time()}
        destination = Path(value["cancel"])
        if destination.exists():
            prior = read(destination)
            if prior.get("run_id") != value["run_id"] or prior.get("owner") != value["owner"]:
                raise ExecutionError("existing cancellation request is malformed")
        else:
            write(destination, request, exclusive=True)
        return {"result": "cancel_requested", "run_id": value["run_id"],
                "remote_status": "unconfirmed"}


def controller_main(main: Callable[[], int], namespace: dict[str, Any], *,
                    handle: Path | None = None, run_id: str | None = None,
                    commands: tuple[str, ...] = ()) -> int:
    arguments = list(sys.argv[1:])
    if arguments and arguments[0] in {"execution-status", "execution-cancel"}:
        if len(arguments) != 1:
            raise ExecutionError("execution controls take no arguments")
        if os.environ.get(PARENT_ENV):
            raise ExecutionError("child execution cannot invoke root execution controls")
        print(json.dumps(_owned_control(arguments[0], commands), sort_keys=True))
        return 0
    parent_text = os.environ.get(PARENT_ENV)
    parent = Path(parent_text) if parent_text else None
    if parent is not None:
        if handle is not None:
            raise ExecutionError("child execution cannot choose a root handle")
        handle = Path(read(parent)["handle"])
    route = None
    command = [sys.executable, str(Path(sys.argv[0]).resolve()), *arguments]
    if parent is None and SESSION_ENV in os.environ:
        handle, route = _owned_route(command)
        run_id = None
    if handle is None:
        return main()
    context = Execution(handle, command=command, parent=parent, run_id=run_id, route=route,
                        terminal_results=frozenset(namespace.get("EXECUTION_TERMINAL_RESULTS", ())))
    namespace["_EXECUTION"] = context
    common = namespace.get("common")
    if common is not None:
        common._EXECUTION = context
    code, error, cancelled = 1, None, False
    try:
        with (
            Path(context.record["stdout"]).open("x", encoding="utf-8") as output,
            Path(context.record["stderr"]).open("x", encoding="utf-8") as errors,
            redirect_stdout(output), redirect_stderr(errors),
        ):
            context.ready()
            code = main()
            if context.last_result is None and "--result-file" in arguments:
                index = arguments.index("--result-file")
                if index + 1 < len(arguments):
                    result_path = Path(arguments[index + 1])
                    if result_path.is_file():
                        context.emit(read(result_path))
            context.check_cancel()
    except Cancelled as failure:
        code, error, cancelled = 130, str(failure), True
    except BaseException as failure:
        code, error = 1, f"{type(failure).__name__}: {failure}"
    finally:
        namespace["_EXECUTION"] = None
        if common is not None:
            common._EXECUTION = None
    try:
        return context.finish(code, error, cancelled=cancelled)["exit_code"]
    except (OSError, ValueError, KeyError, TypeError, ExecutionError) as failure:
        message = (
            f"controller exit {code}, error {error!r}; "
            f"terminal sealing failed: {type(failure).__name__}: {failure}"
        )
        try:
            with Path(context.record["stderr"]).open("a", encoding="utf-8") as stream:
                stream.write(message + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as sink_error:
            message += f"; stderr sink failed: {sink_error}"
        raise ExecutionError(message) from failure


def entrypoint(main: Callable[[], int], namespace: dict[str, Any], *,
               commands: tuple[str, ...], sealed_handle: Path | None = None,
               run_id: str | None = None) -> int:
    arguments = sys.argv[1:]
    if any(
        argument == "--execution-handle" or argument.startswith("--execution-handle=")
        for argument in arguments
    ):
        raise ExecutionError("--execution-handle is not supported")
    controls = {"execution-status", "execution-cancel"}
    if (
        not os.environ.get(PARENT_ENV)
        and (not arguments or arguments[0] not in {*commands, *controls})
    ):
        return main()
    return controller_main(
        main, namespace, handle=sealed_handle, run_id=run_id, commands=commands
    )
