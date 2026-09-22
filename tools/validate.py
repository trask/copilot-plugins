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
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        [sys.executable, "-m", "pytest", *arguments],
        check=False,
        **options,
    ).returncode


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
    print("usage: python tools/validate.py [fast|full]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
