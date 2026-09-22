#!/usr/bin/env python3
"""Admit only the installed CI Fix Loop command used by its custom agent."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import sys
from typing import Any


TARGET_PATTERN = re.compile(
    r"(?:https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*"
    r"|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*"
    r"|[1-9][0-9]*)"
)
SESSION_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
EXECUTION_CONTROL_COMMANDS = {"execution-status", "execution-cancel"}
FORBIDDEN_COMMAND_CHARACTERS = frozenset("\0\r\n;&|<>`$(){}")
POWERSHELL_PREFIX = (
    '$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { '
    '"$env:USERPROFILE\\.copilot" }; '
    '$ciFixLoop = "$copilotHome\\installed-plugins\\trask-plugins\\'
    'ci-fix-loop\\scripts\\ci_fix_loop.py"; python $ciFixLoop '
)
BASH_PREFIX = (
    'copilot_home="${COPILOT_HOME:-$HOME/.copilot}"; '
    'ci_fix_loop="$copilot_home/installed-plugins/trask-plugins/'
    'ci-fix-loop/scripts/ci_fix_loop.py"; python3 "$ci_fix_loop" '
)


def split_arguments(tool_name: str, command: str) -> list[str] | None:
    prefix = {
        "powershell": POWERSHELL_PREFIX,
        "bash": BASH_PREFIX,
    }.get(tool_name)
    if prefix is None or not command.startswith(prefix):
        return None
    arguments = command[len(prefix) :]
    if (
        not arguments
        or len(arguments) > 32768
        or any(character in FORBIDDEN_COMMAND_CHARACTERS for character in arguments)
    ):
        return None
    try:
        return shlex.split(arguments, posix=True)
    except ValueError:
        return None


def command_allowed(tokens: list[str]) -> bool:
    if len(tokens) == 2 and tokens[0] == "run":
        return TARGET_PATTERN.fullmatch(tokens[1]) is not None
    if (
        len(tokens) == 4
        and tokens[0] == "run"
        and TARGET_PATTERN.fullmatch(tokens[1]) is not None
        and tokens[2:] == ["--github-mutation-policy", "source-only"]
    ):
        return True
    return len(tokens) == 1 and tokens[0] in EXECUTION_CONTROL_COMMANDS


def admission_allowed(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    tool_name = payload.get("toolName")
    tool_input = payload.get("toolInput")
    cwd = payload.get("cwd")
    session_id = payload.get("sessionId")
    if (
        payload.get("hookName") != "permissionRequest"
        or not isinstance(tool_name, str)
        or not isinstance(cwd, str)
        or not Path(cwd).is_absolute()
        or not Path(cwd).is_dir()
        or Path(cwd).is_symlink()
        or not isinstance(session_id, str)
        or SESSION_ID_PATTERN.fullmatch(session_id) is None
        or not isinstance(tool_input, dict)
        or not set(tool_input)
        <= {"command", "mode", "detach", "shellId", "description"}
        or not isinstance(tool_input.get("command"), str)
    ):
        return False
    tokens = split_arguments(tool_name, tool_input["command"])
    if tokens is None or not command_allowed(tokens):
        return False
    launch = tokens[0] == "run"
    if launch:
        if (
            tool_input.get("mode") != "async"
            or not isinstance(tool_input.get("detach", False), bool)
        ):
            return False
    elif (
        tool_input.get("mode", "sync") != "sync"
        or tool_input.get("detach", False) is not False
    ):
        return False
    if (
        "shellId" in tool_input
        and (
            not isinstance(tool_input["shellId"], str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tool_input["shellId"]) is None
        )
    ):
        return False
    return not (
        "description" in tool_input
        and (
            not isinstance(tool_input["description"], str)
            or len(tool_input["description"]) > 100
        )
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, UnicodeError):
        payload = None
    decision = {"behavior": "allow"} if admission_allowed(payload) else {}
    print(json.dumps(decision, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
