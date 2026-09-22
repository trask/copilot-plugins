from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parents[1]


def pytest_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("GIT_CONFIG_COUNT", None)
    for name in tuple(environment):
        if name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(name)
    return environment


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
        result = run_pytest(
            "-n",
            "4",
            "--dist=load",
            "--tb=short",
            "-m",
            "not legacy_e2e and not windows_e2e",
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
    if mode == "legacy":
        return run_pytest(
            "-n",
            "4",
            "--dist=load",
            "--tb=short",
            "-m",
            "legacy_e2e",
        )
    print("usage: python tools/validate.py [fast|legacy|full]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
