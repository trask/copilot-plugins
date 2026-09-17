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
RECONCILIATION_COMMANDS = {
    "verify-legacy-owner-reconciliation": {
        "--repo-root",
        "--state",
        "--eligibility-artifact",
        "--eligibility-sha256-file",
        "--package-manifest",
        "--expected-package-manifest-sha256",
        "--expected-seal",
    },
    "apply-legacy-owner-reconciliation": {
        "--repo-root",
        "--state",
        "--eligibility-artifact",
        "--eligibility-sha256-file",
        "--expected-artifact-sha256",
        "--package-manifest",
        "--expected-package-manifest-sha256",
        "--expected-seal",
        "--expected-authorization-token",
    },
}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
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
    if prefix is None:
        return None
    if command.startswith(prefix):
        arguments = command[len(prefix) :]
        if (
            not arguments
            or len(arguments) > 32768
            or any(
                character in FORBIDDEN_COMMAND_CHARACTERS
                for character in arguments
            )
        ):
            return None
        try:
            return shlex.split(arguments, posix=True)
        except ValueError:
            return None
    if (
        not command
        or len(command) > 32768
        or any(character in FORBIDDEN_COMMAND_CHARACTERS for character in command)
    ):
        return None
    try:
        tokens = shlex.split(command, posix=tool_name != "powershell")
    except ValueError:
        return None
    if tool_name == "powershell":
        tokens = [
            token[1:-1]
            if len(token) >= 2 and token[0] == token[-1] == '"'
            else token
            for token in tokens
        ]
    expected_helper = Path(__file__).with_name("ci_fix_loop.py")
    if (
        len(tokens) < 3
        or re.fullmatch(
            r"python(?:3(?:\.[0-9]+)?)?(?:\.exe)?",
            Path(tokens[0]).name.lower(),
        )
        is None
        or normalized_path(tokens[1]) != normalized_path(str(expected_helper))
        or tokens[2] not in RECONCILIATION_COMMANDS
    ):
        return None
    return tokens[2:]


def reconciliation_admission(
    tokens: list[str],
    *,
    cwd: str,
) -> bool:
    if len(tokens) < 4 or TARGET_PATTERN.fullmatch(tokens[1]) is None:
        return False
    remainder = tokens[2:]
    if len(remainder) % 2:
        return False
    options = {
        remainder[index]: remainder[index + 1]
        for index in range(0, len(remainder), 2)
    }
    if (
        len(options) * 2 != len(remainder)
        or set(options) != RECONCILIATION_COMMANDS.get(tokens[0])
        or normalized_path(options["--repo-root"]) != normalized_path(cwd)
    ):
        return False
    for name in (
        "--state",
        "--eligibility-artifact",
        "--eligibility-sha256-file",
        "--package-manifest",
    ):
        path = Path(options[name])
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            return False
    hash_options = {
        "--expected-package-manifest-sha256",
        "--expected-seal",
    }
    if tokens[0] == "apply-legacy-owner-reconciliation":
        hash_options.update(
            {
                "--expected-artifact-sha256",
                "--expected-authorization-token",
            }
        )
    return all(
        SHA256_PATTERN.fullmatch(options[name]) is not None
        for name in hash_options
    )


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
        return bool(
            tokens
            and tokens[0] in RECONCILIATION_COMMANDS
            and reconciliation_admission(tokens, cwd=cwd)
        )
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
