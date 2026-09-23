from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parents[1]
DEFAULT_WORKERS = 4
MAX_LOCAL_WINDOWS_WORKERS = 8


def pytest_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("GIT_CONFIG_COUNT", None)
    for name in tuple(environment):
        if name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(name)
    return environment


def pytest_parallelism() -> tuple[int, str]:
    if os.name != "nt" or os.environ.get("CI"):
        return DEFAULT_WORKERS, "load"
    workers = (
        MAX_LOCAL_WINDOWS_WORKERS
        if (os.cpu_count() or DEFAULT_WORKERS) >= MAX_LOCAL_WINDOWS_WORKERS * 2
        else DEFAULT_WORKERS
    )
    return workers, "worksteal" if workers > DEFAULT_WORKERS else "load"


def run_pytest(*arguments: str) -> int:
    options: dict[str, object] = {
        "cwd": ROOT,
        "env": pytest_environment(),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "bufsize": 0,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    with subprocess.Popen(
        [sys.executable, "-m", "pytest", *arguments],
        **options,
    ) as process:
        try:
            assert process.stdout is not None
            while chunk := process.stdout.read(4096):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            return process.wait()
        except (KeyboardInterrupt, BrokenPipeError):
            process.kill()
            raise


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "fast"
    if mode in {"fast", "full"}:
        workers, distribution = pytest_parallelism()
        result = run_pytest(
            "-n",
            str(workers),
            f"--dist={distribution}",
            "--tb=short",
            "-m",
            "not windows_e2e",
        )
        if result or os.name != "nt":
            return result
        return run_pytest(
            "-n",
            "0",
            "--tb=short",
            "-m",
            "windows_e2e",
        )
    if mode == "test" and len(sys.argv) > 2:
        return run_pytest("-n", "0", "--tb=short", *sys.argv[2:])
    print(
        "usage: python tools/validate.py [fast|full|test <pytest selector> ...]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
