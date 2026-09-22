#!/usr/bin/env python3
"""Run a fresh GitHub Agent Task and derive a version-5 candidate."""

from __future__ import annotations

import base64
import binascii
import email.utils
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence, TextIO


API_VERSION = "2026-03-10"
ACCEPT = "application/vnd.github+json"
AGENT_TASK_POLL_INTERVAL_SECONDS = 60
MAX_TRANSIENT_FAILURES = 5
MAX_RETRY_SECONDS = 30
RATE_LIMIT_SAFETY_MARGIN_SECONDS = 1
MODEL_IDS = {
    "luna": "gpt-5.6-luna",
    "terra": "gpt-5.6-terra",
    "sol": "gpt-5.6-sol",
    "astra": "gpt-6-astra",
}
ACTIVE_STATES = {"queued", "in_progress"}
SUCCESS_STATES = {"completed"}
ERROR_STATES = {"failed", "timed_out", "cancelled"}
BLOCKED_STATES = {"waiting_for_user", "idle"}
KNOWN_STATES = ACTIVE_STATES | SUCCESS_STATES | ERROR_STATES | BLOCKED_STATES
SHA_PATTERN = re.compile(r"\A[0-9a-fA-F]{40}\Z")
OUTPUT_DIRECTORY = ".github/agent-task-output"
OUTPUT_REPORT_PATH = f"{OUTPUT_DIRECTORY}/report.md"
REPORT_PATH_PLACEHOLDER = "{{MARKETPLACE_REPORT_PATH}}"
SEMANTIC_PATH_PLACEHOLDER = "{{MARKETPLACE_SEMANTIC_PATH}}"
VALIDATION_PATH_PLACEHOLDER = "{{MARKETPLACE_VALIDATION_PATH}}"
PR_CONTEXT_MARKER = "----- /cloud source pull request -----"
POLICY_MARKER = "----- marketplace agent worker policy -----"
RESULT_SCHEMA_ID = "github.copilot.agent-task-result"
CANDIDATE_RESULT_SCHEMA_VERSION = 5
CANDIDATE_MANIFEST_SCHEMA = {
    "id": "github.copilot.agent-task-candidate-manifest",
    "version": 1,
}
MARKETPLACE_CODE_CANDIDATE_POLICY_ID = "marketplace-agent-code-candidate-worker"
MARKETPLACE_CODE_CANDIDATE_POLICY_VERSION = 1
MARKETPLACE_CODE_CANDIDATE_POLICY_SPEC = {
    "id": MARKETPLACE_CODE_CANDIDATE_POLICY_ID,
    "version": MARKETPLACE_CODE_CANDIDATE_POLICY_VERSION,
    "execution_backend": "github-agent-tasks-rest",
    "authentication": "local-gh-api",
    "custom_agent": False,
    "local_fallback": False,
    "mode": "code_candidate",
    "fresh_invocation_only": True,
    "require_exact_task_session_identity": True,
    "require_unchanged_pr_head": True,
    "require_unchanged_local_identity": True,
    "require_linear_generated_history": True,
    "code_commits": "zero-or-more",
    "artifact_commit": "optional-final-path-only",
    "artifact_directory": OUTPUT_DIRECTORY,
    "advisory_report_path": OUTPUT_REPORT_PATH,
    "human_report": "optional-inert-advisory",
    "require_safe_paths": True,
    "dispatcher_candidate_manifest": True,
    "candidate_manifest_schema": CANDIDATE_MANIFEST_SCHEMA,
    "result_schema": {
        "id": RESULT_SCHEMA_ID,
        "version": CANDIDATE_RESULT_SCHEMA_VERSION,
    },
    "completion_evidence": [
        "session-id",
        "actual-model",
        "prompt-sha256",
        "task-session-timestamps",
        "repository-owner",
        "base-generated-refs",
        "raw-task-response-sha256",
    ],
    "application": "forbidden",
}
MARKETPLACE_CODE_CANDIDATE_POLICY_HASH = hashlib.sha256(
    json.dumps(
        MARKETPLACE_CODE_CANDIDATE_POLICY_SPEC,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
).hexdigest()
MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR = (
    f"{MARKETPLACE_CODE_CANDIDATE_POLICY_ID}@"
    f"{MARKETPLACE_CODE_CANDIDATE_POLICY_VERSION}"
)
MARKETPLACE_REPORT_RECOMMENDATION_POLICY_ID = (
    "marketplace-agent-report-recommendation-worker"
)
MARKETPLACE_REPORT_RECOMMENDATION_POLICY_VERSION = 1
MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SPEC = {
    "id": MARKETPLACE_REPORT_RECOMMENDATION_POLICY_ID,
    "version": MARKETPLACE_REPORT_RECOMMENDATION_POLICY_VERSION,
    "execution_backend": "github-agent-tasks-rest",
    "authentication": "local-gh-api",
    "custom_agent": False,
    "local_fallback": False,
    "mode": "report_recommendation",
    "fresh_invocation_only": True,
    "require_exact_task_session_identity": True,
    "require_unchanged_pr_head": True,
    "require_unchanged_local_identity": True,
    "require_linear_generated_history": True,
    "code_commits": "forbidden",
    "artifact_commit": "required-final-path-only",
    "artifact_directory": OUTPUT_DIRECTORY,
    "advisory_report_path": OUTPUT_REPORT_PATH,
    "contents": "inert",
    "require_safe_paths": True,
    "dispatcher_candidate_manifest": True,
    "candidate_manifest_schema": CANDIDATE_MANIFEST_SCHEMA,
    "result_schema": {
        "id": RESULT_SCHEMA_ID,
        "version": CANDIDATE_RESULT_SCHEMA_VERSION,
    },
    "completion_evidence": [
        "session-id",
        "actual-model",
        "prompt-sha256",
        "task-session-timestamps",
        "repository-owner",
        "base-generated-refs",
        "raw-task-response-sha256",
    ],
    "application": "forbidden",
}
MARKETPLACE_REPORT_RECOMMENDATION_POLICY_HASH = hashlib.sha256(
    json.dumps(
        MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SPEC,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
).hexdigest()
MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR = (
    f"{MARKETPLACE_REPORT_RECOMMENDATION_POLICY_ID}@"
    f"{MARKETPLACE_REPORT_RECOMMENDATION_POLICY_VERSION}"
)
CANDIDATE_POLICY_SELECTORS = {
    MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR,
    MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR,
}
REMOVED_OPTIONS = {
    "--dispatch-only",
    "--monitor-only",
    "--resume-apply-with-report",
    "--task-id",
    "--request-id",
    "--worker-receipt",
    "--semantic-kind",
    "--input-result-file",
}

class CloudError(RuntimeError):
    """A user-actionable failure."""

    def __init__(self, message: str, code: str = "cloud_error"):
        super().__init__(message)
        self.code = code

class TransientApiError(RuntimeError):
    """A GitHub request that may succeed when retried."""

    def __init__(self, message: str, retry_after: str | None = None):
        super().__init__(message)
        self.retry_after = retry_after

class RateLimitApiError(TransientApiError):
    """A GitHub rate limit that may succeed after its reset window."""

    def __init__(
        self,
        message: str,
        retry_after: str | None = None,
        rate_limit_reset: str | None = None,
    ):
        super().__init__(message, retry_after)
        self.rate_limit_reset = rate_limit_reset

@dataclass(frozen=True)
class Options:
    report: bool
    model: str
    prompt: str
    pull_request: PrReference | None = None
    apply_with_report: bool = False
    result_file: Path | None = None
    policy: str | None = None
    allow_merged_pr: bool = False
    prompt_file: Path | None = None


@dataclass(frozen=True)
class PrReference:
    number: int
    repository: str | None
    display: str


@dataclass(frozen=True)
class PullRequestSnapshot:
    number: int
    url: str
    state: str
    base_repository: str
    base_ref: str
    base_sha: str
    head_repository: str
    head_ref: str
    head_sha: str
    cross_repository: bool


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: str


@dataclass(frozen=True)
class WorktreeSnapshot:
    root: Path
    repository: str
    remote: str
    branch: str | None
    head: str


@dataclass(frozen=True)
class BaseSnapshot:
    branch: str
    sha: str


@dataclass(frozen=True)
class GeneratedRefs:
    head: str
    base: str | None


@dataclass(frozen=True)
class PrTrackingRefs:
    default: str
    head: str


@dataclass(frozen=True)
class CandidateHistory:
    code_head: str
    code_commits: tuple[Mapping[str, object], ...]
    artifact_commit: Mapping[str, object] | None


@dataclass(frozen=True)
class LocalIdentity:
    branch: str | None
    head: str
    status: str
    operation: str | None


@dataclass
class ResultEnvelope:
    mode: str = "unknown"
    requested_model: str | None = None
    repository: str | None = None
    pull_request: PullRequestSnapshot | None = None
    policy: Mapping[str, object] | None = None
    task_id: str | None = None
    task_url: str | None = None
    task_state: str | None = None
    task_base_ref: str | None = None
    task_base_sha: str | None = None
    generated_branch: str | None = None
    generated_head: str | None = None
    cloud_commits: list[str] | None = None
    application_status: str = "not_started"
    final_local_head: str | None = None
    structural_complete: bool = False
    candidate_manifest: Mapping[str, object] | None = None
    completion_evidence: Mapping[str, object] | None = None
    status: str = "error"
    error_code: str | None = None
    error_message: str | None = None

    def as_dict(self) -> dict[str, object]:
        pull_request = None
        if self.pull_request is not None:
            pull_request = {
                name: getattr(self.pull_request, name)
                for name in (
                    "number",
                    "url",
                    "base_repository",
                    "base_ref",
                    "base_sha",
                    "head_repository",
                    "head_ref",
                    "head_sha",
                )
            }
        return {
            "schema": {
                "id": RESULT_SCHEMA_ID,
                "version": CANDIDATE_RESULT_SCHEMA_VERSION,
            },
            "status": self.status,
            "mode": self.mode,
            "repository": (
                {"name_with_owner": self.repository}
                if self.repository is not None
                else None
            ),
            "pull_request": pull_request,
            "requested_model": self.requested_model,
            "policy": self.policy,
            "task": {
                "id": self.task_id,
                "url": self.task_url,
                "state": self.task_state,
                "base_ref": self.task_base_ref,
                "base_sha": self.task_base_sha,
            },
            "generated": {
                "branch": self.generated_branch,
                "head_sha": self.generated_head,
                "commits": self.cloud_commits or [],
            },
            "application": {
                "status": self.application_status,
                "final_local_head": self.final_local_head,
            },
            "report": None,
            "attestation": {
                "kind": "dispatcher_candidate",
                "structural_complete": self.structural_complete,
            },
            "candidate": self.candidate_manifest,
            "completion": self.completion_evidence,
            "error": (
                {
                    "code": self.error_code,
                    "message": self.error_message,
                }
                if self.error_code is not None
                else None
            ),
        }


@dataclass
class Progress:
    task_id: str | None = None
    last_state: str | None = None


Runner = Callable[..., subprocess.CompletedProcess[str]]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]
UuidFactory = Callable[[], object]

def parse_args(args: Sequence[str]) -> Options:
    report = False
    apply_with_report = False
    allow_merged_pr = False
    model_alias = "sol"
    prompt_file: Path | None = None
    pull_request: PrReference | None = None
    result_file: Path | None = None
    policy: str | None = None
    prompt_start: int | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            prompt_start = index + 1
            break
        if not token.startswith("-"):
            prompt_start = index
            break
        if token in REMOVED_OPTIONS:
            raise CloudError(
                f"{token} was removed; start a fresh candidate invocation",
                "compatibility_removed",
            )
        if token == "--report":
            report = True
            index += 1
            continue
        if token == "--apply-with-report":
            apply_with_report = True
            index += 1
            continue
        if token == "--allow-merged-pr":
            allow_merged_pr = True
            index += 1
            continue
        if token in {"--model", "--prompt-file", "--pr", "--result-file", "--policy"}:
            if index + 1 >= len(args):
                raise CloudError(f"{token} requires a value")
            value = args[index + 1]
            if token == "--model":
                if value not in MODEL_IDS:
                    raise CloudError(
                        f"unsupported model {value!r}; choose luna, terra, sol, or astra"
                    )
                model_alias = value
            elif token == "--prompt-file":
                if prompt_file is not None:
                    raise CloudError("--prompt-file may be specified only once")
                prompt_file = Path(value)
                if not prompt_file.is_absolute():
                    raise CloudError("--prompt-file requires an absolute path")
            elif token == "--pr":
                if pull_request is not None:
                    raise CloudError("--pr may be specified only once")
                pull_request = parse_pr_reference(value)
            elif token == "--result-file":
                if result_file is not None:
                    raise CloudError(
                        "--result-file may be specified only once",
                        "result_file_invalid",
                    )
                result_file = Path(value)
                if not result_file.is_absolute():
                    raise CloudError(
                        "--result-file requires an absolute path",
                        "result_file_invalid",
                    )
            else:
                if policy is not None:
                    raise CloudError(
                        "--policy may be specified only once",
                        "policy_rejected",
                    )
                policy = value
            index += 2
            continue
        raise CloudError(f"unknown option: {token}")

    if prompt_start is None:
        prompt_start = len(args)
    inline_prompt = " ".join(args[prompt_start:]).strip()
    if prompt_file is not None and inline_prompt:
        raise CloudError("--prompt-file cannot be combined with an inline prompt")
    prompt = read_prompt_file(prompt_file) if prompt_file is not None else inline_prompt
    if not prompt:
        raise CloudError("a prompt is required")
    if policy not in CANDIDATE_POLICY_SELECTORS:
        expected = ", ".join(sorted(CANDIDATE_POLICY_SELECTORS))
        raise CloudError(
            f"unknown policy {policy!r}; expected one of {expected}",
            "policy_unknown",
        )
    if pull_request is None or prompt_file is None or result_file is None:
        raise CloudError(
            "candidate policies require --pr, --prompt-file, and --result-file",
            "policy_rejected",
        )
    if policy == MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR:
        if not apply_with_report or report:
            raise CloudError(
                f"{policy} requires --apply-with-report",
                "policy_rejected",
            )
    elif not report or apply_with_report:
        raise CloudError(
            f"{policy} requires --report",
            "policy_rejected",
        )
    if allow_merged_pr and policy != MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR:
        raise CloudError(
            "--allow-merged-pr is valid only for code candidates",
            "policy_rejected",
        )
    return Options(
        report=report,
        model=MODEL_IDS[model_alias],
        prompt=prompt,
        pull_request=pull_request,
        apply_with_report=apply_with_report,
        result_file=result_file,
        policy=policy,
        allow_merged_pr=allow_merged_pr,
        prompt_file=prompt_file,
    )

def parse_pr_reference(value: str) -> PrReference:
    if re.fullmatch(r"[1-9][0-9]*", value):
        return PrReference(int(value), None, value)
    match = re.fullmatch(
        r"https://github\.com/([^/\s]+)/([^/#\s]+)/pull/([1-9][0-9]*)/?",
        value,
        re.IGNORECASE,
    )
    if match:
        repository = f"{match.group(1)}/{match.group(2)}"
        return PrReference(int(match.group(3)), repository, value)
    match = re.fullmatch(
        r"([^/#\s]+)/([^/#\s]+)#([1-9][0-9]*)",
        value,
    )
    if match:
        repository = f"{match.group(1)}/{match.group(2)}"
        return PrReference(int(match.group(3)), repository, value)
    raise CloudError(
        f"invalid --pr value {value!r}; use a GitHub PR URL, number, or owner/repo#number"
    )

def read_prompt_file(value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        raise CloudError("--prompt-file requires an absolute path")
    try:
        prompt = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise CloudError(f"prompt file does not exist: {path}") from None
    except PermissionError:
        raise CloudError(f"prompt file is not readable: {path}") from None
    except IsADirectoryError:
        raise CloudError(f"prompt file is not a file: {path}") from None
    except OSError as error:
        raise CloudError(f"could not read prompt file {path}: {error}") from None
    except UnicodeDecodeError:
        raise CloudError(f"prompt file is not valid UTF-8: {path}") from None
    if not prompt.strip():
        raise CloudError(f"prompt file is empty: {path}")
    return prompt

def mode_name(options: Options) -> str:
    if options.policy == MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR:
        return "code_candidate"
    if options.policy == MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR:
        return "report_recommendation"
    raise CloudError("a current candidate policy is required", "policy_required")


def policy_metadata(options: Options) -> dict[str, object]:
    if options.policy == MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR:
        return {
            "id": MARKETPLACE_CODE_CANDIDATE_POLICY_ID,
            "version": MARKETPLACE_CODE_CANDIDATE_POLICY_VERSION,
            "sha256": MARKETPLACE_CODE_CANDIDATE_POLICY_HASH,
        }
    if options.policy == MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR:
        return {
            "id": MARKETPLACE_REPORT_RECOMMENDATION_POLICY_ID,
            "version": MARKETPLACE_REPORT_RECOMMENDATION_POLICY_VERSION,
            "sha256": MARKETPLACE_REPORT_RECOMMENDATION_POLICY_HASH,
        }
    raise CloudError("a current candidate policy is required", "policy_required")


def validate_policy_before_post(
    options: Options,
    root: Path,
    result_path: Path,
) -> None:
    if options.policy not in CANDIDATE_POLICY_SELECTORS:
        raise CloudError(
            "a current candidate policy is required",
            "policy_required",
        )
    resolved_root = root.resolve()
    resolved_parent = result_path.parent.resolve()
    if not resolved_parent.is_dir():
        raise CloudError(
            f"result-file parent does not exist: {result_path.parent}",
            "result_file_invalid",
        )
    try:
        resolved_parent.relative_to(resolved_root)
    except ValueError:
        pass
    else:
        raise CloudError(
            "--result-file must be outside the target repository",
            "policy_rejected",
        )
    if result_path.exists() and result_path.is_dir():
        raise CloudError(
            f"result-file is a directory: {result_path}",
            "result_file_invalid",
        )
    if contains_credentials(options.prompt):
        raise CloudError(
            "the marketplace worker prompt appears to contain credentials",
            "credentials_rejected",
        )
    if options.prompt_file is None:
        raise CloudError("--prompt-file is required", "policy_rejected")
    _require_path_outside_repository(
        resolved_root,
        options.prompt_file,
        "--prompt-file",
    )

def _require_path_outside_repository(
    root: Path,
    path: Path,
    option: str,
) -> None:
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return
    raise CloudError(
        f"{option} must be outside the target repository",
        "policy_rejected",
    )

def contains_credentials(value: str) -> bool:
    sensitive_patterns = (
        r"(?i)\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{16,}\b",
        r"(?i)\b(?:xox[baprs]|sk-[A-Za-z0-9]+)-[A-Za-z0-9-]{12,}\b",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"(?i)\bAuthorization\s*:\s*(?:Bearer|Basic)\s+\S+",
        r"(?i)\b(?:password|passwd|token|api[_-]?key|secret)\s*[:=]\s*\S+",
        r"(?i)https?://[^/\s:@]+:[^/\s@]+@",
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    )
    return any(re.search(pattern, value) for pattern in sensitive_patterns)

def result_error_message(value: str) -> str:
    if contains_credentials(value):
        return "operation failed; sensitive detail was omitted from the result"
    return value

def atomic_write_json(path: Path, data: Mapping[str, object]) -> None:
    path = path.resolve()
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    payload = (
        json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise CloudError(
            f"could not write result file {path}: {error}",
            "result_file_write_failed",
        ) from None

def _result_path_from_argv(args: Sequence[str]) -> Path | None:
    for index, token in enumerate(args):
        if token == "--result-file" and index + 1 < len(args):
            path = Path(args[index + 1])
            return path if path.is_absolute() else None
    return None

def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

def run_process(
    runner: Runner,
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if _EXECUTION is not None:
        runner = _EXECUTION.run
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "check": False,
        "cwd": str(cwd) if cwd is not None else None,
    }
    if input_text is not None:
        kwargs["input"] = input_text
    if os.name == "nt":
        kwargs["creationflags"] = _creation_flags()
    try:
        return runner(list(command), **kwargs)
    except UnicodeError:
        raise CloudError(
            f"{command[0]} returned output that is not valid UTF-8",
            "malformed_output",
        ) from None
    except OSError as error:
        raise CloudError(f"could not run {command[0]}: {error}") from None

def _command_error(command: Sequence[str], result: subprocess.CompletedProcess[str]) -> str:
    detail = result.stderr.strip() or result.stdout.strip()
    if detail:
        return f"{' '.join(command)} failed: {detail}"
    return f"{' '.join(command)} failed with exit code {result.returncode}"

class GitRepository:
    def __init__(
        self,
        runner: Runner = subprocess.run,
        path_exists: Callable[[Path], bool] = Path.exists,
    ):
        self.runner = runner
        self.path_exists = path_exists

    def _run(
        self,
        root: Path | None,
        *args: str,
        allowed: set[int] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = ["git", *args]
        result = run_process(self.runner, command, cwd=root)
        accepted = allowed if allowed is not None else {0}
        if result.returncode not in accepted:
            raise CloudError(_command_error(command, result))
        return result

    def root(self, cwd: Path) -> Path:
        result = self._run(cwd, "rev-parse", "--show-toplevel")
        value = result.stdout.strip()
        if not value:
            raise CloudError("git did not return a repository root")
        return Path(value).resolve()

    def repository_name(self, root: Path) -> str:
        command = ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
        result = run_process(self.runner, command, cwd=root)
        if result.returncode != 0:
            raise CloudError(_command_error(command, result))
        value = result.stdout.strip()
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", value):
            raise CloudError("gh did not resolve the current repository as owner/repo")
        return value

    def matching_remote(self, root: Path, repository: str) -> str:
        remotes = self._run(root, "remote").stdout.splitlines()
        for remote in remotes:
            remote = remote.strip()
            if not remote:
                continue
            result = self._run(
                root, "remote", "get-url", remote, allowed={0, 2, 128}
            )
            if result.returncode == 0 and _repository_from_url(
                result.stdout.strip()
            ).casefold() == repository.casefold():
                return remote
        raise CloudError(
            f"no git remote in this worktree points to https://github.com/{repository}"
        )

    def branch(self, root: Path, *, allow_detached: bool = False) -> str | None:
        result = self._run(root, "symbolic-ref", "--quiet", "--short", "HEAD", allowed={0, 1})
        branch = result.stdout.strip()
        if result.returncode == 1 and allow_detached:
            return None
        if result.returncode != 0 or not branch:
            raise CloudError("code mode requires a checked-out local branch")
        return branch

    def head(self, root: Path) -> str:
        value = self._run(root, "rev-parse", "--verify", "HEAD").stdout.strip()
        if not SHA_PATTERN.fullmatch(value):
            raise CloudError("git returned an invalid HEAD commit")
        return value.lower()

    def ref_sha(self, root: Path, ref: str) -> str:
        value = self._run(root, "rev-parse", "--verify", ref).stdout.strip()
        if not SHA_PATTERN.fullmatch(value):
            raise CloudError(
                f"git returned an invalid commit for {ref}",
                "malformed_history",
            )
        return value.lower()

    def identity(self, root: Path) -> LocalIdentity:
        branch_result = self._run(
            root,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            allowed={0, 1},
        )
        branch = (
            branch_result.stdout.strip()
            if branch_result.returncode == 0 and branch_result.stdout.strip()
            else None
        )
        status = self._run(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
        ).stdout
        return LocalIdentity(branch, self.head(root), status, self.operation(root))

    def require_identity_unchanged(
        self, root: Path, expected: LocalIdentity
    ) -> None:
        current = self.identity(root)
        if current != expected:
            raise CloudError(
                "the local repository changed after marketplace policy "
                "validation; refusing local mutation",
                "local_drift",
            )

    def require_clean(self, root: Path) -> None:
        status = self._run(
            root, "status", "--porcelain=v1", "--untracked-files=normal"
        ).stdout
        if status:
            raise CloudError("code mode requires a clean worktree, including untracked files")

    def operation(self, root: Path) -> str | None:
        markers = {
            "MERGE_HEAD": "merge",
            "CHERRY_PICK_HEAD": "cherry-pick",
            "REVERT_HEAD": "revert",
        }
        for marker, name in markers.items():
            result = self._run(
                root, "rev-parse", "--verify", "--quiet", marker, allowed={0, 1}
            )
            if result.returncode == 0:
                return name
        for marker, name in (
            ("rebase-merge", "rebase"),
            ("rebase-apply", "rebase"),
            ("sequencer", "cherry-pick or revert"),
        ):
            path = self._run(root, "rev-parse", "--git-path", marker).stdout.strip()
            if path and self.path_exists(_resolve_git_path(root, path)):
                return name
        return None

    def require_no_operation(self, root: Path) -> None:
        operation = self.operation(root)
        if operation:
            raise CloudError(f"code mode cannot run during an in-progress {operation}")

    def snapshot(
        self, cwd: Path, *, allow_detached: bool = False
    ) -> WorktreeSnapshot:
        root = self.root(cwd)
        repository = self.repository_name(root)
        remote = self.matching_remote(root, repository)
        self.require_clean(root)
        self.require_no_operation(root)
        return WorktreeSnapshot(
            root=root,
            repository=repository,
            remote=remote,
            branch=self.branch(root, allow_detached=allow_detached),
            head=self.head(root),
        )

    def require_unchanged(self, snapshot: WorktreeSnapshot) -> None:
        self.require_clean(snapshot.root)
        self.require_no_operation(snapshot.root)
        branch = self.branch(
            snapshot.root, allow_detached=snapshot.branch is None
        )
        head = self.head(snapshot.root)
        if branch != snapshot.branch:
            raise CloudError(
                f"the worktree moved from branch {snapshot.branch} to {branch}; "
                "the cloud result was not applied",
                "local_drift",
            )
        if head != snapshot.head:
            raise CloudError(
                f"the worktree HEAD moved from {snapshot.head} to {head}; "
                "the cloud result was not applied",
                "local_drift",
            )

    def require_historical_unchanged(
        self,
        snapshot: WorktreeSnapshot,
        allowed_heads: set[str],
    ) -> None:
        self.require_clean(snapshot.root)
        self.require_no_operation(snapshot.root)
        repository = self.repository_name(snapshot.root)
        if repository != snapshot.repository:
            raise CloudError(
                "the authenticated repository changed during historical audit",
                "local_drift",
            )
        remote = self.matching_remote(snapshot.root, repository)
        if remote != snapshot.remote:
            raise CloudError(
                "the matching repository remote changed during historical audit",
                "local_drift",
            )
        branch = self.branch(snapshot.root)
        head = self.head(snapshot.root)
        if branch != snapshot.branch or head not in allowed_heads:
            raise CloudError(
                "the historical audit branch or HEAD changed",
                "local_drift",
            )

    def _require_valid_branch(self, root: Path, branch: str, description: str) -> None:
        result = self._run(
            root,
            "check-ref-format",
            f"refs/heads/{branch}",
            allowed={0, 1},
        )
        if result.returncode != 0:
            raise CloudError(f"GitHub returned an invalid {description} branch: {branch!r}")

    def fetch_pr_inputs(
        self,
        snapshot: WorktreeSnapshot,
        default_branch: str,
        pull_request: PullRequestSnapshot,
        request_id: str,
    ) -> PrTrackingRefs:
        self._require_valid_branch(snapshot.root, default_branch, "default")
        self._require_valid_branch(
            snapshot.root, pull_request.head_ref, "pull request"
        )
        prefix = f"refs/cloud-agent-tasks/{request_id}"
        refs = PrTrackingRefs(f"{prefix}/default", f"{prefix}/pr-head")
        source_head = (
            f"refs/pull/{pull_request.number}/head"
            if pull_request.cross_repository
            else f"refs/heads/{pull_request.head_ref}"
        )
        self._run(
            snapshot.root,
            "fetch",
            "--no-tags",
            snapshot.remote,
            f"+refs/heads/{default_branch}:{refs.default}",
            f"+{source_head}:{refs.head}",
        )
        fetched_head = self._run(
            snapshot.root, "rev-parse", "--verify", refs.head
        ).stdout.strip().lower()
        if fetched_head != pull_request.head_sha:
            raise CloudError(
                f"pull request #{pull_request.number} moved while it was fetched: "
                f"GitHub reported {pull_request.head_sha}, but git fetched {fetched_head}"
            )
        return refs

    def verify_fork_head(
        self,
        root: Path,
        repository: str,
        pull_request: PullRequestSnapshot,
    ) -> None:
        if not pull_request.cross_repository:
            return
        remote = self.matching_remote(root, repository)
        pull_ref = f"refs/pull/{pull_request.number}/head"
        output = self._run(root, "ls-remote", remote, pull_ref).stdout
        refs: dict[str, str] = {}
        for line in output.splitlines():
            sha, separator, name = line.partition("\t")
            if separator:
                refs[name] = sha.lower()
        if refs.get(pull_ref) != pull_request.head_sha:
            raise CloudError(
                f"upstream {pull_ref} does not match pull request "
                f"#{pull_request.number} head {pull_request.head_sha}"
            )

    def commits_outside_default(
        self, snapshot: WorktreeSnapshot, default_ref: str
    ) -> list[str]:
        output = self._run(
            snapshot.root,
            "rev-list",
            "--reverse",
            snapshot.head,
            f"^{default_ref}",
        ).stdout
        commits = [line.strip().lower() for line in output.splitlines() if line.strip()]
        if any(not SHA_PATTERN.fullmatch(commit) for commit in commits):
            raise CloudError("git returned an invalid local commit")
        return commits

    def align_to_pr(
        self,
        snapshot: WorktreeSnapshot,
        pull_request: PullRequestSnapshot,
        refs: PrTrackingRefs,
    ) -> WorktreeSnapshot:
        if snapshot.head == pull_request.head_sha:
            return snapshot
        if snapshot.branch is None:
            raise CloudError(
                "a detached code candidate checkout must already match the "
                f"pull request head {pull_request.head_sha}; "
                "the local checkout was not changed",
                "local_drift",
            )
        local_commits = self.commits_outside_default(snapshot, refs.default)
        if local_commits:
            raise CloudError(
                "the current branch has commits outside the fetched default branch; "
                f"refusing to align it to PR #{pull_request.number}: "
                f"{', '.join(local_commits)}"
            )
        can_fast_forward = self._run(
            snapshot.root,
            "merge-base",
            "--is-ancestor",
            snapshot.head,
            refs.head,
            allowed={0, 1},
        ).returncode == 0
        if can_fast_forward:
            self._run(snapshot.root, "merge", "--ff-only", refs.head)
        else:
            self._run(snapshot.root, "reset", "--hard", pull_request.head_sha)
        aligned_head = self.head(snapshot.root)
        if aligned_head != pull_request.head_sha:
            raise CloudError(
                f"local alignment did not reach pull request head {pull_request.head_sha}"
            )
        return WorktreeSnapshot(
            snapshot.root,
            snapshot.repository,
            snapshot.remote,
            snapshot.branch,
            pull_request.head_sha,
        )

    def fetch_generated(
        self, snapshot: WorktreeSnapshot, head_ref: str, request_id: str
    ) -> str:
        branch = _short_branch_ref(head_ref)
        check = self._run(
            snapshot.root,
            "check-ref-format",
            f"refs/heads/{branch}",
            allowed={0, 1},
        )
        if check.returncode != 0:
            raise CloudError(f"GitHub returned an invalid generated branch: {head_ref!r}")
        tracking_ref = f"refs/cloud-agent-tasks/{request_id}/generated"
        self._run(
            snapshot.root,
            "fetch",
            "--no-tags",
            snapshot.remote,
            f"+refs/heads/{branch}:{tracking_ref}",
        )
        return tracking_ref

    def cloud_commits(
        self, root: Path, base_sha: str, tracking_ref: str
    ) -> list[str]:
        ancestry = self._run(
            root,
            "merge-base",
            "--is-ancestor",
            base_sha,
            tracking_ref,
            allowed={0, 1},
        )
        if ancestry.returncode != 0:
            raise CloudError(
                "the recorded base commit is not an ancestor of the generated "
                "branch; refusing to apply it",
                "malformed_history",
            )
        output = self._run(
            root,
            "rev-list",
            "--reverse",
            "--topo-order",
            f"{base_sha}..{tracking_ref}",
        ).stdout
        commits = [line.strip().lower() for line in output.splitlines() if line.strip()]
        if any(not SHA_PATTERN.fullmatch(commit) for commit in commits):
            raise CloudError("git returned an invalid cloud commit", "malformed_history")
        return commits

    def candidate_history(
        self,
        root: Path,
        base_sha: str,
        commits: Sequence[str],
        *,
        report_only: bool,
    ) -> CandidateHistory:
        expected_parent = base_sha
        code_commits: list[Mapping[str, object]] = []
        artifact_commit: Mapping[str, object] | None = None
        for index, commit in enumerate(commits):
            parent_output = self._run(
                root,
                "rev-list",
                "--parents",
                "-n",
                "1",
                commit,
            ).stdout.strip().lower()
            commit_and_parent = parent_output.split()
            if (
                len(commit_and_parent) != 2
                or commit_and_parent[0] != commit
                or commit_and_parent[1] != expected_parent
            ):
                raise CloudError(
                    f"generated commit {commit} is not the next linear "
                    "single-parent commit",
                    "unexpected_commits",
                )
            changed_output = self._run(
                root,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                "--no-renames",
                commit,
            ).stdout
            changed_paths = tuple(
                sorted(path for path in changed_output.split("\0") if path)
            )
            if not changed_paths:
                raise CloudError(
                    f"generated commit {commit} has no changed paths",
                    "unexpected_commits",
                )
            for path in changed_paths:
                _validate_candidate_path(path)
            output_paths = tuple(
                _is_candidate_output_path(path) for path in changed_paths
            )
            if any(output_paths) and not all(output_paths):
                raise CloudError(
                    f"generated commit {commit} mixes candidate code and output "
                    "artifact paths",
                    "unexpected_paths",
                )
            tree = self._run(
                root,
                "show",
                "-s",
                "--format=%T",
                commit,
            ).stdout.strip().lower()
            if not SHA_PATTERN.fullmatch(tree):
                raise CloudError(
                    f"git returned an invalid tree for generated commit {commit}",
                    "malformed_history",
                )
            patch = self._run(
                root,
                "diff-tree",
                "--binary",
                "--full-index",
                "--no-color",
                "--no-renames",
                "--patch",
                "-r",
                expected_parent,
                commit,
            ).stdout
            metadata: Mapping[str, object] = {
                "sha": commit,
                "parent_sha": expected_parent,
                "tree_sha": tree,
                "patch_sha256": hashlib.sha256(
                    patch.encode("utf-8")
                ).hexdigest(),
                "changed_paths": list(changed_paths),
            }
            if all(output_paths):
                if artifact_commit is not None or index != len(commits) - 1:
                    raise CloudError(
                        "the generated history contains multiple or non-final "
                        "output artifact commits",
                        "unexpected_commits",
                    )
                artifact_commit = metadata
            else:
                if artifact_commit is not None:
                    raise CloudError(
                        "the generated history contains code after its output "
                        "artifact commit",
                        "unexpected_commits",
                    )
                code_commits.append(metadata)
            expected_parent = commit
        if report_only:
            if code_commits:
                raise CloudError(
                    "report recommendation policy forbids candidate code commits",
                    "unexpected_commits",
                )
            if artifact_commit is None:
                raise CloudError(
                    "report recommendation policy requires one final output "
                    "artifact commit",
                    "malformed_history",
                )
        code_head = (
            str(code_commits[-1]["sha"]) if code_commits else base_sha
        )
        return CandidateHistory(
            code_head,
            tuple(code_commits),
            artifact_commit,
        )

    def fast_forward(
        self, snapshot: WorktreeSnapshot, tracking_ref: str
    ) -> None:
        command = ["git", "merge", "--ff-only", tracking_ref]
        result = run_process(self.runner, command, cwd=snapshot.root)
        if result.returncode != 0:
            raise CloudError(_command_error(command, result))
        expected = self._run(
            snapshot.root, "rev-parse", "--verify", tracking_ref
        ).stdout.strip().lower()
        actual = self.head(snapshot.root)
        if not SHA_PATTERN.fullmatch(expected) or actual != expected:
            raise CloudError("the local branch did not reach the generated commit")

def _resolve_git_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path

def verify_candidate_result(
    result: Mapping[str, object], *, options: Options,
    pull_request: PullRequestSnapshot, root: Path, git: GitRepository,
) -> dict[str, object]:
    """Recheck dispatcher provenance and fetched history for a candidate consumer."""
    report_only = options.policy == MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR
    if options.policy not in CANDIDATE_POLICY_SELECTORS:
        raise CloudError("consumer requires a candidate policy", "policy_rejected")
    pr = pull_request
    if pr.state != ("MERGED" if options.allow_merged_pr else "OPEN"):
        raise CloudError("candidate source state is not authorized by the caller", "stale_pr_head")
    if options.allow_merged_pr and (
        report_only or git.identity(root).branch != f"trask-pr-audit-{pr.number}"
        or git.head(root) != pr.head_sha
    ):
        raise CloudError("historical candidate source is not the frozen audit branch", "policy_rejected")
    repository = pr.base_repository
    source_ref = pr.head_sha if options.allow_merged_pr or pr.cross_repository else pr.head_ref
    expected_pr = {name: getattr(pr, name) for name in (
        "number", "url", "base_repository", "base_ref", "base_sha",
        "head_repository", "head_ref", "head_sha",
    )}
    if (
        result.get("schema") != {"id": RESULT_SCHEMA_ID, "version": 5}
        or result.get("status") != "success" or result.get("error") is not None
        or result.get("policy") != policy_metadata(options)
        or result.get("mode") != ("report_recommendation" if report_only else "code_candidate")
        or result.get("repository") != {"name_with_owner": repository}
        or result.get("pull_request") != expected_pr
        or result.get("requested_model") != options.model
        or result.get("report") is not None
        or result.get("attestation") != {"kind": "dispatcher_candidate", "structural_complete": True}
        or result.get("application") != {
            "status": "not_applicable" if report_only else "not_applied",
            "final_local_head": git.head(root) if report_only else pr.head_sha,
        }
    ):
        raise CloudError("candidate consumer identity or policy mismatch", "candidate_invalid")
    task, generated, completion = (result.get(name) for name in ("task", "generated", "completion"))
    if (
        not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"]
        or task.get("state") != "completed"
        or task.get("base_ref") != source_ref or task.get("base_sha") != pr.head_sha
        or not isinstance(generated, dict)
        or not isinstance(generated.get("branch"), str) or not generated["branch"]
        or not isinstance(generated.get("head_sha"), str)
        or re.fullmatch(r"[0-9a-f]{40}", generated["head_sha"]) is None
        or not isinstance(completion, dict)
        or set(completion) != {"request", "task", "session", "repository", "refs"}
    ):
        raise CloudError("candidate task completion is malformed", "candidate_invalid")
    submitted = task_payload(options, OUTPUT_REPORT_PATH, pr)["prompt"]
    prompt_hash = hashlib.sha256(str(submitted).encode("utf-8")).hexdigest()
    session, completed_task, repo_identity = (
        completion.get(name) for name in ("session", "task", "repository")
    )
    if (
        completion["request"] != {"requested_model": options.model, "prompt_sha256": prompt_hash}
        or completion["refs"] != {"base": source_ref, "generated": generated["branch"]}
        or not isinstance(session, dict) or not isinstance(session.get("id"), str) or not session["id"]
        or session.get("state") != "completed"
        or not isinstance(session.get("actual_model"), str)
        or session.get("actual_model") not in {options.model, f"sweagent-capi:{options.model}"}
        or session.get("prompt_sha256") != prompt_hash
        or not isinstance(completed_task, dict) or completed_task.get("id") != task["id"]
        or completed_task.get("state") != "completed"
        or not isinstance(completed_task.get("raw_response_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", completed_task["raw_response_sha256"]) is None
        or not isinstance(repo_identity, dict)
        or repo_identity.get("name_with_owner") != repository
        or type(repo_identity.get("id")) is not int or repo_identity["id"] <= 0
        or not isinstance(repo_identity.get("owner"), dict)
        or not isinstance(repo_identity["owner"].get("login"), str)
        or repo_identity["owner"].get("login", "").casefold() != repository.split("/")[0].casefold()
        or type(repo_identity["owner"].get("id")) is not int or repo_identity["owner"]["id"] <= 0
    ):
        raise CloudError("candidate completion identity mismatch", "candidate_invalid")
    for owner in (session, completed_task):
        for field in ("created_at", "updated_at", "completed_at"):
            value = owner.get(field)
            if value is None and field != "created_at":
                continue
            try:
                timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    raise ValueError("timezone missing")
            except ValueError as error:
                raise CloudError("candidate completion timestamp invalid", "candidate_invalid") from error
    commits = git.cloud_commits(root, pr.head_sha, generated["head_sha"])
    history = git.candidate_history(root, pr.head_sha, commits, report_only=report_only)
    manifest = {
        "schema": CANDIDATE_MANIFEST_SCHEMA,
        "repository": {"name_with_owner": repository},
        "task": {"id": task["id"], "session_id": session["id"]},
        "base": {"ref": source_ref, "sha": pr.head_sha},
        "generated": {"ref": generated["branch"], "head_sha": generated["head_sha"],
                      "code_tip_sha": history.code_head},
        "code_commits": list(history.code_commits),
        "artifact_commit": history.artifact_commit,
    }
    code_commits = [entry["sha"] for entry in history.code_commits]
    if result.get("candidate") != manifest or generated.get("commits") != code_commits:
        raise CloudError("candidate manifest differs from fetched history", "candidate_invalid")
    if generated["head_sha"] != (commits[-1] if commits else pr.head_sha):
        raise CloudError("candidate generated tip differs from history", "candidate_invalid")
    return {"task": task, "completion": completion, "candidate": manifest,
            "commits": code_commits, "code_tip": history.code_head,
            "artifact_commit": history.artifact_commit}

def verify_current_candidate(
    result: Mapping[str, object], *, options: Options,
    pull_request: PullRequestSnapshot, root: Path, git: GitRepository,
) -> dict[str, object]:
    """Verify a current version-5 candidate against live Git history."""
    return verify_candidate_result(
        result,
        options=options,
        pull_request=pull_request,
        root=root,
        git=git,
    )


def guarded_fast_forward_candidate(
    result: Mapping[str, object], *, options: Options,
    pull_request: PullRequestSnapshot, root: Path, git: GitRepository,
) -> dict[str, object]:
    """Verify and fast-forward a clean source branch to the candidate code tip."""
    if options.policy != MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR:
        raise CloudError(
            "only code candidates can be imported",
            "policy_rejected",
        )
    verified = verify_current_candidate(
        result,
        options=options,
        pull_request=pull_request,
        root=root,
        git=git,
    )
    snapshot = git.snapshot(root)
    if (
        snapshot.repository.casefold()
        != pull_request.base_repository.casefold()
        or snapshot.head != pull_request.head_sha
    ):
        raise CloudError(
            "candidate import source no longer matches the verified pull request",
            "local_drift",
        )
    code_tip = str(verified["code_tip"])
    git.require_unchanged(snapshot)
    if code_tip != snapshot.head:
        git.fast_forward(snapshot, code_tip)
        git.require_clean(snapshot.root)
        git.require_no_operation(snapshot.root)
        if git.head(snapshot.root) != code_tip:
            raise CloudError(
                "candidate import did not reach the verified code tip",
                "local_drift",
            )
    return {
        **verified,
        "application": "fast_forwarded" if code_tip != snapshot.head else "no_changes",
        "final_local_head": code_tip,
    }

def _validate_candidate_path(path: str) -> None:
    parts = path.split("/")
    reserved_names = {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or ":" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
        or any(part in {"", ".", ".."} for part in parts)
        or any(part.endswith((" ", ".")) for part in parts)
        or any(part.casefold() == ".git" for part in parts)
        or any(part.split(".", 1)[0].casefold() in reserved_names for part in parts)
        or path.casefold() == OUTPUT_DIRECTORY.casefold()
        or (
            path.casefold().startswith(f"{OUTPUT_DIRECTORY.casefold()}/")
            and not path.startswith(f"{OUTPUT_DIRECTORY}/")
        )
    ):
        raise CloudError(
            f"generated history contains unsafe path {path!r}",
            "unsafe_path",
        )

def _is_candidate_output_path(path: str) -> bool:
    return path.startswith(f"{OUTPUT_DIRECTORY}/")

def _repository_from_url(url: str) -> str:
    value = url.strip()
    patterns = (
        r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?\Z",
        r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?\Z",
        r"ssh://git@github\.com/([^/]+/[^/]+?)(?:\.git)?/?\Z",
        r"git://github\.com/([^/]+/[^/]+?)(?:\.git)?/?\Z",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, value, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""

def _short_branch_ref(value: str) -> str:
    prefix = "refs/heads/"
    return value[len(prefix) :] if value.startswith(prefix) else value

def _pr_repository_name(value: object, field: str) -> str:
    if not isinstance(value, dict):
        raise CloudError(f"GitHub returned invalid {field} repository metadata")
    name_with_owner = value.get("nameWithOwner")
    if isinstance(name_with_owner, str) and re.fullmatch(
        r"[^/\s]+/[^/\s]+", name_with_owner
    ):
        return name_with_owner
    name = value.get("name")
    owner = value.get("owner")
    login = owner.get("login") if isinstance(owner, dict) else None
    if (
        isinstance(name, str)
        and name
        and isinstance(login, str)
        and login
        and "/" not in name
        and "/" not in login
    ):
        return f"{login}/{name}"
    raise CloudError(f"GitHub returned invalid {field} repository metadata")

def pull_request_base_tip(
    runner: Runner,
    root: Path,
    repository: str,
    base_ref: str,
) -> str:
    encoded_ref = urllib.parse.quote(base_ref, safe="")
    command = [
        "gh",
        "api",
        f"repos/{repository}/git/ref/heads/{encoded_ref}",
    ]
    result = run_process(runner, command, cwd=root)
    if result.returncode != 0:
        raise CloudError(_command_error(command, result))
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise CloudError(
            f"gh returned invalid base branch JSON: {error.msg}"
        ) from None
    target = data.get("object") if isinstance(data, dict) else None
    sha = target.get("sha") if isinstance(target, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("ref") != f"refs/heads/{base_ref}"
        or not isinstance(sha, str)
        or SHA_PATTERN.fullmatch(sha) is None
    ):
        raise CloudError(
            f"GitHub returned invalid base branch identity for {base_ref!r}"
        )
    return sha.lower()

def resolve_pull_request(
    runner: Runner,
    root: Path,
    repository: str,
    reference: PrReference,
    *,
    allow_merged: bool = False,
) -> PullRequestSnapshot:
    if (
        reference.repository is not None
        and reference.repository.casefold() != repository.casefold()
    ):
        raise CloudError(
            f"{reference.display} belongs to {reference.repository}, not the "
            f"current repository {repository}"
        )
    fields = (
        "number,url,state,baseRefName,baseRefOid,headRepository,"
        "headRepositoryOwner,headRefName,headRefOid,isCrossRepository"
    )
    command = [
        "gh",
        "pr",
        "view",
        str(reference.number),
        "--repo",
        repository,
        "--json",
        fields,
    ]
    result = run_process(runner, command, cwd=root)
    if result.returncode != 0:
        raise CloudError(_command_error(command, result))
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise CloudError(f"gh returned invalid pull request JSON: {error.msg}") from None
    if not isinstance(data, dict):
        raise CloudError("gh returned pull request metadata that is not an object")

    number = data.get("number")
    url = data.get("url")
    state = data.get("state")
    base_ref = data.get("baseRefName")
    reported_base_sha = data.get("baseRefOid")
    head_ref = data.get("headRefName")
    head_sha = data.get("headRefOid")
    cross_repository = data.get("isCrossRepository")
    if number != reference.number:
        raise CloudError(
            f"gh returned pull request #{number!r} while resolving #{reference.number}"
        )
    if not isinstance(url, str) or not url:
        raise CloudError(f"pull request #{number} has no valid URL")
    try:
        resolved_url = parse_pr_reference(url)
    except CloudError:
        raise CloudError(f"pull request #{number} has an invalid URL: {url!r}") from None
    if (
        resolved_url.number != reference.number
        or resolved_url.repository is None
        or resolved_url.repository.casefold() != repository.casefold()
    ):
        raise CloudError(
            f"pull request #{number} returned a URL outside the current repository: {url}"
        )
    expected_state = "MERGED" if allow_merged else "OPEN"
    if state != expected_state:
        rendered_state = state.lower() if isinstance(state, str) else "unknown"
        description = "merged" if allow_merged else "open"
        raise CloudError(
            f"pull request #{number} is {rendered_state}; only {description} "
            "pull requests are supported"
        )
    if not isinstance(cross_repository, bool):
        raise CloudError(f"pull request #{number} has invalid cross-repository metadata")
    base_repository = resolved_url.repository
    head_repository = _pr_repository_name(data.get("headRepository"), "head")
    if base_repository.casefold() != repository.casefold():
        raise CloudError(
            f"pull request #{number} belongs to {base_repository}, not the "
            f"current repository {repository}"
        )
    repositories_differ = head_repository.casefold() != repository.casefold()
    if cross_repository != repositories_differ:
        raise CloudError(
            f"pull request #{number} returned inconsistent cross-repository metadata"
        )
    for name, value in (("base", base_ref), ("head", head_ref)):
        if not isinstance(value, str) or not value:
            raise CloudError(f"pull request #{number} has no valid {name} branch")
        check = run_process(
            runner,
            ["git", "check-ref-format", f"refs/heads/{value}"],
            cwd=root,
        )
        if check.returncode != 0:
            raise CloudError(
                f"pull request #{number} has an invalid {name} branch: {value!r}"
            )
    for name, value in (("base", reported_base_sha), ("head", head_sha)):
        if not isinstance(value, str) or not SHA_PATTERN.fullmatch(value):
            raise CloudError(f"pull request #{number} has an invalid {name} SHA")
    base_sha = pull_request_base_tip(
        runner,
        root,
        repository,
        base_ref,
    )
    return PullRequestSnapshot(
        number,
        url,
        state,
        base_repository,
        base_ref,
        base_sha.lower(),
        head_repository,
        head_ref,
        head_sha.lower(),
        cross_repository,
    )

def require_pr_unchanged(
    original: PullRequestSnapshot,
    current: PullRequestSnapshot,
    *,
    full_identity: bool = False,
    task_completed: bool = False,
) -> None:
    fields = (
        (
            "number",
            "url",
            "state",
            "base_repository",
            "base_ref",
            "base_sha",
            "head_repository",
            "head_ref",
            "head_sha",
            "cross_repository",
        )
        if full_identity
        else ("state", "head_repository", "head_ref", "head_sha")
    )
    changed = [
        field for field in fields
        if getattr(original, field) != getattr(current, field)
    ]
    if changed:
        phase = (
            "no longer matches the frozen source at post-completion validation; "
            "refusing to accept generated work"
            if task_completed
            else "moved during local preparation; the Agent Task was not started"
        )
        detail = "; ".join(
            f"{field} expected={getattr(original, field)!r} "
            f"observed={getattr(current, field)!r}"
            for field in changed
        )
        raise CloudError(
            f"pull request #{original.number} {phase}; {detail}",
            "stale_pr_head",
        )

def validate_policy_before_mutation(
    git: GitRepository,
    identity: LocalIdentity,
    runner: Runner,
    root: Path,
    repository: str,
    reference: PrReference | None,
    pull_request: PullRequestSnapshot | None,
    *,
    allow_merged_pr: bool = False,
) -> None:
    git.require_identity_unchanged(root, identity)
    if reference is None or pull_request is None:
        return
    current = resolve_pull_request(
        runner,
        root,
        repository,
        reference,
        allow_merged=allow_merged_pr,
    )
    try:
        require_pr_unchanged(
            pull_request,
            current,
            full_identity=allow_merged_pr,
            task_completed=True,
        )
    except CloudError as error:
        raise CloudError(str(error), "stale_pr_head") from None

class ApiClient:
    def __init__(
        self,
        runner: Runner = subprocess.run,
        sleep: Sleeper = time.sleep,
        wall_clock: Clock = time.time,
        max_transient_failures: int = MAX_TRANSIENT_FAILURES,
    ):
        self.runner = runner
        self.sleep = sleep
        self.wall_clock = wall_clock
        self.max_transient_failures = max_transient_failures
        self.last_response_sha256: str | None = None

    def request_json(
        self,
        method: str,
        endpoint: str,
        *,
        payload: Mapping[str, object] | None = None,
        expected_status: int,
        operation: str,
    ) -> object:
        for failure_count in range(self.max_transient_failures):
            try:
                return self._request_once(
                    method,
                    endpoint,
                    payload=payload,
                    expected_status=expected_status,
                    operation=operation,
                )
            except RateLimitApiError as error:
                if failure_count + 1 >= self.max_transient_failures:
                    raise CloudError(
                        f"{operation} failed after {self.max_transient_failures} "
                        f"rate-limit attempts: {error}",
                        "api_failure",
                    ) from None
                delay = _rate_limit_retry_delay(
                    error.retry_after,
                    error.rate_limit_reset,
                    failure_count,
                    self.wall_clock(),
                )
                self.sleep(delay)
            except TransientApiError as error:
                if failure_count + 1 >= self.max_transient_failures:
                    raise CloudError(
                        f"{operation} failed after {self.max_transient_failures} "
                        f"transient attempts: {error}",
                        "api_failure",
                    ) from None
                delay = _retry_delay(
                    error.retry_after, failure_count, self.wall_clock()
                )
                self.sleep(delay)
        raise AssertionError("retry loop did not return or raise")

    def _request_once(
        self,
        method: str,
        endpoint: str,
        *,
        payload: Mapping[str, object] | None,
        expected_status: int,
        operation: str,
    ) -> object:
        command = [
            "gh",
            "api",
            "--include",
            "--method",
            method,
            "-H",
            f"Accept: {ACCEPT}",
            "-H",
            f"X-GitHub-Api-Version: {API_VERSION}",
            endpoint,
        ]
        input_text = None
        if payload is not None:
            command.extend(["--input", "-"])
            input_text = json.dumps(payload, ensure_ascii=False)
        result = run_process(self.runner, command, input_text=input_text)
        try:
            response = parse_http_response(result.stdout)
        except CloudError as error:
            if result.returncode != 0:
                detail = result.stderr.strip() or str(error)
                raise TransientApiError(
                    f"{operation} transport failed: {detail}"
                ) from None
            raise

        if response.status == expected_status and result.returncode == 0:
            try:
                data = json.loads(response.body)
            except json.JSONDecodeError as error:
                raise CloudError(
                    f"{operation} returned invalid JSON: {error.msg}",
                    "api_failure",
                ) from None
            self.last_response_sha256 = hashlib.sha256(
                response.body.encode("utf-8")
            ).hexdigest()
            return data
        if response.status == 429 or _is_rate_limited_403(response):
            message = _api_message(response.body)
            raise RateLimitApiError(
                f"GitHub returned HTTP {response.status}"
                + (f": {message}" if message else ""),
                response.headers.get("retry-after"),
                response.headers.get("x-ratelimit-reset"),
            )
        if 500 <= response.status <= 599:
            message = _api_message(response.body)
            raise TransientApiError(
                f"GitHub returned HTTP {response.status}"
                + (f": {message}" if message else ""),
                response.headers.get("retry-after"),
            )
        raise CloudError(_permanent_api_error(operation, response), "api_failure")

def parse_http_response(output: str) -> HttpResponse:
    remainder = output
    response: HttpResponse | None = None
    while remainder.startswith("HTTP/"):
        separators = [
            (index, marker)
            for marker in ("\r\n\r\n", "\n\n")
            if (index := remainder.find(marker)) >= 0
        ]
        if not separators:
            raise CloudError("gh returned response headers without a body")
        separator, marker = min(separators, key=lambda item: item[0])
        header_block = remainder[:separator]
        body = remainder[separator + len(marker) :]
        lines = header_block.splitlines()
        match = re.fullmatch(r"HTTP/\S+\s+(\d{3})(?:\s+.*)?", lines[0])
        if not match:
            raise CloudError("gh returned an invalid HTTP status line")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, separator_text, value = line.partition(":")
            if not separator_text or not name.strip():
                raise CloudError("gh returned malformed HTTP response headers")
            headers[name.strip().lower()] = value.strip()
        response = HttpResponse(int(match.group(1)), headers, body)
        if body.startswith("HTTP/"):
            remainder = body
            continue
        return response
    if response is None:
        raise CloudError("gh did not include an HTTP response status")
    return response

def _api_message(body: str) -> str:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    message = data.get("message")
    return message if isinstance(message, str) else ""

def _is_rate_limited_403(response: HttpResponse) -> bool:
    if response.status != 403:
        return False
    remaining = response.headers.get("x-ratelimit-remaining")
    if remaining is not None:
        try:
            if int(remaining) <= 0:
                return True
        except ValueError:
            pass
    if "retry-after" in response.headers:
        return True
    return "rate limit" in _api_message(response.body).lower()

def _permanent_api_error(operation: str, response: HttpResponse) -> str:
    contexts = {
        400: "the request was rejected as malformed",
        401: "authenticate gh with a user token that can access Agent Tasks",
        403: (
            "check the Copilot subscription, organization policy, and Agent tasks "
            "repository permission"
        ),
        404: "check repository access and whether the task still exists",
        422: "check the selected model, organization policy, and preview API schema",
    }
    context = contexts.get(response.status, "the request cannot be completed")
    message = _api_message(response.body)
    detail = f": {message}" if message else ""
    return f"{operation} failed with HTTP {response.status}{detail}; {context}"

def _parse_retry_after(retry_after: str | None, now: float) -> float | None:
    if not retry_after:
        return None
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(retry_after)
            return max(0.0, parsed.timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return None

def _retry_delay(retry_after: str | None, failure_count: int, now: float) -> float:
    parsed_retry_after = _parse_retry_after(retry_after, now)
    if parsed_retry_after is not None:
        return min(MAX_RETRY_SECONDS, parsed_retry_after)
    return min(MAX_RETRY_SECONDS, float(2**failure_count))

def _rate_limit_retry_delay(
    retry_after: str | None,
    rate_limit_reset: str | None,
    failure_count: int,
    now: float,
) -> float:
    delays = []
    parsed_retry_after = _parse_retry_after(retry_after, now)
    if parsed_retry_after is not None:
        delays.append(parsed_retry_after)
    has_reset = False
    if rate_limit_reset:
        try:
            delays.append(max(0.0, float(rate_limit_reset) - now))
            has_reset = True
        except ValueError:
            pass
    if delays:
        return max(delays) + (
            RATE_LIMIT_SAFETY_MARGIN_SECONDS if has_reset else 0
        )
    return min(MAX_RETRY_SECONDS, float(2**failure_count))

def validate_task(data: object, expected_id: str | None = None) -> dict[str, object]:
    if not isinstance(data, dict):
        raise CloudError("GitHub returned a task response that is not an object")
    task_id = data.get("id")
    state = data.get("state")
    if not isinstance(task_id, str) or not task_id:
        raise CloudError("GitHub returned a task without a valid id")
    if expected_id is not None and task_id != expected_id:
        raise CloudError(
        f"GitHub returned task {task_id!r} while polling task {expected_id!r}",
        "task_identity_mismatch",
    )
    if not isinstance(state, str) or not state:
        raise CloudError(f"task {task_id} has no valid state")
    if state not in KNOWN_STATES:
        raise CloudError(f"task {task_id} returned unknown preview state {state!r}")
    if not isinstance(data.get("created_at"), str) or not data["created_at"]:
        raise CloudError(f"task {task_id} has no valid created_at timestamp")

    artifacts = data.get("artifacts")
    if artifacts is not None:
        if not isinstance(artifacts, list):
            raise CloudError(f"task {task_id} has an invalid artifacts field")
        for artifact in artifacts:
            _validate_artifact(task_id, artifact)
    sessions = data.get("sessions")
    if sessions is not None:
        if not isinstance(sessions, list):
            raise CloudError(f"task {task_id} has an invalid sessions field")
        for session in sessions:
            _validate_session(task_id, session)
    for field in ("url", "html_url"):
        if field in data and data[field] is not None and not isinstance(data[field], str):
            raise CloudError(f"task {task_id} has an invalid {field} field")
    return data

def _validate_artifact(task_id: str, artifact: object) -> None:
    if not isinstance(artifact, dict):
        raise CloudError(f"task {task_id} has an artifact that is not an object")
    provider = artifact.get("provider")
    artifact_type = artifact.get("type")
    data = artifact.get("data")
    if provider != "github" or artifact_type not in {"pull", "branch"}:
        raise CloudError(f"task {task_id} has an unsupported artifact")
    if not isinstance(data, dict):
        raise CloudError(f"task {task_id} has an artifact without valid data")
    if artifact_type == "branch":
        if not isinstance(data.get("head_ref"), str) or not data["head_ref"]:
            raise CloudError(f"task {task_id} has a branch artifact without head_ref")
        if not isinstance(data.get("base_ref"), str) or not data["base_ref"]:
            raise CloudError(f"task {task_id} has a branch artifact without base_ref")
    artifact_id = data.get("id")
    if artifact_type == "pull" and (
        not isinstance(artifact_id, int) or isinstance(artifact_id, bool)
    ):
        raise CloudError(f"task {task_id} has a pull artifact without a valid id")

def _validate_session(task_id: str, session: object) -> None:
    if not isinstance(session, dict):
        raise CloudError(f"task {task_id} has a session that is not an object")
    session_id = session.get("id")
    state = session.get("state")
    if not isinstance(session_id, str) or not session_id:
        raise CloudError(f"task {task_id} has a session without a valid id")
    if not isinstance(state, str) or state not in KNOWN_STATES:
        raise CloudError(f"session {session_id} has an invalid state")
    if not isinstance(session.get("created_at"), str) or not session["created_at"]:
        raise CloudError(f"session {session_id} has no valid created_at timestamp")
    usage = session.get("usage")
    if usage is not None:
        if (
            not isinstance(usage, dict)
            or not isinstance(usage.get("type"), str)
            or not isinstance(usage.get("amount"), (int, float))
            or isinstance(usage.get("amount"), bool)
        ):
            raise CloudError(f"session {session_id} has invalid usage")
    error = session.get("error")
    if error is not None:
        if not isinstance(error, dict):
            raise CloudError(f"session {session_id} has an invalid error")
        if "message" in error and not isinstance(error["message"], str):
            raise CloudError(f"session {session_id} has an invalid error message")
    for field in ("head_ref", "base_ref", "html_url"):
        if field in session and session[field] is not None and not isinstance(
            session[field], str
        ):
            raise CloudError(f"session {session_id} has an invalid {field}")

def repository_base(api: ApiClient, repository: str) -> BaseSnapshot:
    encoded_repo = "/".join(urllib.parse.quote(part, safe="") for part in repository.split("/"))
    repository_data = api.request_json(
        "GET",
        f"repos/{encoded_repo}",
        expected_status=200,
        operation=f"resolve {repository}",
    )
    if not isinstance(repository_data, dict):
        raise CloudError("GitHub returned invalid repository metadata")
    default_branch = repository_data.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise CloudError("GitHub returned no valid default branch")
    encoded_branch = urllib.parse.quote(default_branch, safe="")
    ref_data = api.request_json(
        "GET",
        f"repos/{encoded_repo}/git/ref/heads/{encoded_branch}",
        expected_status=200,
        operation=f"resolve {repository}'s default branch",
    )
    if not isinstance(ref_data, dict) or not isinstance(ref_data.get("object"), dict):
        raise CloudError("GitHub returned invalid default-branch metadata")
    sha = ref_data["object"].get("sha")
    if not isinstance(sha, str) or not SHA_PATTERN.fullmatch(sha):
        raise CloudError("GitHub returned an invalid default-branch commit")
    return BaseSnapshot(default_branch, sha.lower())

def build_candidate_policy_prompt(
    prompt: str,
    *,
    policy: str,
) -> str:
    if policy == MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR:
        policy_hash = MARKETPLACE_CODE_CANDIDATE_POLICY_HASH
        history_instruction = (
            "Put substantive code, test, documentation, or configuration changes "
            "in zero or more linear single-parent commits. You may then create one "
            "final single-parent artifact commit whose changed paths are all under "
            f"`{OUTPUT_DIRECTORY}/`. The artifact commit is optional. "
        )
    elif policy == MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR:
        policy_hash = MARKETPLACE_REPORT_RECOMMENDATION_POLICY_HASH
        history_instruction = (
            "Do not create code, test, documentation, or configuration commits. "
            "Create exactly one final single-parent artifact commit directly on "
            "the task base. Every changed path must be under "
            f"`{OUTPUT_DIRECTORY}/`. "
        )
    else:
        raise CloudError(f"unsupported candidate policy {policy!r}")
    return (
        f"{prompt.rstrip()}\n\n"
        f"{POLICY_MARKER}\n"
        f"Policy: {policy}\n"
        f"Policy SHA-256: {policy_hash}\n"
        "Authentication and all request, repository, source, model, policy, task, "
        "session, generated-history, candidate-manifest, and completion identities "
        "belong only to the dispatcher. Do not echo or reconstruct them. Do not "
        "request, read, print, persist, or transmit credentials, tokens, keys, "
        "cookies, or authorization headers. Do not select or invoke a custom_agent. "
        "Do not use a local-execution fallback.\n"
        f"{history_instruction}"
        "Do not mix output-directory paths with other paths in one commit, create "
        "more than one output commit, or add commits after the output commit. "
        f"`{OUTPUT_REPORT_PATH}` is optional free-form advisory Markdown. Its "
        "presence, syntax, and contents are never mechanical validation evidence. "
        "Do not write commit SHAs or claim dispatcher attestation. The dispatcher "
        "binds the task and its single completed session to the fetched base and "
        "generated refs, derives exact commit parents, trees, patch digests, and "
        "changed paths, and writes the versioned candidate manifest. It never "
        "imports or applies candidate commits.\n"
        f"{POLICY_MARKER}"
    )

def build_pr_prompt(prompt: str, pull_request: PullRequestSnapshot) -> str:
    return (
        f"{PR_CONTEXT_MARKER}\n"
        f"Source PR: {pull_request.url}\n"
        f"Source PR number: {pull_request.number}\n"
        f"Source base repository: {pull_request.base_repository}\n"
        f"Source base branch: {pull_request.base_ref}\n"
        f"Source head repository: {pull_request.head_repository}\n"
        f"Exact source head branch: {pull_request.head_ref}\n"
        f"Exact source head SHA: {pull_request.head_sha}\n"
        f"{PR_CONTEXT_MARKER}\n\n"
        f"{prompt}"
    )

def task_base_ref(pull_request: PullRequestSnapshot) -> str:
    if pull_request.state == "MERGED" or pull_request.cross_repository:
        return pull_request.head_sha
    return pull_request.head_ref

def render_artifact_paths(
    prompt: str,
    *,
    report_path: str,
    worker_receipt: str | None = None,
    semantic: bool = False,
) -> str:
    if worker_receipt is not None or semantic:
        raise CloudError("legacy candidate artifact parameters were removed")
    rendered = prompt.replace(REPORT_PATH_PLACEHOLDER, report_path)
    if (
        REPORT_PATH_PLACEHOLDER in rendered
        or SEMANTIC_PATH_PLACEHOLDER in rendered
        or VALIDATION_PATH_PLACEHOLDER in rendered
    ):
        raise CloudError(
            "the workflow prompt contains an unsupported artifact placeholder",
            "malformed_report",
        )
    return rendered


def task_payload(
    options: Options,
    report_path: str | None = None,
    pull_request: PullRequestSnapshot | None = None,
    **_removed: object,
) -> dict[str, object]:
    if options.policy not in CANDIDATE_POLICY_SELECTORS:
        raise CloudError("a current candidate policy is required", "policy_required")
    if report_path != OUTPUT_REPORT_PATH:
        raise AssertionError("candidate policy did not use its fixed report path")
    prompt = render_artifact_paths(options.prompt, report_path=report_path)
    if pull_request is not None:
        prompt = build_pr_prompt(prompt, pull_request)
    prompt = build_candidate_policy_prompt(prompt, policy=options.policy)
    payload: dict[str, object] = {
        "prompt": prompt,
        "model": options.model,
        "create_pull_request": False,
    }
    if pull_request is not None:
        payload["base_ref"] = task_base_ref(pull_request)
    return payload

def start_task(
    api: ApiClient,
    repository: str,
    payload: Mapping[str, object],
) -> dict[str, object]:
    data = api.request_json(
        "POST",
        f"agents/repos/{repository}/tasks",
        payload=payload,
        expected_status=201,
        operation="start Agent Task",
    )
    return validate_task(data)

def get_task(api: ApiClient, repository: str, task_id: str) -> dict[str, object]:
    encoded_id = urllib.parse.quote(task_id, safe="")
    data = api.request_json(
        "GET",
        f"agents/repos/{repository}/tasks/{encoded_id}",
        expected_status=200,
        operation=f"poll Agent Task {task_id}",
    )
    return validate_task(data, task_id)

def monitor_task(
    api: ApiClient,
    repository: str,
    initial: dict[str, object],
    progress: Progress,
    sleep: Sleeper,
    report_stopped: Callable[[Mapping[str, object]], None] | None = None,
) -> dict[str, object]:
    current = initial
    progress.task_id = str(current["id"])
    while True:
        state = str(current["state"])
        progress.last_state = state
        if state in SUCCESS_STATES:
            return current
        if state in ERROR_STATES:
            if report_stopped is not None:
                report_stopped(current)
            raise CloudError(f"Agent Task {progress.task_id} ended in state {state}")
        if state in BLOCKED_STATES:
            if report_stopped is not None:
                report_stopped(current)
            raise CloudError(
                f"Agent Task {progress.task_id} is {state}; open the task in GitHub "
                "to provide input or resume it"
            )
        if state not in ACTIVE_STATES:
            raise CloudError(
                f"Agent Task {progress.task_id} returned unknown preview state {state!r}"
            )
        sleep(AGENT_TASK_POLL_INTERVAL_SECONDS)
        current = get_task(api, repository, progress.task_id)

def resolve_generated_refs(task: Mapping[str, object]) -> GeneratedRefs:
    task_id = str(task["id"])
    heads: set[str] = set()
    bases: set[str] = set()
    artifacts = task.get("artifacts") or []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise CloudError(f"task {task_id} has an invalid artifact")
        if artifact.get("type") != "branch":
            continue
        if artifact.get("provider") != "github" or not isinstance(
            artifact.get("data"), dict
        ):
            raise CloudError(f"task {task_id} has an invalid branch artifact")
        data = artifact["data"]
        head = data.get("head_ref")
        base = data.get("base_ref")
        if not isinstance(head, str) or not head:
            raise CloudError(f"task {task_id} has a branch artifact without head_ref")
        if not isinstance(base, str) or not base:
            raise CloudError(f"task {task_id} has a branch artifact without base_ref")
        heads.add(_short_branch_ref(head))
        bases.add(_short_branch_ref(base))

    sessions = task.get("sessions") or []
    for session in sessions:
        if not isinstance(session, dict):
            raise CloudError(f"task {task_id} has an invalid session")
        head = session.get("head_ref")
        base = session.get("base_ref")
        if isinstance(head, str) and head:
            heads.add(_short_branch_ref(head))
        if isinstance(base, str) and base:
            bases.add(_short_branch_ref(base))

    if not heads:
        raise CloudError(f"completed task {task_id} did not return a generated branch")
    if len(heads) != 1:
        raise CloudError(
            f"completed task {task_id} returned conflicting generated branches: "
            f"{', '.join(sorted(heads))}"
        )
    if len(bases) > 1:
        raise CloudError(
            f"completed task {task_id} returned conflicting base branches: "
            f"{', '.join(sorted(bases))}"
        )
    return GeneratedRefs(next(iter(heads)), next(iter(bases)) if bases else None)

def _validated_timestamp(
    value: object,
    *,
    description: str,
    required: bool,
) -> tuple[str | None, datetime | None]:
    if value is None and not required:
        return None, None
    if not isinstance(value, str) or not value:
        raise CloudError(
            f"completed Agent Task has no valid {description} timestamp",
            "task_identity_mismatch",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.utcoffset() is None:
        raise CloudError(
            f"completed Agent Task has an invalid {description} timestamp",
            "task_identity_mismatch",
        )
    return value, parsed

def _identity_object_id(value: object, description: str) -> int:
    identifier = value.get("id") if isinstance(value, dict) else None
    if not isinstance(identifier, int) or isinstance(identifier, bool):
        raise CloudError(
            f"completed Agent Task has invalid {description} identity",
            "task_identity_mismatch",
        )
    return identifier

def validate_fresh_completion(
    task: Mapping[str, object],
    *,
    expected_task_id: str,
    repository: str,
    requested_model: str,
    expected_prompt: str,
    expected_base_ref: str,
    generated_ref: str,
    raw_task_response_sha256: object,
) -> Mapping[str, object]:
    task_id = task.get("id")
    if (
        task_id != expected_task_id
        or not isinstance(task_id, str)
        or not task_id
        or task.get("state") != "completed"
    ):
        raise CloudError(
            "candidate policy requires one freshly completed Agent Task",
            "task_identity_mismatch",
        )
    if (
        not isinstance(raw_task_response_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", raw_task_response_sha256)
    ):
        raise CloudError(
            "completed Agent Task response has no raw response digest",
            "task_identity_mismatch",
        )
    sessions = task.get("sessions")
    if not isinstance(sessions, list) or len(sessions) != 1:
        raise CloudError(
            "candidate policy requires exactly one relevant Agent Task session",
            "task_identity_mismatch",
        )
    session = sessions[0]
    if not isinstance(session, dict):
        raise CloudError(
            "completed Agent Task session identity is malformed",
            "task_identity_mismatch",
        )
    task_repository = task.get("repository")
    task_owner = task.get("owner")
    repository_id = _identity_object_id(task_repository, "repository")
    owner_id = _identity_object_id(task_owner, "owner")
    if session.get("repository") != task_repository or session.get("owner") != task_owner:
        raise CloudError(
            "completed session repository or owner does not match its task",
            "task_identity_mismatch",
        )
    owner_login, separator, _ = repository.partition("/")
    if not separator:
        raise AssertionError("validated repository lost owner/name identity")
    if isinstance(task_repository, dict):
        reported_repository = next(
            (
                task_repository[field]
                for field in ("full_name", "name_with_owner", "nameWithOwner")
                if field in task_repository
            ),
            None,
        )
        if (
            reported_repository is not None
            and (
                not isinstance(reported_repository, str)
                or reported_repository.casefold() != repository.casefold()
            )
        ):
            raise CloudError(
                "completed Agent Task repository does not match the request",
                "task_identity_mismatch",
            )
    if isinstance(task_owner, dict) and task_owner.get("login") is not None:
        login = task_owner.get("login")
        if not isinstance(login, str) or login.casefold() != owner_login.casefold():
            raise CloudError(
                "completed Agent Task owner does not match the request",
                "task_identity_mismatch",
            )
    actual_model = session.get("model")
    if actual_model not in {requested_model, f"sweagent-capi:{requested_model}"}:
        raise CloudError(
            "completed Agent Task session model does not match the request",
            "task_identity_mismatch",
        )
    prompt = session.get("prompt")
    if not isinstance(prompt, str) or prompt != expected_prompt:
        raise CloudError(
            "completed Agent Task session prompt does not match the submitted prompt",
            "task_identity_mismatch",
        )
    session_id = session.get("id")
    if (
        not isinstance(session_id, str)
        or not session_id
        or session.get("task_id") != task_id
        or session.get("state") != "completed"
    ):
        raise CloudError(
            "completed Agent Task session identity or state is invalid",
            "task_identity_mismatch",
        )
    if (
        _short_branch_ref(str(session.get("base_ref", "")))
        != _short_branch_ref(expected_base_ref)
        or _short_branch_ref(str(session.get("head_ref", ""))) != generated_ref
    ):
        raise CloudError(
            "completed Agent Task session base or generated ref does not match",
            "task_identity_mismatch",
        )
    for field, expected in (
        ("base_ref", expected_base_ref),
        ("head_ref", generated_ref),
    ):
        value = task.get(field)
        if value is not None and _short_branch_ref(str(value)) != _short_branch_ref(
            expected
        ):
            raise CloudError(
                f"completed Agent Task {field} does not match",
                "task_identity_mismatch",
            )
    task_created, task_created_at = _validated_timestamp(
        task.get("created_at"),
        description="task created_at",
        required=True,
    )
    task_updated, task_updated_at = _validated_timestamp(
        task.get("updated_at"),
        description="task updated_at",
        required=False,
    )
    task_completed, task_completed_at = _validated_timestamp(
        task.get("completed_at"),
        description="task completed_at",
        required=False,
    )
    session_created, session_created_at = _validated_timestamp(
        session.get("created_at"),
        description="session created_at",
        required=True,
    )
    session_updated, session_updated_at = _validated_timestamp(
        session.get("updated_at"),
        description="session updated_at",
        required=False,
    )
    session_completed, session_completed_at = _validated_timestamp(
        session.get("completed_at"),
        description="session completed_at",
        required=False,
    )
    chronological = (
        (task_created_at, task_updated_at, "task updated_at"),
        (task_created_at, task_completed_at, "task completed_at"),
        (task_created_at, session_created_at, "session created_at"),
        (session_created_at, session_updated_at, "session updated_at"),
        (session_created_at, session_completed_at, "session completed_at"),
    )
    if any(
        start is not None and end is not None and end < start
        for start, end, _ in chronological
    ):
        field = next(
            description
            for start, end, description in chronological
            if start is not None and end is not None and end < start
        )
        raise CloudError(
            f"completed Agent Task has non-chronological {field}",
            "task_identity_mismatch",
        )
    prompt_digest = hashlib.sha256(expected_prompt.encode("utf-8")).hexdigest()
    return {
        "request": {
            "requested_model": requested_model,
            "prompt_sha256": prompt_digest,
        },
        "task": {
            "id": task_id,
            "state": "completed",
            "created_at": task_created,
            "updated_at": task_updated,
            "completed_at": task_completed,
            "raw_response_sha256": raw_task_response_sha256,
        },
        "session": {
            "id": session_id,
            "state": "completed",
            "actual_model": actual_model,
            "created_at": session_created,
            "updated_at": session_updated,
            "completed_at": session_completed,
            "prompt_sha256": prompt_digest,
        },
        "repository": {
            "name_with_owner": repository,
            "id": repository_id,
            "owner": {
                "login": owner_login,
                "id": owner_id,
            },
        },
        "refs": {
            "base": expected_base_ref,
            "generated": generated_ref,
        },
    }

def report_metadata(task: Mapping[str, object], stream: TextIO) -> None:
    task_id = task["id"]
    state = task["state"]
    print(f"Agent Task {task_id}: {state}", file=stream)
    for field in ("html_url", "url"):
        value = task.get(field)
        if isinstance(value, str) and value:
            print(value, file=stream)
    for session in task.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        print(f"Session {session['id']}: {session['state']}", file=stream)
        usage = session.get("usage")
        if isinstance(usage, dict):
            print(f"Usage: {usage['amount']} {usage['type']}", file=stream)
        error = session.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                print(f"Session error: {message}", file=stream)
        link = session.get("html_url")
        if isinstance(link, str) and link:
            print(link, file=stream)
    try:
        refs = resolve_generated_refs(task)
    except CloudError:
        return
    print(f"Generated branch: {refs.head}", file=stream)
    if refs.base:
        print(f"Base branch: {refs.base}", file=stream)

def execute(
    options: Options,
    *,
    cwd: Path,
    runner: Runner = subprocess.run,
    sleep: Sleeper = time.sleep,
    wall_clock: Clock = time.time,
    uuid_factory: UuidFactory = uuid.uuid4,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    path_exists: Callable[[Path], bool] = Path.exists,
    progress: Progress | None = None,
    result: ResultEnvelope | None = None,
) -> int:
    progress = progress or Progress()
    git = GitRepository(runner, path_exists)
    api = ApiClient(runner, sleep, wall_clock)
    report_only = (
        options.policy == MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR
    )
    if options.result_file is None or options.pull_request is None:
        raise AssertionError("candidate invocation lost required paths")
    if result is not None:
        result.mode = mode_name(options)
        result.requested_model = options.model
        result.policy = policy_metadata(options)
        result.application_status = (
            "not_applicable" if report_only else "not_applied"
        )

    snapshot = None if report_only else git.snapshot(cwd, allow_detached=True)
    root = git.root(cwd) if snapshot is None else snapshot.root
    repository = git.repository_name(root) if snapshot is None else snapshot.repository
    if result is not None:
        result.repository = repository
        result.final_local_head = git.head(root)
    validate_policy_before_post(options, root, options.result_file)

    request_id = str(uuid_factory())
    pull_request = resolve_pull_request(
        runner,
        root,
        repository,
        options.pull_request,
        allow_merged=options.allow_merged_pr,
    )
    if options.allow_merged_pr:
        if snapshot is None:
            raise AssertionError("historical candidate lost its worktree snapshot")
        expected_branch = f"trask-pr-audit-{pull_request.number}"
        if snapshot.branch != expected_branch or snapshot.head != pull_request.head_sha:
            raise CloudError(
                "historical candidate requires the frozen audit branch at "
                "the merged pull request head",
                "local_drift",
            )
        refreshed = resolve_pull_request(
            runner,
            root,
            repository,
            options.pull_request,
            allow_merged=True,
        )
        require_pr_unchanged(pull_request, refreshed, full_identity=True)
        pull_request = refreshed
        git.require_historical_unchanged(snapshot, {snapshot.head})
    elif report_only:
        refreshed = resolve_pull_request(
            runner, root, repository, options.pull_request
        )
        require_pr_unchanged(pull_request, refreshed)
        pull_request = refreshed
        git.verify_fork_head(root, repository, pull_request)
    else:
        if snapshot is None:
            raise AssertionError("code candidate lost its worktree snapshot")
        base = repository_base(api, repository)
        tracking_refs = git.fetch_pr_inputs(
            snapshot, base.branch, pull_request, request_id
        )
        git.require_unchanged(snapshot)
        snapshot = git.align_to_pr(snapshot, pull_request, tracking_refs)
        refreshed = resolve_pull_request(
            runner, root, repository, options.pull_request
        )
        require_pr_unchanged(pull_request, refreshed)
        pull_request = refreshed
        git.require_unchanged(snapshot)

    if result is not None:
        result.pull_request = pull_request
    policy_identity = git.identity(root)
    payload = task_payload(options, OUTPUT_REPORT_PATH, pull_request)
    submitted_prompt = payload["prompt"]
    if not isinstance(submitted_prompt, str):
        raise AssertionError("Agent Task payload lost its prompt")
    if _EXECUTION is not None:
        _EXECUTION.record_dispatch(options.result_file, request_id, repository)
    initial = start_task(api, repository, payload)
    if result is not None:
        result.task_id = str(initial["id"])
        result.task_state = str(initial["state"])
        link = initial.get("html_url") or initial.get("url")
        result.task_url = link if isinstance(link, str) and link else None
        result.task_base_ref = task_base_ref(pull_request)
        result.task_base_sha = pull_request.head_sha
        if _EXECUTION is not None:
            _EXECUTION.record_dispatch(
                options.result_file,
                request_id,
                repository,
                {
                    "id": result.task_id,
                    "url": result.task_url,
                    "state": result.task_state,
                },
            )

    final = monitor_task(
        api,
        repository,
        initial,
        progress,
        sleep,
        lambda task: report_metadata(task, stderr),
    )
    report_metadata(final, stderr)
    if result is not None:
        result.task_id = str(final["id"])
        result.task_state = str(final["state"])
        link = final.get("html_url") or final.get("url")
        result.task_url = link if isinstance(link, str) and link else result.task_url
    refs = resolve_generated_refs(final)
    if result is not None:
        result.generated_branch = refs.head
    expected_base = task_base_ref(pull_request)
    if refs.base is None or refs.base != expected_base:
        raise CloudError(
            f"task {final['id']} used base branch {refs.base}, but the "
            f"recorded base was {expected_base}"
        )

    validate_policy_before_mutation(
        git,
        policy_identity,
        runner,
        root,
        repository,
        options.pull_request,
        pull_request,
        allow_merged_pr=options.allow_merged_pr,
    )
    if options.allow_merged_pr:
        if snapshot is None:
            raise AssertionError("historical candidate lost its snapshot")
        git.require_historical_unchanged(snapshot, {snapshot.head})
    if snapshot is None:
        snapshot = WorktreeSnapshot(
            root,
            repository,
            git.matching_remote(root, repository),
            policy_identity.branch,
            policy_identity.head,
        )
    tracking_ref = git.fetch_generated(snapshot, refs.head, request_id)
    generated_head = git.ref_sha(root, tracking_ref)
    all_commits = git.cloud_commits(root, pull_request.head_sha, tracking_ref)
    completion = validate_fresh_completion(
        final,
        expected_task_id=str(initial["id"]),
        repository=repository,
        requested_model=options.model,
        expected_prompt=submitted_prompt,
        expected_base_ref=expected_base,
        generated_ref=refs.head,
        raw_task_response_sha256=getattr(api, "last_response_sha256", None),
    )
    history = git.candidate_history(
        root,
        pull_request.head_sha,
        all_commits,
        report_only=report_only,
    )
    expected_generated_head = (
        all_commits[-1] if all_commits else pull_request.head_sha
    )
    if generated_head != expected_generated_head:
        raise CloudError(
            "fetched generated head does not match the derived candidate history",
            "malformed_history",
        )
    code_commit_shas = [str(commit["sha"]) for commit in history.code_commits]
    manifest = {
        "schema": CANDIDATE_MANIFEST_SCHEMA,
        "repository": {"name_with_owner": repository},
        "task": {
            "id": completion["task"]["id"],
            "session_id": completion["session"]["id"],
        },
        "base": {"ref": expected_base, "sha": pull_request.head_sha},
        "generated": {
            "ref": refs.head,
            "head_sha": generated_head,
            "code_tip_sha": history.code_head,
        },
        "code_commits": list(history.code_commits),
        "artifact_commit": history.artifact_commit,
    }
    if result is not None:
        result.generated_head = generated_head
        result.cloud_commits = code_commit_shas
        result.candidate_manifest = manifest
        result.completion_evidence = completion
        result.structural_complete = True
        result.final_local_head = git.head(root)
        result.status = "success"
    print(
        f"Derived {len(code_commit_shas)} candidate code commit(s) from "
        f"{refs.head} without importing or applying them.",
        file=stderr,
    )
    return 0

def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner = subprocess.run,
    sleep: Sleeper = time.sleep,
    wall_clock: Clock = time.time,
    uuid_factory: UuidFactory = uuid.uuid4,
    cwd: Path | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    path_exists: Callable[[Path], bool] = Path.exists,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    result_path = _result_path_from_argv(args)
    result = ResultEnvelope()
    progress = Progress()
    try:
        options = parse_args(args)
        result_path = options.result_file
        code = execute(
            options,
            cwd=Path.cwd() if cwd is None else cwd,
            runner=runner,
            sleep=sleep,
            wall_clock=wall_clock,
            uuid_factory=uuid_factory,
            stdout=stdout,
            stderr=stderr,
            path_exists=path_exists,
            progress=progress,
            result=result if result_path is not None else None,
        )
    except KeyboardInterrupt:
        result.status = "interrupted"
        result.error_code = "interrupted"
        if progress.task_id:
            state = progress.last_state or "unknown"
            result.task_id = progress.task_id
            result.task_state = state
            result.error_message = (
                f"monitoring stopped; Agent Task {progress.task_id} remains "
                f"remote in state {state}"
            )
            print(
                f"Monitoring stopped. Agent Task {progress.task_id} remains "
                f"remote; last state: {state}.",
                file=stderr,
            )
        else:
            result.error_message = "interrupted before an Agent Task was started"
            print("Interrupted before an Agent Task was started.", file=stderr)
        code = 130
    except CloudError as error:
        result.status = "error"
        result.error_code = error.code
        result.error_message = result_error_message(str(error))
        if progress.task_id is not None:
            result.task_id = progress.task_id
        if progress.last_state is not None:
            result.task_state = progress.last_state
        if result.application_status == "not_started":
            result.application_status = "not_applied"
        print(f"error: {error}", file=stderr)
        code = 2
    except BaseException as error:
        result.status = "error"
        result.error_code = "unexpected_helper_error"
        result.error_message = result_error_message(
            f"{type(error).__name__}: {error}"
        )
        if progress.task_id is not None:
            result.task_id = progress.task_id
        if progress.last_state is not None:
            result.task_state = progress.last_state
        if result.application_status == "not_started":
            result.application_status = "not_applied"
        print(f"error: {result.error_message}", file=stderr)
        code = 2
    if (
        _EXECUTION is not None
        and result_path is not None
        and result.request_id is not None
        and result.repository is not None
        and progress.task_id is not None
    ):
        try:
            _EXECUTION.record_dispatch(
                result_path,
                result.request_id,
                result.repository,
                {
                    "id": progress.task_id,
                    "state": progress.last_state,
                    "url": result.task_url,
                },
            )
        except (OSError, ValueError, RuntimeError) as error:
            result.status = "error"
            result.error_code = "remote_observation_failed"
            result.error_message = result_error_message(str(error))
            result.application_status = "not_applied"
            code = 2
    if result_path is not None:
        try:
            atomic_write_json(result_path, result.as_dict())
        except CloudError as error:
            print(f"error: {error}", file=stderr)
            return 2
    return code


_EXECUTION = None
EXECUTION_SHA256 = "2972a39513197ad0ffbaed94e608f3084d297d3ede86bbe6283df8c711a510c3"

def _load_execution():
    """Load only the pinned shared foreground execution source."""
    import types
    inventory = subprocess.run(
        ["copilot", "skill", "list", "--json"], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        **({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
           if os.name == "nt" else {}),
    )
    matches = [
        entry for entry in json.loads(inventory.stdout)
        if entry.get("name") == "agent-tasks-runtime" and entry.get("source") == "plugin"
        and entry.get("enabled") is True
    ]
    if len(matches) != 1:
        raise RuntimeError("shared execution Runtime is not uniquely installed and enabled")
    root = Path(matches[0]["path"])
    source_path = root / "scripts" / "execution.py"
    if not root.is_absolute() or any(path.is_symlink() for path in (root, source_path.parent, source_path)):
        raise RuntimeError("shared execution Runtime path is invalid")
    source = source_path.read_bytes()
    if hashlib.sha256(source).hexdigest() != EXECUTION_SHA256:
        raise RuntimeError("shared execution Runtime source digest changed")
    module = types.ModuleType("trask_foreground_execution")
    module.__file__ = str(source_path)
    exec(compile(source, str(source_path), "exec", dont_inherit=True), module.__dict__)
    return module

def execution_main():
    if not os.environ.get("TRASK_EXECUTION_PARENT"):
        return main()
    try:
        return _load_execution().controller_main(
            lambda: main(stdout=sys.stdout, stderr=sys.stderr), globals(),
        )
    except BaseException as error:
        result_path = _result_path_from_argv(sys.argv[1:])
        message = result_error_message(f"{type(error).__name__}: {error}")
        if result_path is not None and not result_path.exists():
            result = ResultEnvelope(
                status="error",
                application_status="not_applied",
                error_code="execution_runtime_unavailable",
                error_message=message,
            )
            try:
                atomic_write_json(result_path, result.as_dict())
            except CloudError as write_error:
                print(f"error: {write_error}", file=sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        return 2



if __name__ == "__main__":
    raise SystemExit(execution_main())
