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
SEALED_CI_FIX_COMMANDS = {"run-sealed-ci-fix"}
EXECUTION_CONTROL_COMMANDS = {"execution-status", "execution-cancel"}
LEGACY_OWNER_ELIGIBILITY_SCHEMA = (
    "github.copilot.ci-fix-loop-legacy-owner-eligibility.v3"
)
LEGACY_OWNER_AUTHORIZATION_FILE_SCHEMA = (
    "github.copilot.ci-fix-loop-legacy-owner-authorization-file.v1"
)
SEALED_CI_FIX_INVOCATION_SCHEMA = (
    "github.copilot.ci-fix-loop-sealed-invocation.v3"
)
SEALED_CI_FIX_MUTATION_POLICY = {
    "id": "allow",
    "allowed": [
        "create_managed_agent_task",
        "push_verified_fix_commits",
        "github_workflow_rerun",
    ],
    "forbidden": [
        "github_comments",
        "github_reviews",
        "github_review_threads",
        "github_labels",
        "github_pull_request_metadata",
    ],
}
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
        not in (
            set(RECONCILIATION_COMMANDS)
            | SEALED_RECONCILIATION_COMMANDS
            | SEALED_CI_FIX_COMMANDS
            | EXECUTION_CONTROL_COMMANDS
        )
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
        ensure_ascii=True,
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


