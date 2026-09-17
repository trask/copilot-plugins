#!/usr/bin/env python3
"""Admit only the installed CI Fix Loop commands used by its custom agent."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import sys
from typing import Any


ALLOWED_SUBCOMMANDS = {
    "agent-task",
    "loop",
    "stack-format",
    "stack-next",
    "stack-propagate",
    "stack-record",
    "stack-start",
    "stack-status",
}
TARGET_COMMANDS = {"agent-task", "loop", "stack-start"}
MODEL_COMMANDS = {"agent-task", "loop"}
TARGET_PATTERN = re.compile(
    r"(?:https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*"
    r"|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*)"
)
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


def normalized_path(value: str) -> str:
    return os.path.normcase(os.path.abspath(value))


def option_values(tokens: list[str], option: str) -> list[str]:
    return [
        tokens[index + 1]
        for index, token in enumerate(tokens[:-1])
        if token == option
    ]


def command_tokens(tool_name: str, command: str) -> list[str] | None:
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


def admission_allowed(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    tool_name = payload.get("toolName")
    tool_input = payload.get("toolInput")
    cwd = payload.get("cwd")
    if (
        payload.get("hookName") != "permissionRequest"
        or not isinstance(tool_name, str)
        or not isinstance(cwd, str)
        or not isinstance(tool_input, dict)
        or set(tool_input) != {"command"}
        or not isinstance(tool_input["command"], str)
    ):
        return False
    tokens = command_tokens(tool_name, tool_input["command"])
    if not tokens or tokens[0] not in ALLOWED_SUBCOMMANDS:
        return False
    subcommand = tokens[0]
    if subcommand in TARGET_COMMANDS:
        if len(tokens) < 2 or TARGET_PATTERN.fullmatch(tokens[1]) is None:
            return False
        roots = option_values(tokens, "--repo-root")
        if len(roots) != 1 or normalized_path(roots[0]) != normalized_path(cwd):
            return False
    models = option_values(tokens, "--model")
    if subcommand in MODEL_COMMANDS:
        if models != ["sol"]:
            return False
    elif models:
        return False
    return True


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
