#!/usr/bin/env python3
"""Admit only the installed CI Fix Loop commands used by its custom agent."""

from __future__ import annotations

import json
import hashlib
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
SEALED_RECONCILIATION_COMMANDS = {
    "verify-sealed-legacy-owner-reconciliation",
    "apply-sealed-legacy-owner-reconciliation",
}
LEGACY_OWNER_ELIGIBILITY_SCHEMA = (
    "github.copilot.ci-fix-loop-legacy-owner-eligibility.v3"
)
LEGACY_OWNER_AUTHORIZATION_FILE_SCHEMA = (
    "github.copilot.ci-fix-loop-legacy-owner-authorization-file.v1"
)
COMMAND_RESULT_SCHEMAS = {
    "stack-start": "github.copilot.ci-fix-loop-stack-start-result.v1",
    "loop": "github.copilot.ci-fix-loop-loop-result.v1",
}
COMMAND_RESULT_KEYS = {
    "command",
    "command_id",
    "exit_code",
    "finished_at",
    "outcome",
    "outcome_sha256",
    "owner",
    "request",
    "result_file",
    "schema",
    "started_at",
    "state_identity",
    "status",
    "terminal",
}
COMMAND_REQUEST_KEYS = {
    "argv_sha256",
    "invocation_run",
    "model",
    "new_invocation",
    "pipeline_iteration",
    "pipeline_max_iterations",
    "pipeline_run",
    "preflight_result_file",
    "repo_root",
    "stack_state",
    "state",
    "target",
}
COMMAND_OWNER_KEYS = {"executable", "parent_process_id", "process_id"}
COMMAND_RESULT_FILE_PATTERNS = {
    "stack-start": re.compile(r"^ci-fix-loop-stack-start-result\.json$"),
    "loop": re.compile(r"^ci-fix-loop-loop-result-[1-9][0-9]*\.json$"),
}
COMMAND_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
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
        or tokens[2]
        not in set(RECONCILIATION_COMMANDS) | SEALED_RECONCILIATION_COMMANDS
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


def canonical_session_evidence_path(path: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    return (
        path.is_absolute()
        and resolved == path
        and path.is_file()
        and not path.is_symlink()
        and path.parent.name == "files"
        and re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            path.parent.parent.name,
        )
        is not None
        and path.parent.parent.parent.name == "session-state"
    )


def read_small_json(path: Path) -> dict[str, Any] | None:
    def object_without_duplicates(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    try:
        if path.stat().st_size > 1024 * 1024:
            return None
        content = path.read_bytes()
        if b"\r" in content or not content.endswith(b"\n"):
            return None
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=object_without_duplicates,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def canonical_json_sha256(value: Any) -> str:
    content = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def command_result_path(value: str, command: str, *, must_exist: bool) -> Path | None:
    supplied = Path(value)
    if not supplied.is_absolute():
        return None
    path = supplied.resolve()
    session = path.parent.parent
    if (
        COMMAND_RESULT_FILE_PATTERNS[command].fullmatch(path.name) is None
        or path.parent.name != "files"
        or path.parent.parent.parent.name != "session-state"
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            session.name,
        )
        is None
        or not path.parent.is_dir()
        or path.parent.is_symlink()
        or session.is_symlink()
    ):
        return None
    if must_exist:
        return path if path.is_file() and not path.is_symlink() else None
    return None if path.exists() else path


def target_identity(value: str) -> tuple[str, str, int] | None:
    match = TARGET_PATTERN.fullmatch(value)
    if match is None:
        return None
    if value.startswith("https://"):
        parts = value.removeprefix("https://github.com/").split("/")
        return parts[0].lower(), parts[1].lower(), int(parts[3])
    repository, number = value.rsplit("#", 1)
    owner, repo = repository.split("/", 1)
    return owner.lower(), repo.lower(), int(number)