def sealed_ci_fix_admission(
    tokens: list[str],
    *,
    cwd: str,
    session_id: str,
    fresh: bool = True,
) -> bool:
    if len(tokens) != 2:
        return False
    artifact_path = Path(tokens[1])
    if not canonical_session_evidence_path(artifact_path):
        return False
    payload = read_small_json(artifact_path)
    if payload is None:
        return False
    digest_path = artifact_path.with_name(f"{artifact_path.name}.sha256")
    try:
        content = artifact_path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        digest_valid = (
            digest_path.is_file()
            and not digest_path.is_symlink()
            and digest_path.read_bytes() == f"{digest}\n".encode("ascii")
        )
    except OSError:
        return False
    required = {
        "schema",
        "result",
        "created_at",
        "invocation_id",
        "invocation_artifact",
        "invocation_sha256_file",
        "package_manifest",
        "request",
        "outputs",
        "inner_argv",
        "run_command_argv",
        "seal",
    }
    if (
        not digest_valid
        or set(payload) != required
        or payload.get("schema") != SEALED_CI_FIX_INVOCATION_SCHEMA
        or payload.get("result") != "sealed_ci_fix_ready"
        or payload.get("invocation_artifact") != str(artifact_path)
        or payload.get("invocation_sha256_file") != str(digest_path)
        or not isinstance(payload.get("invocation_id"), str)
        or re.fullmatch(r"[0-9a-f]{32}", payload["invocation_id"]) is None
        or not isinstance(payload.get("seal"), str)
        or SHA256_PATTERN.fullmatch(payload["seal"]) is None
    ):
        return False
    identity = dict(payload)
    identity.pop("seal")
    if canonical_json_sha256(identity) != payload["seal"]:
        return False
    request = payload.get("request")
    snapshot = request.get("initial_snapshot") if isinstance(request, dict) else None
    state = snapshot.get("state") if isinstance(snapshot, dict) else None
    package_manifest = payload.get("package_manifest")
    manifest_path = (
        Path(str(package_manifest.get("path") or ""))
        if isinstance(package_manifest, dict)
        else None
    )
    if (
        not isinstance(request, dict)
        or request.get("repo_root") is None
        or normalized_path(str(request["repo_root"])) != normalized_path(cwd)
        or request.get("owner_session_id") != session_id
        or request.get("execution_mode") != "foreground_controller"
        or request.get("execution_handle") != str(artifact_path.with_name(
            f"ci-fix-loop-sealed-{payload['invocation_id']}-execution.json"
        ))
        or TARGET_PATTERN.fullmatch(str(request.get("target") or "")) is None
        or request.get("model_alias") != "sol"
        or request.get("model") != "gpt-5.6-sol"
        or request.get("fresh_invocation") is not True
        or request.get("topology") != "single_pull_request"
        or request.get("github_mutation_policy")
        != SEALED_CI_FIX_MUTATION_POLICY
        or not isinstance(snapshot, dict)
        or snapshot.get("target") != request.get("target")
        or normalized_path(str(snapshot.get("repo_root") or ""))
        != normalized_path(cwd)
        or not isinstance(state, dict)
        or not isinstance(state.get("path"), str)
        or not Path(state["path"]).is_absolute()
        or request.get("state") != state["path"]
        or state.get("exists") is not False
        or state.get("size") is not None
        or state.get("sha256") is not None
        or snapshot.get("active_owner") is not None
        or not isinstance(snapshot.get("source"), dict)
        or not isinstance(snapshot.get("pull_request"), dict)
        or snapshot["source"].get("status") != ""
        or snapshot["source"].get("head")
        != snapshot["pull_request"].get("head_sha")
        or snapshot["source"].get("branch")
        != snapshot["pull_request"].get("head_branch")
        or manifest_path is None
        or not manifest_path.is_absolute()
        or not manifest_path.is_file()
        or manifest_path.is_symlink()
        or not isinstance(package_manifest.get("sha256"), str)
        or SHA256_PATTERN.fullmatch(package_manifest["sha256"]) is None
    ):
        return False
    run_argv = payload.get("run_command_argv")
    expected_helper = Path(__file__).with_name("ci_fix_loop.py").resolve()
    if (
        not isinstance(run_argv, list)
        or len(run_argv) != 4
        or not all(isinstance(value, str) for value in run_argv)
        or normalized_path(run_argv[1])
        != normalized_path(str(expected_helper))
        or run_argv[2] != "run-sealed-ci-fix"
        or run_argv[3] != str(artifact_path)
    ):
        return False
    outputs = payload.get("outputs")
    invocation_id = payload["invocation_id"]
    expected_outputs = {
        "state": artifact_path.with_name(
            f"ci-fix-loop-sealed-{invocation_id}-state.json"
        ),
        "result": artifact_path.with_name(
            f"ci-fix-loop-sealed-{invocation_id}-result.json"
        ),
        "stack_start_result": artifact_path.with_name(
            f"ci-fix-loop-sealed-{invocation_id}-stack-start-result.json"
        ),
        "loop_result": artifact_path.with_name(
            f"ci-fix-loop-sealed-{invocation_id}-loop-result.json"
        ),
    }
    if (
        not isinstance(outputs, dict)
        or set(outputs) != set(expected_outputs)
        or any(
            outputs[name] != str(path)
            or fresh and path.exists()
            or path.is_symlink()
            for name, path in expected_outputs.items()
        )
        or state["path"] != outputs["state"]
    ):
        return False
    handle = Path(request["execution_handle"])
    if handle.is_symlink() or fresh and (handle.exists() or handle.with_name(handle.name + ".d").exists()):
        return False
    if not fresh:
        execution = read_small_json(handle)
        if (
            execution is None or execution.get("schema") != "github.copilot.foreground-execution.v1"
            or execution.get("run_id") != invocation_id
            or execution.get("handle") != str(handle)
            or execution.get("root") != str(handle)
        ):
            return False
    return True


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
        or not isinstance(session_id, str)
        or not isinstance(tool_input, dict)
        or not set(tool_input) <= {"command", "mode", "detach", "shellId", "description"}
        or not isinstance(tool_input.get("command"), str)
    ):
        return False
    tokens = command_tokens(tool_name, tool_input["command"])
    fresh = bool(tokens and tokens[0] == "run-sealed-ci-fix")
    if fresh:
        if tool_input.get("mode") != "async" or tool_input.get("detach") is not True:
            return False
    elif tool_input.get("mode", "sync") != "sync" or tool_input.get("detach", False) is not False:
        return False
    if (
        "shellId" in tool_input and (
            not isinstance(tool_input["shellId"], str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tool_input["shellId"]) is None
        )
        or "description" in tool_input and (
            not isinstance(tool_input["description"], str) or len(tool_input["description"]) > 100
        )
    ):
        return False
    return bool(
        tokens
        and tokens[0] in SEALED_CI_FIX_COMMANDS | EXECUTION_CONTROL_COMMANDS
        and sealed_ci_fix_admission(
            tokens,
            cwd=cwd,
            session_id=session_id,
            fresh=fresh,
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