def valid_stack_start_result(
    path: Path,
    *,
    target: str,
    cwd: str,
    stack_state: str | None,
) -> bool:
    payload = read_small_json(path)
    if (
        payload is None
        or set(payload) != COMMAND_RESULT_KEYS
        or payload.get("schema") != COMMAND_RESULT_SCHEMAS["stack-start"]
        or payload.get("command") != "stack-start"
        or COMMAND_ID_PATTERN.fullmatch(str(payload.get("command_id") or "")) is None
        or payload.get("status") != "succeeded"
        or payload.get("terminal") is not True
        or payload.get("exit_code") != 0
        or normalized_path(str(payload.get("result_file") or ""))
        != normalized_path(str(path))
        or not isinstance(payload.get("request"), dict)
        or set(payload["request"]) != COMMAND_REQUEST_KEYS
        or not isinstance(payload.get("owner"), dict)
        or set(payload["owner"]) != COMMAND_OWNER_KEYS
        or not isinstance(payload.get("outcome"), dict)
        or payload.get("outcome_sha256")
        != canonical_json_sha256(payload["outcome"])
        or normalized_path(str(payload["request"].get("repo_root") or ""))
        != normalized_path(cwd)
        or target_identity(str(payload["request"].get("target") or ""))
        != target_identity(target)
    ):
        return False
    outcome = payload["outcome"]
    if target_identity(str(outcome.get("target") or "")) != target_identity(target):
        return False
    if outcome.get("result") == "single":
        return stack_state is None
    if outcome.get("result") != "stack" or stack_state is None:
        return False
    return normalized_path(str(outcome.get("state") or "")) == normalized_path(
        stack_state
    )


def sealed_reconciliation_admission(
    tokens: list[str],
    *,
    cwd: str,
) -> bool:
    if len(tokens) != 2:
        return False
    evidence_path = Path(tokens[1])
    if not canonical_session_evidence_path(evidence_path):
        return False
    payload = read_small_json(evidence_path)
    if payload is None:
        return False
    expected_helper = Path(__file__).with_name("ci_fix_loop.py").resolve()
    if tokens[0] == "verify-sealed-legacy-owner-reconciliation":
        snapshot = payload.get("snapshot")
        verifier = payload.get("verifier_command_argv")
        return bool(
            payload.get("schema") == LEGACY_OWNER_ELIGIBILITY_SCHEMA
            and payload.get("eligibility_artifact") == str(evidence_path)
            and isinstance(snapshot, dict)
            and normalized_path(str(snapshot.get("repo_root") or ""))
            == normalized_path(cwd)
            and TARGET_PATTERN.fullmatch(str(snapshot.get("target") or ""))
            is not None
            and isinstance(verifier, list)
            and len(verifier) == 4
            and normalized_path(str(verifier[1]))
            == normalized_path(str(expected_helper))
            and verifier[2] == tokens[0]
            and verifier[3] == str(evidence_path)
        )
    apply = payload.get("apply_command_argv")
    return bool(
        payload.get("schema") == LEGACY_OWNER_AUTHORIZATION_FILE_SCHEMA
        and payload.get("authorization_file") == str(evidence_path)
        and normalized_path(str(payload.get("repo_root") or ""))
        == normalized_path(cwd)
        and TARGET_PATTERN.fullmatch(str(payload.get("target") or ""))
        is not None
        and isinstance(apply, list)
        and len(apply) == 4
        and normalized_path(str(apply[1]))
        == normalized_path(str(expected_helper))
        and apply[2] == tokens[0]
        and apply[3] == str(evidence_path)
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
            and (
                (
                    tokens[0] in RECONCILIATION_COMMANDS
                    and reconciliation_admission(tokens, cwd=cwd)
                )
                or (
                    tokens[0] in SEALED_RECONCILIATION_COMMANDS
                    and sealed_reconciliation_admission(tokens, cwd=cwd)
                )
            )
        )
    subcommand = tokens[0]
    if subcommand in TARGET_COMMANDS:
        if len(tokens) < 2 or TARGET_PATTERN.fullmatch(tokens[1]) is None:
            return False
        roots = option_values(tokens, "--repo-root")
        if len(roots) != 1 or normalized_path(roots[0]) != normalized_path(cwd):
            return False
    if subcommand in COMMAND_RESULT_SCHEMAS:
        result_files = option_values(tokens, "--result-file")
        if len(result_files) != 1:
            return False
        result_path = command_result_path(
            result_files[0],
            subcommand,
            must_exist=False,
        )
        if result_path is None:
            return False
        if subcommand == "loop":
            pipeline_runs = option_values(tokens, "--pipeline-run")
            preflight_files = option_values(tokens, "--preflight-result-file")
            if pipeline_runs:
                if len(pipeline_runs) != 1 or preflight_files:
                    return False
            else:
                if len(preflight_files) != 1:
                    return False
                preflight_path = command_result_path(
                    preflight_files[0],
                    "stack-start",
                    must_exist=True,
                )
                stack_states = option_values(tokens, "--stack-state")
                if (
                    preflight_path is None
                    or preflight_path.parent != result_path.parent
                    or len(stack_states) > 1
                    or not valid_stack_start_result(
                        preflight_path,
                        target=tokens[1],
                        cwd=cwd,
                        stack_state=stack_states[0] if stack_states else None,
                    )
                ):
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
