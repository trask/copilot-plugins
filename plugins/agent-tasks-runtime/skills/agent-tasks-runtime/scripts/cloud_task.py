#!/usr/bin/env python3
"""Run a GitHub Agent Task and retrieve its code or committed report."""

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
from dataclasses import dataclass, field
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
REPORT_DIRECTORY = ".github/agent-task-reports"
SEMANTIC_DIRECTORY = ".github/agent-task-semantic"
VALIDATION_DIRECTORY = ".github/agent-task-validations"
OUTPUT_DIRECTORY = ".github/agent-task-output"
OUTPUT_REPORT_PATH = f"{OUTPUT_DIRECTORY}/report.md"
REPORT_PATH_PLACEHOLDER = "{{MARKETPLACE_REPORT_PATH}}"
SEMANTIC_PATH_PLACEHOLDER = "{{MARKETPLACE_SEMANTIC_PATH}}"
VALIDATION_PATH_PLACEHOLDER = "{{MARKETPLACE_VALIDATION_PATH}}"
REPORT_MARKER = "----- /cloud report instructions -----"
APPLY_WITH_REPORT_MARKER = "----- /cloud apply-with-report instructions -----"
PR_CONTEXT_MARKER = "----- /cloud source pull request -----"
POLICY_MARKER = "----- marketplace agent worker policy -----"
RESULT_SCHEMA_ID = "github.copilot.agent-task-result"
RESULT_SCHEMA_VERSION = 1
REPORT_RESULT_SCHEMA_VERSION = 2
LEGACY_SEMANTIC_RESULT_SCHEMA_VERSION = 3
SEMANTIC_RESULT_SCHEMA_VERSION = 4
CANDIDATE_RESULT_SCHEMA_VERSION = 5
CANDIDATE_MANIFEST_SCHEMA = {
    "id": "github.copilot.agent-task-candidate-manifest",
    "version": 1,
}
LEGACY_SEMANTIC_OUTPUT_SCHEMA = {
    "id": "github.copilot.agent-task-semantic-output",
    "version": 1,
}
SEMANTIC_OUTPUT_SCHEMA = {
    "id": "github.copilot.agent-task-semantic-output",
    "version": 2,
}
MARKETPLACE_POLICY_ID = "marketplace-agent-worker"
MARKETPLACE_POLICY_VERSION = 5
MARKETPLACE_POLICY_SPEC = {
    "id": MARKETPLACE_POLICY_ID,
    "version": MARKETPLACE_POLICY_VERSION,
    "execution_backend": "github-agent-tasks-rest",
    "authentication": "local-gh-api",
    "custom_agent": False,
    "local_fallback": False,
    "require_exact_task_identity": True,
    "require_unchanged_pr_head": True,
    "require_unchanged_local_identity": True,
    "require_linear_generated_history": True,
    "require_expected_paths_only": True,
    "require_fix_commit_correlation": True,
    "human_report": "nonempty-utf8-markdown",
    "dispatcher_generated_commit_order": True,
    "require_complete_successful_validation": True,
    "worker_artifact": "remote-validation-command-outcome-array",
    "worker_validation_fields": ["command", "outcome"],
    "dispatcher_attestation": True,
    "worker_identity_echo": False,
}
MARKETPLACE_POLICY_HASH = hashlib.sha256(
    json.dumps(
        MARKETPLACE_POLICY_SPEC,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
).hexdigest()
MARKETPLACE_POLICY_SELECTOR = (
    f"{MARKETPLACE_POLICY_ID}@{MARKETPLACE_POLICY_VERSION}"
)
MARKETPLACE_REPORT_POLICY_ID = "marketplace-agent-report-worker"
MARKETPLACE_REPORT_POLICY_VERSION = 1
MARKETPLACE_REPORT_POLICY_SPEC = {
    "id": MARKETPLACE_REPORT_POLICY_ID,
    "version": MARKETPLACE_REPORT_POLICY_VERSION,
    "execution_backend": "github-agent-tasks-rest",
    "authentication": "local-gh-api",
    "custom_agent": False,
    "local_fallback": False,
    "mode": "report",
    "require_exact_task_identity": True,
    "require_unchanged_pr_head": True,
    "require_unchanged_local_identity": True,
    "require_linear_generated_history": True,
    "require_expected_paths_only": True,
    "human_report": "nonempty-utf8-markdown",
    "worker_validation": False,
    "dispatcher_structural_attestation": True,
}
MARKETPLACE_REPORT_POLICY_HASH = hashlib.sha256(
    json.dumps(
        MARKETPLACE_REPORT_POLICY_SPEC,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
).hexdigest()
MARKETPLACE_REPORT_POLICY_SELECTOR = (
    f"{MARKETPLACE_REPORT_POLICY_ID}@{MARKETPLACE_REPORT_POLICY_VERSION}"
)
MARKETPLACE_APPLY_REPORT_POLICY_ID = "marketplace-agent-apply-report-worker"
MARKETPLACE_APPLY_REPORT_POLICY_V1_VERSION = 1
MARKETPLACE_APPLY_REPORT_POLICY_V1_SPEC = {
    "id": MARKETPLACE_APPLY_REPORT_POLICY_ID,
    "version": MARKETPLACE_APPLY_REPORT_POLICY_V1_VERSION,
    "execution_backend": "github-agent-tasks-rest",
    "authentication": "local-gh-api",
    "custom_agent": False,
    "local_fallback": False,
    "mode": "apply_with_report",
    "require_exact_task_identity": True,
    "require_unchanged_pr_head": True,
    "require_unchanged_local_identity": True,
    "require_linear_generated_history": True,
    "require_expected_paths_only": True,
    "require_fix_commit_correlation": True,
    "human_report": "nonempty-utf8-markdown",
    "worker_validation": False,
    "dispatcher_generated_commit_order": True,
    "dispatcher_structural_attestation": True,
}
MARKETPLACE_APPLY_REPORT_POLICY_V1_HASH = hashlib.sha256(
    json.dumps(
        MARKETPLACE_APPLY_REPORT_POLICY_V1_SPEC,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
).hexdigest()
MARKETPLACE_APPLY_REPORT_POLICY_V1_SELECTOR = (
    f"{MARKETPLACE_APPLY_REPORT_POLICY_ID}@"
    f"{MARKETPLACE_APPLY_REPORT_POLICY_V1_VERSION}"
)
MARKETPLACE_APPLY_REPORT_POLICY_V2_VERSION = 2
MARKETPLACE_APPLY_REPORT_POLICY_V2_SPEC = {
    "id": MARKETPLACE_APPLY_REPORT_POLICY_ID,
    "version": MARKETPLACE_APPLY_REPORT_POLICY_V2_VERSION,
    "execution_backend": "github-agent-tasks-rest",
    "authentication": "local-gh-api",
    "custom_agent": False,
    "local_fallback": False,
    "mode": "apply_with_report",
    "require_exact_task_identity": True,
    "require_unchanged_pr_head": True,
    "require_unchanged_local_identity": True,
    "require_linear_generated_history": True,
    "require_expected_paths_only": True,
    "fix_commit_correlation": "consumer-validated-report",
    "human_report": "nonempty-utf8-markdown",
    "worker_validation": False,
    "dispatcher_generated_commit_order": True,
    "dispatcher_structural_attestation": True,
}
MARKETPLACE_APPLY_REPORT_POLICY_V2_HASH = hashlib.sha256(
    json.dumps(
        MARKETPLACE_APPLY_REPORT_POLICY_V2_SPEC,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
).hexdigest()
MARKETPLACE_APPLY_REPORT_POLICY_V2_SELECTOR = (
    f"{MARKETPLACE_APPLY_REPORT_POLICY_ID}@"
    f"{MARKETPLACE_APPLY_REPORT_POLICY_V2_VERSION}"
)
MARKETPLACE_APPLY_REPORT_POLICY_V3_VERSION = 3
MARKETPLACE_APPLY_REPORT_POLICY_V3_SPEC = {
    **MARKETPLACE_APPLY_REPORT_POLICY_V2_SPEC,
    "version": MARKETPLACE_APPLY_REPORT_POLICY_V3_VERSION,
    "application": "consumer-after-report-validation",
}
MARKETPLACE_APPLY_REPORT_POLICY_V3_HASH = hashlib.sha256(
    json.dumps(
        MARKETPLACE_APPLY_REPORT_POLICY_V3_SPEC,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
).hexdigest()
MARKETPLACE_APPLY_REPORT_POLICY_V3_SELECTOR = (
    f"{MARKETPLACE_APPLY_REPORT_POLICY_ID}@"
    f"{MARKETPLACE_APPLY_REPORT_POLICY_V3_VERSION}"
)
MARKETPLACE_APPLY_REPORT_POLICY_V4_VERSION = 4
MARKETPLACE_APPLY_REPORT_POLICY_V4_SPEC = {
    **MARKETPLACE_APPLY_REPORT_POLICY_V3_SPEC,
    "version": MARKETPLACE_APPLY_REPORT_POLICY_V4_VERSION,
    "worker_artifact": "versioned-semantic-json",
    "worker_identity_fields": False,
    "worker_commit_identity": "one-based-commit-index",
    "dispatcher_semantic_binding": True,
}
MARKETPLACE_APPLY_REPORT_POLICY_V4_HASH = hashlib.sha256(
    json.dumps(
        MARKETPLACE_APPLY_REPORT_POLICY_V4_SPEC,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
).hexdigest()
MARKETPLACE_APPLY_REPORT_POLICY_V4_SELECTOR = (
    f"{MARKETPLACE_APPLY_REPORT_POLICY_ID}@"
    f"{MARKETPLACE_APPLY_REPORT_POLICY_V4_VERSION}"
)
MARKETPLACE_APPLY_REPORT_POLICY_VERSION = 5
MARKETPLACE_APPLY_REPORT_POLICY_SPEC = {
    **MARKETPLACE_APPLY_REPORT_POLICY_V4_SPEC,
    "version": MARKETPLACE_APPLY_REPORT_POLICY_VERSION,
    "worker_artifact": "minimal-semantic-payload-json",
    "semantic_wrapper_owner": "dispatcher",
}
MARKETPLACE_APPLY_REPORT_POLICY_HASH = hashlib.sha256(
    json.dumps(
        MARKETPLACE_APPLY_REPORT_POLICY_SPEC,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
).hexdigest()
MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR = (
    f"{MARKETPLACE_APPLY_REPORT_POLICY_ID}@"
    f"{MARKETPLACE_APPLY_REPORT_POLICY_VERSION}"
)
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
MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS = {
    MARKETPLACE_APPLY_REPORT_POLICY_V1_SELECTOR,
    MARKETPLACE_APPLY_REPORT_POLICY_V2_SELECTOR,
    MARKETPLACE_APPLY_REPORT_POLICY_V3_SELECTOR,
    MARKETPLACE_APPLY_REPORT_POLICY_V4_SELECTOR,
    MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR,
}
LEGACY_MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS = {
    MARKETPLACE_APPLY_REPORT_POLICY_V1_SELECTOR,
    MARKETPLACE_APPLY_REPORT_POLICY_V2_SELECTOR,
    MARKETPLACE_APPLY_REPORT_POLICY_V3_SELECTOR,
}
SEMANTIC_APPLY_REPORT_POLICY_SELECTORS = {
    MARKETPLACE_APPLY_REPORT_POLICY_V4_SELECTOR,
    MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR,
}
RECOVERY_ONLY_APPLY_REPORT_POLICY_SELECTORS = {
    MARKETPLACE_APPLY_REPORT_POLICY_V1_SELECTOR,
    MARKETPLACE_APPLY_REPORT_POLICY_V2_SELECTOR,
}
FIX_COMMIT_CORRELATION_FIELD = "Finding"


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
    dispatch_only: bool = False
    monitor_only: bool = False
    apply_with_report: bool = False
    resume_apply_with_report: bool = False
    result_file: Path | None = None
    policy: str | None = None
    task_id: str | None = None
    request_id: str | None = None
    worker_receipt: str | None = None
    semantic_kind: str | None = None
    input_result_file: Path | None = None
    prior_result: Mapping[str, object] | None = None
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
class ReportHistory:
    code_head: str
    code_commits: tuple[str, ...]
    report_commit: str


@dataclass(frozen=True)
class WorkerHistory:
    code_head: str
    code_commits: tuple[str, ...]
    receipt_commit: str


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
    schema_version: int = RESULT_SCHEMA_VERSION
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
    report_path: str | None = None
    report_commit: str | None = None
    report_sha256: str | None = None
    semantic_kind: str | None = None
    semantic_path: str | None = None
    semantic_commit: str | None = None
    semantic_sha256: str | None = None
    semantic_payload: Mapping[str, object] | None = None
    semantic_schema: Mapping[str, object] = field(
        default_factory=lambda: SEMANTIC_OUTPUT_SCHEMA
    )
    receipt_path: str | None = None
    receipt_commit: str | None = None
    receipt_sha256: str | None = None
    validation_complete: bool = False
    validation_outcomes: list[dict[str, str]] | None = None
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
                "number": self.pull_request.number,
                "url": self.pull_request.url,
                "base_repository": self.pull_request.base_repository,
                "base_ref": self.pull_request.base_ref,
                "base_sha": self.pull_request.base_sha,
                "head_repository": self.pull_request.head_repository,
                "head_ref": self.pull_request.head_ref,
                "head_sha": self.pull_request.head_sha,
            }
        common = {
            "schema": {
                "id": RESULT_SCHEMA_ID,
                "version": self.schema_version,
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
            "report": (
                {
                    "path": self.report_path,
                    "commit": self.report_commit,
                    "sha256": self.report_sha256,
                }
                if self.report_path is not None
                else None
            ),
        }
        if self.schema_version in {
            REPORT_RESULT_SCHEMA_VERSION,
            LEGACY_SEMANTIC_RESULT_SCHEMA_VERSION,
            SEMANTIC_RESULT_SCHEMA_VERSION,
            CANDIDATE_RESULT_SCHEMA_VERSION,
        }:
            common["attestation"] = {
                "kind": (
                    "dispatcher_candidate"
                    if self.schema_version == CANDIDATE_RESULT_SCHEMA_VERSION
                    else "dispatcher_semantic"
                    if self.schema_version
                    in {
                        LEGACY_SEMANTIC_RESULT_SCHEMA_VERSION,
                        SEMANTIC_RESULT_SCHEMA_VERSION,
                    }
                    else "dispatcher_structural"
                ),
                "structural_complete": self.structural_complete,
            }
            if self.schema_version == CANDIDATE_RESULT_SCHEMA_VERSION:
                common["candidate"] = self.candidate_manifest
                common["completion"] = self.completion_evidence
            if self.schema_version in {
                LEGACY_SEMANTIC_RESULT_SCHEMA_VERSION,
                SEMANTIC_RESULT_SCHEMA_VERSION,
            }:
                common["semantic_output"] = (
                    {
                        "schema": self.semantic_schema,
                        "kind": self.semantic_kind,
                        "path": self.semantic_path,
                        "commit": self.semantic_commit,
                        "sha256": self.semantic_sha256,
                        "payload": self.semantic_payload,
                    }
                    if self.semantic_path is not None
                    else None
                )
        else:
            common["worker_receipt"] = (
                {
                    "path": self.receipt_path,
                    "commit": self.receipt_commit,
                    "sha256": self.receipt_sha256,
                }
                if self.receipt_path is not None
                else None
            )
            common["validation"] = {
                "complete": self.validation_complete,
                "outcomes": self.validation_outcomes or [],
            }
        common["error"] = (
                {
                    "code": self.error_code,
                    "message": self.error_message,
                }
                if self.error_code is not None
                else None
            )
        return common


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
    dispatch_only = False
    monitor_only = False
    apply_with_report = False
    resume_apply_with_report = False
    model_alias = "sol"
    prompt_file: str | None = None
    pull_request: PrReference | None = None
    result_file: Path | None = None
    policy: str | None = None
    task_id: str | None = None
    request_id: str | None = None
    worker_receipt: str | None = None
    semantic_kind: str | None = None
    input_result_file: Path | None = None
    prior_result: Mapping[str, object] | None = None
    allow_merged_pr = False
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
        if token == "--report":
            report = True
            index += 1
            continue
        if token == "--dispatch-only":
            dispatch_only = True
            index += 1
            continue
        if token == "--monitor-only":
            monitor_only = True
            index += 1
            continue
        if token == "--apply-with-report":
            if apply_with_report:
                raise CloudError("--apply-with-report mode may be specified only once")
            apply_with_report = True
            index += 1
            continue
        if token == "--resume-apply-with-report":
            if apply_with_report:
                raise CloudError(
                    "--apply-with-report mode may be specified only once"
                )
            apply_with_report = True
            resume_apply_with_report = True
            index += 1
            continue
        if token == "--allow-merged-pr":
            if allow_merged_pr:
                raise CloudError("--allow-merged-pr may be specified only once")
            allow_merged_pr = True
            index += 1
            continue
        if token == "--result-file":
            if index + 1 >= len(args):
                raise CloudError(
                    "--result-file requires an absolute path",
                    "result_file_invalid",
                )
            if result_file is not None:
                raise CloudError(
                    "--result-file may be specified only once",
                    "result_file_invalid",
                )
            result_file = Path(args[index + 1])
            if not result_file.is_absolute():
                raise CloudError(
                    "--result-file requires an absolute path",
                    "result_file_invalid",
                )
            index += 2
            continue
        if token == "--policy":
            if index + 1 >= len(args):
                raise CloudError("--policy requires a policy selector", "policy_required")
            if policy is not None:
                raise CloudError(
                    "--policy may be specified only once",
                    "policy_rejected",
                )
            policy = args[index + 1]
            index += 2
            continue
        if token == "--task-id":
            if index + 1 >= len(args):
                raise CloudError("--task-id requires a task id", "task_identity_invalid")
            if task_id is not None:
                raise CloudError(
                    "--task-id may be specified only once",
                    "task_identity_invalid",
                )
            task_id = args[index + 1]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", task_id):
                raise CloudError("invalid --task-id value", "task_identity_invalid")
            index += 2
            continue
        if token == "--request-id":
            if index + 1 >= len(args):
                raise CloudError(
                    "--request-id requires a value",
                    "task_identity_invalid",
                )
            if request_id is not None:
                raise CloudError(
                    "--request-id may be specified only once",
                    "task_identity_invalid",
                )
            request_id = args[index + 1]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", request_id):
                raise CloudError(
                    "invalid --request-id value",
                    "task_identity_invalid",
                )
            index += 2
            continue
        if token == "--worker-receipt":
            if index + 1 >= len(args):
                raise CloudError(
                    "--worker-receipt requires a repository-relative path",
                    "receipt_invalid",
                )
            if worker_receipt is not None:
                raise CloudError(
                    "--worker-receipt may be specified only once",
                    "receipt_invalid",
                )
            worker_receipt = args[index + 1]
            index += 2
            continue
        if token == "--semantic-kind":
            if index + 1 >= len(args):
                raise CloudError(
                    "--semantic-kind requires a value",
                    "semantic_output_invalid",
                )
            if semantic_kind is not None:
                raise CloudError(
                    "--semantic-kind may be specified only once",
                    "semantic_output_invalid",
                )
            semantic_kind = args[index + 1]
            if not re.fullmatch(r"[a-z][a-z0-9.-]*", semantic_kind):
                raise CloudError(
                    "invalid --semantic-kind value",
                    "semantic_output_invalid",
                )
            index += 2
            continue
        if token == "--input-result-file":
            if index + 1 >= len(args):
                raise CloudError(
                    "--input-result-file requires an absolute path",
                    "malformed_result",
                )
            if input_result_file is not None:
                raise CloudError(
                    "--input-result-file may be specified only once",
                    "malformed_result",
                )
            input_result_file = Path(args[index + 1])
            if not input_result_file.is_absolute():
                raise CloudError(
                    "--input-result-file requires an absolute path",
                    "malformed_result",
                )
            index += 2
            continue
        if token == "--model":
            if index + 1 >= len(args):
                raise CloudError("--model requires one of: luna, terra, sol, astra")
            model_alias = args[index + 1]
            if model_alias not in MODEL_IDS:
                raise CloudError(
                    f"unsupported model {model_alias!r}; choose luna, terra, sol, or astra"
                )
            index += 2
            continue
        if token == "--prompt-file":
            if index + 1 >= len(args):
                raise CloudError("--prompt-file requires an absolute path")
            if prompt_file is not None:
                raise CloudError("--prompt-file may be specified only once")
            prompt_file = args[index + 1]
            index += 2
            continue
        if token == "--pr":
            if index + 1 >= len(args):
                raise CloudError("--pr requires a pull request URL, number, or owner/repo#number")
            if pull_request is not None:
                raise CloudError("--pr may be specified only once")
            pull_request = parse_pr_reference(args[index + 1])
            index += 2
            continue
        raise CloudError(f"unknown option: {token}")

    if (
        resume_apply_with_report
        or input_result_file is not None
        or task_id is not None
        or monitor_only
    ):
        raise CloudError(
            "resume, monitor-only, and prior-result import are disabled; start a "
            "fresh invocation",
            "recovery_disabled",
        )
    if prompt_start is None:
        prompt_start = len(args)
    inline_prompt = " ".join(args[prompt_start:]).strip()
    if prompt_file is not None and inline_prompt:
        raise CloudError("--prompt-file cannot be combined with an inline prompt")
    if input_result_file is not None:
        if task_id is not None or worker_receipt is not None:
            raise CloudError(
                "--input-result-file cannot be combined with --task-id or "
                "--worker-receipt",
                "malformed_result",
            )
        if monitor_only:
            prior_result = read_dispatch_result(input_result_file)
        elif apply_with_report and allow_merged_pr:
            prior_result = read_apply_result(input_result_file, policy=policy)
        else:
            raise CloudError(
                "--input-result-file is valid only with --monitor-only or "
                "historical --apply-with-report",
                "malformed_result",
            )
        task = prior_result["task"]
        if not isinstance(task, dict):
            raise AssertionError("validated result lost task metadata")
        if not isinstance(task.get("id"), str) or not task["id"]:
            raise CloudError(
                "resume result has no reusable Agent Task identity",
                "task_identity_invalid",
            )
        task_id = task["id"]
        if policy == MARKETPLACE_POLICY_SELECTOR:
            receipt_data = prior_result.get("worker_receipt")
            if not isinstance(receipt_data, dict):
                raise AssertionError("validated result lost receipt metadata")
            worker_receipt = str(receipt_data["path"])
    if prompt_file is not None:
        prompt = read_prompt_file(prompt_file)
    elif inline_prompt:
        prompt = inline_prompt
    elif (monitor_only or resume_apply_with_report) and task_id is not None:
        prompt = ""
    else:
        raise CloudError("a prompt is required")
    if sum((report, dispatch_only, monitor_only, apply_with_report)) > 1:
        raise CloudError(
            "--report, --dispatch-only, --monitor-only, --apply-with-report, "
            "and --resume-apply-with-report are mutually exclusive"
        )
    if (dispatch_only or monitor_only or apply_with_report) and pull_request is None:
        if dispatch_only:
            option = "--dispatch-only"
        elif monitor_only:
            option = "--monitor-only"
        else:
            option = "--apply-with-report"
        raise CloudError(f"{option} requires --pr")
    if allow_merged_pr and (pull_request is None or not apply_with_report):
        raise CloudError(
            "--allow-merged-pr is valid only with --pr and --apply-with-report"
        )
    if result_file is not None and policy is None:
        raise CloudError(
            "--result-file requires --policy",
            "policy_required",
        )
    if policy is not None and result_file is None:
        raise CloudError(
            "--policy requires --result-file",
            "result_file_required",
        )
    known_policies = {
        *MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS,
        *CANDIDATE_POLICY_SELECTORS,
        MARKETPLACE_POLICY_SELECTOR,
        MARKETPLACE_REPORT_POLICY_SELECTOR,
    }
    if policy is not None and policy not in known_policies:
        raise CloudError(
            f"unknown policy {policy!r}; expected one of "
            f"{', '.join(sorted(known_policies))}",
            "policy_unknown",
        )
    if (
        policy in RECOVERY_ONLY_APPLY_REPORT_POLICY_SELECTORS
        and not resume_apply_with_report
    ):
        raise CloudError(
            f"{policy} is immutable and available only for task recovery",
            "policy_rejected",
        )
    if policy == MARKETPLACE_REPORT_POLICY_SELECTOR and (
        not report
        or pull_request is None
        or prompt_file is None
        or dispatch_only
        or monitor_only
        or apply_with_report
        or resume_apply_with_report
        or task_id is not None
        or worker_receipt is not None
        or input_result_file is not None
        or allow_merged_pr
    ):
        raise CloudError(
            f"{MARKETPLACE_REPORT_POLICY_SELECTOR} requires --report, --pr, "
            "--prompt-file, and --result-file and does not support recovery or "
            "validation-receipt options",
            "policy_rejected",
        )
    if policy == MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR and (
        not report
        or pull_request is None
        or prompt_file is None
        or dispatch_only
        or monitor_only
        or apply_with_report
        or resume_apply_with_report
        or task_id is not None
        or worker_receipt is not None
        or input_result_file is not None
        or allow_merged_pr
    ):
        raise CloudError(
            f"{MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR} requires "
            "--report, --pr, --prompt-file, and --result-file and does not "
            "support recovery or validation-receipt options",
            "policy_rejected",
        )
    if policy == MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR and (
        not apply_with_report
        or pull_request is None
        or prompt_file is None
        or report
        or dispatch_only
        or monitor_only
        or resume_apply_with_report
        or task_id is not None
        or worker_receipt is not None
        or input_result_file is not None
    ):
        raise CloudError(
            f"{MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR} requires "
            "--apply-with-report, --pr, --prompt-file, and --result-file and "
            "does not support recovery or application options",
            "policy_rejected",
        )
    if policy in MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS and (
        not apply_with_report
        or pull_request is None
        or (prompt_file is None and not resume_apply_with_report)
        or report
        or dispatch_only
        or monitor_only
    ):
        raise CloudError(
            f"{policy} requires "
            "--apply-with-report, --pr, --prompt-file, and --result-file",
            "policy_rejected",
        )
    if policy in SEMANTIC_APPLY_REPORT_POLICY_SELECTORS and semantic_kind is None:
        raise CloudError(
            f"{policy} requires --semantic-kind",
            "policy_rejected",
        )
    if (
        semantic_kind is not None
        and policy not in SEMANTIC_APPLY_REPORT_POLICY_SELECTORS
    ):
        raise CloudError(
            "--semantic-kind is valid only with "
            "a semantic apply-with-report policy",
            "policy_rejected",
        )
    if allow_merged_pr and (
        policy
        not in {
            MARKETPLACE_POLICY_SELECTOR, *MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS,
            MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR,
        }
        or result_file is None
        or prompt_file is None
    ):
        raise CloudError(
            "historical apply-with-report requires --prompt-file, "
            "--result-file, and a supported apply-with-report policy",
            "policy_required",
        )
    if task_id is not None and not (
        monitor_only
        or resume_apply_with_report
        or (
            apply_with_report
            and allow_merged_pr
            and input_result_file is not None
        )
    ):
        raise CloudError(
            "--task-id is valid only with --monitor-only, "
            "--resume-apply-with-report, or supported result recovery",
            "task_identity_invalid",
        )
    if request_id is not None and not resume_apply_with_report:
        raise CloudError(
            "--request-id is valid only with --resume-apply-with-report",
            "task_identity_invalid",
        )
    if worker_receipt is not None and policy != MARKETPLACE_POLICY_SELECTOR:
        raise CloudError(
            "--worker-receipt is valid only with "
            f"{MARKETPLACE_POLICY_SELECTOR}",
            "receipt_invalid",
        )
    if worker_receipt is not None and not (
        monitor_only
        or resume_apply_with_report
        or (
            apply_with_report
            and allow_merged_pr
            and input_result_file is not None
        )
    ):
        raise CloudError(
            "--worker-receipt is valid only with --monitor-only, "
            "--resume-apply-with-report, or supported result recovery",
            "receipt_invalid",
        )
    if (
        input_result_file is not None
        and result_file is not None
        and input_result_file.resolve() == result_file.resolve()
    ):
        raise CloudError(
            "--result-file must differ from --input-result-file",
            "result_file_invalid",
        )
    if policy == MARKETPLACE_POLICY_SELECTOR and monitor_only:
        if task_id is None:
            raise CloudError(
                f"{MARKETPLACE_POLICY_SELECTOR} monitor-only mode requires --task-id",
                "task_identity_invalid",
            )
        if worker_receipt is None:
            raise CloudError(
                f"{MARKETPLACE_POLICY_SELECTOR} monitor-only mode requires "
                "--worker-receipt",
                "receipt_invalid",
            )
    if resume_apply_with_report:
        if (
            pull_request is None
            or task_id is None
            or (
                policy in MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS
                and request_id is None
            )
            or result_file is None
            or policy
            not in {
                MARKETPLACE_POLICY_SELECTOR,
                *MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS,
            }
            or (
                policy == MARKETPLACE_POLICY_SELECTOR
                and worker_receipt is None
            )
        ):
            raise CloudError(
                "--resume-apply-with-report requires --pr, --task-id, "
                "--result-file, and a supported apply-with-report policy "
                "(the legacy validation policy also requires "
                "--worker-receipt; the structural policy requires "
                "--request-id)",
                "policy_required",
            )
        if prompt_file is not None or inline_prompt:
            raise CloudError(
                "--resume-apply-with-report retrieves the authoritative prompt "
                "from the Agent Task and does not accept a caller prompt",
                "policy_rejected",
            )
        if allow_merged_pr or input_result_file is not None:
            raise CloudError(
                "--resume-apply-with-report cannot be combined with historical "
                "or result-file recovery options",
                "policy_rejected",
            )
    return Options(
        report=report,
        model=MODEL_IDS[model_alias],
        prompt=prompt,
        pull_request=pull_request,
        dispatch_only=dispatch_only,
        monitor_only=monitor_only,
        apply_with_report=apply_with_report,
        resume_apply_with_report=resume_apply_with_report,
        result_file=result_file,
        policy=policy,
        task_id=task_id,
        request_id=request_id,
        worker_receipt=worker_receipt,
        input_result_file=input_result_file,
        prior_result=prior_result,
        allow_merged_pr=allow_merged_pr,
        prompt_file=Path(prompt_file) if prompt_file is not None else None,
        semantic_kind=semantic_kind,
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


def read_dispatch_result(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        detail = error.msg if isinstance(error, json.JSONDecodeError) else str(error)
        raise CloudError(
            f"could not read dispatch result {path}: {detail}",
            "malformed_result",
        ) from None
    expected_keys = {
        "schema",
        "status",
        "mode",
        "repository",
        "pull_request",
        "requested_model",
        "policy",
        "task",
        "generated",
        "application",
        "report",
        "worker_receipt",
        "validation",
        "error",
    }
    if not isinstance(data, dict) or set(data) != expected_keys:
        raise CloudError(
            "dispatch result has unexpected or missing fields",
            "malformed_result",
        )
    if data.get("schema") != {
        "id": RESULT_SCHEMA_ID,
        "version": RESULT_SCHEMA_VERSION,
    }:
        raise CloudError("dispatch result has an unsupported schema", "malformed_result")
    if data.get("status") != "success" or data.get("mode") != "dispatch_only":
        raise CloudError(
            "input result is not a successful dispatch-only result",
            "malformed_result",
        )
    if data.get("policy") != {
        "id": MARKETPLACE_POLICY_ID,
        "version": MARKETPLACE_POLICY_VERSION,
        "sha256": MARKETPLACE_POLICY_HASH,
    }:
        raise CloudError(
            "dispatch result policy does not match the required policy",
            "policy_rejected",
        )
    repository = data.get("repository")
    task = data.get("task")
    receipt = data.get("worker_receipt")
    model = data.get("requested_model")
    generated = data.get("generated")
    application = data.get("application")
    validation = data.get("validation")
    if (
        not isinstance(repository, dict)
        or set(repository) != {"name_with_owner"}
        or not isinstance(repository.get("name_with_owner"), str)
        or not re.fullmatch(
            r"[^/\s]+/[^/\s]+", repository["name_with_owner"]
        )
        or not isinstance(model, str)
        or model not in MODEL_IDS.values()
        or not isinstance(task, dict)
        or set(task) != {"id", "url", "state", "base_ref", "base_sha"}
        or not isinstance(task.get("id"), str)
        or not task["id"]
        or task.get("state") not in KNOWN_STATES
        or (
            task.get("url") is not None
            and not isinstance(task.get("url"), str)
        )
        or not isinstance(task.get("base_ref"), str)
        or not task["base_ref"]
        or not isinstance(task.get("base_sha"), str)
        or not SHA_PATTERN.fullmatch(task["base_sha"])
        or not isinstance(receipt, dict)
        or set(receipt) != {"path", "commit", "sha256"}
        or not isinstance(receipt.get("path"), str)
        or receipt.get("commit") is not None
        or receipt.get("sha256") is not None
        or generated
        != {
            "branch": None,
            "head_sha": None,
            "commits": [],
        }
        or not isinstance(application, dict)
        or set(application) != {"status", "final_local_head"}
        or application.get("status") != "not_applicable"
        or not isinstance(application.get("final_local_head"), str)
        or not SHA_PATTERN.fullmatch(application["final_local_head"])
        or data.get("report") is not None
        or validation != {"complete": False, "outcomes": []}
        or data.get("error") is not None
    ):
        raise CloudError(
            "dispatch result contains malformed identity fields",
            "malformed_result",
        )
    validate_receipt_path(receipt["path"])
    pull_request = data.get("pull_request")
    if (
        not isinstance(pull_request, dict)
        or set(pull_request)
        != {
            "number",
            "url",
            "base_repository",
            "base_ref",
            "base_sha",
            "head_repository",
            "head_ref",
            "head_sha",
        }
        or not isinstance(pull_request.get("number"), int)
        or isinstance(pull_request.get("number"), bool)
        or not isinstance(pull_request.get("url"), str)
        or not isinstance(pull_request.get("base_repository"), str)
        or not isinstance(pull_request.get("base_ref"), str)
        or not isinstance(pull_request.get("base_sha"), str)
        or not SHA_PATTERN.fullmatch(pull_request["base_sha"])
        or not isinstance(pull_request.get("head_repository"), str)
        or not isinstance(pull_request.get("head_ref"), str)
        or not isinstance(pull_request.get("head_sha"), str)
        or not SHA_PATTERN.fullmatch(pull_request["head_sha"])
    ):
        raise CloudError(
            "dispatch result contains malformed pull request identity",
            "malformed_result",
        )
    return data


def read_apply_result(
    path: Path,
    *,
    policy: str | None,
) -> dict[str, object]:
    if policy in MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS:
        return read_structural_apply_result(path, policy=policy)
    return read_legacy_apply_result(path)


def structural_policy_metadata(policy: str) -> dict[str, object]:
    if policy == MARKETPLACE_APPLY_REPORT_POLICY_V1_SELECTOR:
        version = MARKETPLACE_APPLY_REPORT_POLICY_V1_VERSION
        digest = MARKETPLACE_APPLY_REPORT_POLICY_V1_HASH
    elif policy == MARKETPLACE_APPLY_REPORT_POLICY_V2_SELECTOR:
        version = MARKETPLACE_APPLY_REPORT_POLICY_V2_VERSION
        digest = MARKETPLACE_APPLY_REPORT_POLICY_V2_HASH
    elif policy == MARKETPLACE_APPLY_REPORT_POLICY_V3_SELECTOR:
        version = MARKETPLACE_APPLY_REPORT_POLICY_V3_VERSION
        digest = MARKETPLACE_APPLY_REPORT_POLICY_V3_HASH
    elif policy == MARKETPLACE_APPLY_REPORT_POLICY_V4_SELECTOR:
        version = MARKETPLACE_APPLY_REPORT_POLICY_V4_VERSION
        digest = MARKETPLACE_APPLY_REPORT_POLICY_V4_HASH
    elif policy == MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR:
        version = MARKETPLACE_APPLY_REPORT_POLICY_VERSION
        digest = MARKETPLACE_APPLY_REPORT_POLICY_HASH
    else:
        raise CloudError(
            f"unsupported structural apply policy {policy!r}",
            "policy_rejected",
        )
    return {
        "id": MARKETPLACE_APPLY_REPORT_POLICY_ID,
        "version": version,
        "sha256": digest,
    }


def read_structural_apply_result(
    path: Path,
    *,
    policy: str = MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR,
) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        detail = error.msg if isinstance(error, json.JSONDecodeError) else str(error)
        raise CloudError(
            f"could not read prior result {path}: {detail}",
            "malformed_result",
        ) from None
    expected_keys = {
        "schema",
        "status",
        "mode",
        "repository",
        "pull_request",
        "requested_model",
        "policy",
        "task",
        "generated",
        "application",
        "report",
        "attestation",
        "error",
    }
    if not isinstance(data, dict) or set(data) != expected_keys:
        raise CloudError(
            "prior result has unexpected or missing fields",
            "malformed_result",
        )
    if (
        data.get("schema")
        != {
            "id": RESULT_SCHEMA_ID,
            "version": REPORT_RESULT_SCHEMA_VERSION,
        }
        or data.get("mode") != "apply_with_report"
        or data.get("policy") != structural_policy_metadata(policy)
    ):
        raise CloudError(
            "prior result has an unsupported schema, mode, or policy",
            "policy_rejected",
        )
    status = data.get("status")
    repository = data.get("repository")
    pull_request = data.get("pull_request")
    task = data.get("task")
    generated = data.get("generated")
    application = data.get("application")
    report = data.get("report")
    attestation = data.get("attestation")
    error = data.get("error")
    if (
        status not in {"success", "error", "interrupted"}
        or not isinstance(repository, dict)
        or set(repository) != {"name_with_owner"}
        or not isinstance(repository.get("name_with_owner"), str)
        or not re.fullmatch(r"[^/\s]+/[^/\s]+", repository["name_with_owner"])
        or not isinstance(data.get("requested_model"), str)
        or data["requested_model"] not in MODEL_IDS.values()
        or not isinstance(pull_request, dict)
        or set(pull_request)
        != {
            "number",
            "url",
            "base_repository",
            "base_ref",
            "base_sha",
            "head_repository",
            "head_ref",
            "head_sha",
        }
        or not isinstance(pull_request.get("number"), int)
        or isinstance(pull_request.get("number"), bool)
        or not isinstance(pull_request.get("url"), str)
        or not isinstance(pull_request.get("base_repository"), str)
        or not isinstance(pull_request.get("base_ref"), str)
        or not isinstance(pull_request.get("base_sha"), str)
        or not SHA_PATTERN.fullmatch(pull_request["base_sha"])
        or not isinstance(pull_request.get("head_repository"), str)
        or not isinstance(pull_request.get("head_ref"), str)
        or not isinstance(pull_request.get("head_sha"), str)
        or not SHA_PATTERN.fullmatch(pull_request["head_sha"])
        or not isinstance(task, dict)
        or set(task) != {"id", "url", "state", "base_ref", "base_sha"}
        or (
            task.get("id") is not None
            and (
                not isinstance(task.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", task["id"])
            )
        )
        or (task.get("url") is not None and not isinstance(task.get("url"), str))
        or (
            task.get("state") is not None
            and task.get("state") not in KNOWN_STATES
        )
        or (
            task.get("base_ref") is not None
            and not isinstance(task.get("base_ref"), str)
        )
        or (
            task.get("base_sha") is not None
            and (
                not isinstance(task.get("base_sha"), str)
                or not SHA_PATTERN.fullmatch(task["base_sha"])
            )
        )
        or not isinstance(generated, dict)
        or set(generated) != {"branch", "head_sha", "commits"}
        or (
            generated.get("branch") is not None
            and not isinstance(generated.get("branch"), str)
        )
        or (
            generated.get("head_sha") is not None
            and (
                not isinstance(generated.get("head_sha"), str)
                or not SHA_PATTERN.fullmatch(generated["head_sha"])
            )
        )
        or not isinstance(generated.get("commits"), list)
        or any(
            not isinstance(commit, str) or not SHA_PATTERN.fullmatch(commit)
            for commit in generated.get("commits", [])
        )
        or not isinstance(application, dict)
        or set(application) != {"status", "final_local_head"}
        or application.get("status")
        not in {"not_started", "not_applied", "applied", "no_changes"}
        or (
            application.get("final_local_head") is not None
            and (
                not isinstance(application.get("final_local_head"), str)
                or not SHA_PATTERN.fullmatch(application["final_local_head"])
            )
        )
        or not isinstance(attestation, dict)
        or attestation.get("kind") != "dispatcher_structural"
        or set(attestation) != {"kind", "structural_complete"}
        or not isinstance(attestation.get("structural_complete"), bool)
    ):
        raise CloudError(
            "prior result contains malformed identity fields",
            "malformed_result",
        )
    if report is not None and (
        not isinstance(report, dict)
        or set(report) != {"path", "commit", "sha256"}
        or not isinstance(report.get("path"), str)
        or (
            report.get("commit") is not None
            and (
                not isinstance(report.get("commit"), str)
                or not SHA_PATTERN.fullmatch(report["commit"])
            )
        )
        or (
            report.get("sha256") is not None
            and (
                not isinstance(report.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", report["sha256"])
            )
        )
    ):
        raise CloudError("prior result report identity is malformed", "malformed_result")
    try:
        resolved_url = parse_pr_reference(pull_request["url"])
    except CloudError:
        raise CloudError(
            "prior result contains a malformed pull request URL",
            "malformed_result",
        ) from None
    if (
        resolved_url.number != pull_request["number"]
        or resolved_url.repository is None
        or resolved_url.repository.casefold()
        != pull_request["base_repository"].casefold()
    ):
        raise CloudError(
            "prior result contains inconsistent pull request identity",
            "malformed_result",
        )
    if status == "success":
        code_commits = generated["commits"]
        two_phase = policy in {
            MARKETPLACE_APPLY_REPORT_POLICY_V3_SELECTOR,
            MARKETPLACE_APPLY_REPORT_POLICY_V4_SELECTOR,
            MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR,
        }
        expected_local_head = (
            pull_request["head_sha"]
            if two_phase
            else code_commits[-1]
            if code_commits
            else pull_request["head_sha"]
        )
        expected_application = (
            "not_applied"
            if two_phase
            else "applied"
            if code_commits
            else "no_changes"
        )
        if (
            error is not None
            or not task.get("id")
            or task.get("state") != "completed"
            or task.get("base_ref") != pull_request["head_sha"]
            or task.get("base_sha") != pull_request["head_sha"]
            or not generated.get("branch")
            or not generated.get("head_sha")
            or report is None
            or report.get("commit") != generated.get("head_sha")
            or report.get("sha256") is None
            or application.get("status") != expected_application
            or application.get("final_local_head") != expected_local_head
            or attestation.get("structural_complete") is not True
        ):
            raise CloudError(
                "successful prior result is incomplete",
                "malformed_result",
            )
    elif (
        not isinstance(error, dict)
        or set(error) != {"code", "message"}
        or not isinstance(error.get("code"), str)
        or not error["code"]
        or not isinstance(error.get("message"), str)
        or not error["message"]
    ):
        raise CloudError("prior result error is malformed", "malformed_result")
    return data


def read_legacy_apply_result(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        detail = error.msg if isinstance(error, json.JSONDecodeError) else str(error)
        raise CloudError(
            f"could not read prior result {path}: {detail}",
            "malformed_result",
        ) from None
    expected_keys = {
        "schema",
        "status",
        "mode",
        "repository",
        "pull_request",
        "requested_model",
        "policy",
        "task",
        "generated",
        "application",
        "report",
        "worker_receipt",
        "validation",
        "error",
    }
    if not isinstance(data, dict) or set(data) != expected_keys:
        raise CloudError(
            "prior result has unexpected or missing fields",
            "malformed_result",
        )
    if data.get("schema") != {
        "id": RESULT_SCHEMA_ID,
        "version": RESULT_SCHEMA_VERSION,
    } or data.get("mode") != "apply_with_report":
        raise CloudError(
            "prior result has an unsupported schema or mode",
            "malformed_result",
        )
    if data.get("policy") != {
        "id": MARKETPLACE_POLICY_ID,
        "version": MARKETPLACE_POLICY_VERSION,
        "sha256": MARKETPLACE_POLICY_HASH,
    }:
        raise CloudError(
            "prior result policy does not match the required policy",
            "policy_rejected",
        )
    status = data.get("status")
    repository = data.get("repository")
    pull_request = data.get("pull_request")
    task = data.get("task")
    generated = data.get("generated")
    application = data.get("application")
    report = data.get("report")
    receipt = data.get("worker_receipt")
    validation = data.get("validation")
    error = data.get("error")
    if (
        status not in {"success", "error", "interrupted"}
        or not isinstance(repository, dict)
        or set(repository) != {"name_with_owner"}
        or not isinstance(repository.get("name_with_owner"), str)
        or not re.fullmatch(r"[^/\s]+/[^/\s]+", repository["name_with_owner"])
        or not isinstance(data.get("requested_model"), str)
        or data["requested_model"] not in MODEL_IDS.values()
        or not isinstance(pull_request, dict)
        or set(pull_request)
        != {
            "number",
            "url",
            "base_repository",
            "base_ref",
            "base_sha",
            "head_repository",
            "head_ref",
            "head_sha",
        }
        or not isinstance(pull_request.get("number"), int)
        or isinstance(pull_request.get("number"), bool)
        or not isinstance(pull_request.get("url"), str)
        or not isinstance(pull_request.get("base_repository"), str)
        or not isinstance(pull_request.get("base_ref"), str)
        or not isinstance(pull_request.get("base_sha"), str)
        or not SHA_PATTERN.fullmatch(pull_request["base_sha"])
        or not isinstance(pull_request.get("head_repository"), str)
        or not isinstance(pull_request.get("head_ref"), str)
        or not isinstance(pull_request.get("head_sha"), str)
        or not SHA_PATTERN.fullmatch(pull_request["head_sha"])
        or not isinstance(task, dict)
        or set(task) != {"id", "url", "state", "base_ref", "base_sha"}
        or (
            task.get("id") is not None
            and (
                not isinstance(task.get("id"), str)
                or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._:-]*",
                    task["id"],
                )
            )
        )
        or (
            task.get("url") is not None
            and not isinstance(task.get("url"), str)
        )
        or (
            task.get("state") is not None
            and task.get("state") not in KNOWN_STATES
        )
        or (
            task.get("base_ref") is not None
            and not isinstance(task.get("base_ref"), str)
        )
        or (
            task.get("base_sha") is not None
            and (
                not isinstance(task.get("base_sha"), str)
                or not SHA_PATTERN.fullmatch(task["base_sha"])
            )
        )
        or not isinstance(generated, dict)
        or set(generated) != {"branch", "head_sha", "commits"}
        or (
            generated.get("branch") is not None
            and not isinstance(generated.get("branch"), str)
        )
        or (
            generated.get("head_sha") is not None
            and (
                not isinstance(generated.get("head_sha"), str)
                or not SHA_PATTERN.fullmatch(generated["head_sha"])
            )
        )
        or not isinstance(generated.get("commits"), list)
        or any(
            not isinstance(commit, str) or not SHA_PATTERN.fullmatch(commit)
            for commit in generated.get("commits", [])
        )
        or not isinstance(application, dict)
        or set(application) != {"status", "final_local_head"}
        or application.get("status")
        not in {"not_started", "not_applied", "applied", "no_changes"}
        or (
            application.get("final_local_head") is not None
            and (
                not isinstance(application.get("final_local_head"), str)
                or not SHA_PATTERN.fullmatch(application["final_local_head"])
            )
        )
        or not isinstance(receipt, dict)
        or set(receipt) != {"path", "commit", "sha256"}
        or not isinstance(receipt.get("path"), str)
        or (
            receipt.get("commit") is not None
            and (
                not isinstance(receipt.get("commit"), str)
                or not SHA_PATTERN.fullmatch(receipt["commit"])
            )
        )
        or (
            receipt.get("sha256") is not None
            and (
                not isinstance(receipt.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"])
            )
        )
        or not isinstance(validation, dict)
        or set(validation) != {"complete", "outcomes"}
        or not isinstance(validation.get("complete"), bool)
        or not isinstance(validation.get("outcomes"), list)
        or (
            validation.get("complete") is False
            and validation.get("outcomes") != []
        )
    ):
        raise CloudError(
            "prior result contains malformed identity fields",
            "malformed_result",
        )
    validate_receipt_path(receipt["path"])
    try:
        resolved_url = parse_pr_reference(pull_request["url"])
    except CloudError:
        raise CloudError(
            "prior result contains a malformed pull request URL",
            "malformed_result",
        ) from None
    if (
        resolved_url.number != pull_request["number"]
        or resolved_url.repository is None
        or resolved_url.repository.casefold()
        != pull_request["base_repository"].casefold()
    ):
        raise CloudError(
            "prior result contains inconsistent pull request identity",
            "malformed_result",
        )
    if report is not None and (
        not isinstance(report, dict)
        or set(report) != {"path", "commit", "sha256"}
        or not isinstance(report.get("path"), str)
        or (
            report.get("commit") is not None
            and (
                not isinstance(report.get("commit"), str)
                or not SHA_PATTERN.fullmatch(report["commit"])
            )
        )
        or (
            report.get("sha256") is not None
            and (
                not isinstance(report.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", report["sha256"])
            )
        )
    ):
        raise CloudError("prior result report identity is malformed", "malformed_result")
    outcomes = validation["outcomes"]
    if validation["complete"]:
        for outcome in outcomes:
            if (
                not isinstance(outcome, dict)
                or set(outcome) != {"command", "outcome"}
                or not isinstance(outcome.get("command"), str)
                or not outcome["command"].strip()
                or outcome.get("outcome") != "passed"
            ):
                raise CloudError(
                    "prior result validation is malformed",
                    "malformed_result",
                )
    if status == "success":
        code_commits = generated["commits"]
        expected_local_head = (
            code_commits[-1] if code_commits else pull_request["head_sha"]
        )
        if (
            error is not None
            or not task.get("id")
            or task.get("state") != "completed"
            or task.get("base_ref") != pull_request["head_sha"]
            or task.get("base_sha") != pull_request["head_sha"]
            or not generated.get("branch")
            or not generated.get("head_sha")
            or receipt.get("commit") is None
            or receipt.get("sha256") is None
            or report is None
            or report.get("commit") is None
            or report.get("sha256") is None
            or receipt.get("commit") != generated.get("head_sha")
            or report.get("commit") != generated.get("head_sha")
            or application.get("status")
            != ("applied" if code_commits else "no_changes")
            or application.get("final_local_head") != expected_local_head
            or validation["complete"] is not True
            or not outcomes
        ):
            raise CloudError(
                "successful prior result is incomplete",
                "malformed_result",
            )
    elif (
        not isinstance(error, dict)
        or set(error) != {"code", "message"}
        or not isinstance(error.get("code"), str)
        or not error["code"]
        or not isinstance(error.get("message"), str)
        or not error["message"]
    ):
        raise CloudError("prior result error is malformed", "malformed_result")
    return data


def mode_name(options: Options) -> str:
    if options.policy == MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR:
        return "code_candidate"
    if options.policy == MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR:
        return "report_recommendation"
    if options.dispatch_only:
        return "dispatch_only"
    if options.monitor_only:
        return "monitor_only"
    if options.apply_with_report:
        return "apply_with_report"
    if options.report:
        return "report"
    return "code"


def validate_interrupted_apply_task(
    task: Mapping[str, object],
    *,
    task_id: str,
    repository: str,
    model: str,
    pull_request: PullRequestSnapshot,
    request_id: str,
    report_path: str,
    worker_receipt: str | None,
    policy: str = MARKETPLACE_POLICY_SELECTOR,
    semantic_kind: str | None = None,
) -> None:
    if task.get("id") != task_id:
        raise CloudError(
            "Agent Task identity does not match the interrupted recovery request",
            "task_identity_mismatch",
        )
    sessions = task.get("sessions")
    if not isinstance(sessions, list) or len(sessions) != 1:
        raise CloudError(
            "interrupted apply-with-report recovery requires exactly one Agent "
            "Task session",
            "task_identity_mismatch",
        )
    session = sessions[0]
    if not isinstance(session, dict):
        raise CloudError(
            "Agent Task session identity is malformed",
            "task_identity_mismatch",
        )
    task_repository = task.get("repository")
    task_owner = task.get("owner")
    expected_models = {model, f"sweagent-capi:{model}"}
    if (
        session.get("task_id") != task_id
        or session.get("model") not in expected_models
        or session.get("base_ref") != task_base_ref(pull_request)
        or not isinstance(task_repository, dict)
        or not isinstance(task_repository.get("id"), int)
        or isinstance(task_repository.get("id"), bool)
        or session.get("repository") != task_repository
        or not isinstance(task_owner, dict)
        or not isinstance(task_owner.get("id"), int)
        or isinstance(task_owner.get("id"), bool)
        or session.get("owner") != task_owner
    ):
        raise CloudError(
            "Agent Task session, model, repository, owner, or source base does "
            "not match the interrupted recovery request",
            "task_identity_mismatch",
        )
    prompt = session.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise CloudError(
            "Agent Task session has no authoritative prompt",
            "malformed_report",
        )
    expected_prefix = build_pr_prompt("", pull_request)
    expected_apply_suffix = (
        ""
        if policy in SEMANTIC_APPLY_REPORT_POLICY_SELECTORS
        else build_apply_with_report_prompt(
            "",
            report_path,
            worker_receipt,
        )
    )
    if policy == MARKETPLACE_POLICY_SELECTOR:
        if worker_receipt is None:
            raise AssertionError("legacy recovery lost its validation receipt")
        expected_policy_suffix = build_policy_prompt(
            "",
            request_id=request_id,
            receipt=worker_receipt,
            mode="apply_with_report",
            repository=repository,
            pull_request=pull_request,
        )
    elif policy in MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS:
        expected_policy_suffix = build_apply_report_policy_prompt(
            "",
            report_path=report_path,
            policy=policy,
            semantic_kind=semantic_kind,
        )
    else:
        raise AssertionError("unsupported interrupted recovery policy")
    markers = [PR_CONTEXT_MARKER, POLICY_MARKER]
    if policy not in SEMANTIC_APPLY_REPORT_POLICY_SELECTORS:
        markers.append(APPLY_WITH_REPORT_MARKER)
    if (
        any(prompt.count(marker) != 2 for marker in markers)
        or REPORT_MARKER in prompt
        or not prompt.startswith(expected_prefix)
        or not prompt.endswith(expected_policy_suffix)
    ):
        raise CloudError(
            "Agent Task prompt does not prove the original apply-with-report "
            "policy and source identity",
            "task_identity_mismatch",
        )
    before_policy = prompt[: -len(expected_policy_suffix)]
    if expected_apply_suffix and not before_policy.endswith(expected_apply_suffix):
        raise CloudError(
            "Agent Task prompt does not prove the expected report and validation "
            "artifact paths",
            "task_identity_mismatch",
        )
    workflow_prompt = (
        before_policy[len(expected_prefix) : -len(expected_apply_suffix)]
        if expected_apply_suffix
        else before_policy[len(expected_prefix) :]
    )
    if not workflow_prompt.strip():
        raise CloudError(
            "Agent Task prompt has no workflow instructions",
            "malformed_report",
        )


def policy_metadata(options: Options) -> dict[str, object] | None:
    if options.policy is None:
        return None
    if options.policy == MARKETPLACE_REPORT_POLICY_SELECTOR:
        return {
            "id": MARKETPLACE_REPORT_POLICY_ID,
            "version": MARKETPLACE_REPORT_POLICY_VERSION,
            "sha256": MARKETPLACE_REPORT_POLICY_HASH,
        }
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
    if options.policy in MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS:
        return structural_policy_metadata(options.policy)
    return {
        "id": MARKETPLACE_POLICY_ID,
        "version": MARKETPLACE_POLICY_VERSION,
        "sha256": MARKETPLACE_POLICY_HASH,
    }


def receipt_path(request_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", request_id):
        raise CloudError("invalid worker validation request id", "receipt_invalid")
    return f"{VALIDATION_DIRECTORY}/{request_id}.json"


def validate_receipt_path(path: str) -> None:
    if not re.fullmatch(
        rf"{re.escape(VALIDATION_DIRECTORY)}/[A-Za-z0-9][A-Za-z0-9._-]*\.json",
        path,
    ):
        raise CloudError(
            f"invalid worker validation path {path!r}",
            "receipt_invalid",
        )


def validate_policy_before_post(
    options: Options,
    root: Path,
    result_path: Path,
) -> None:
    if options.policy not in {
        *MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS,
        *CANDIDATE_POLICY_SELECTORS,
        MARKETPLACE_POLICY_SELECTOR,
        MARKETPLACE_REPORT_POLICY_SELECTOR,
    }:
        raise CloudError(
            "a supported marketplace policy is required",
            "policy_required",
        )
    if options.policy in CANDIDATE_POLICY_SELECTORS and (
        options.pull_request is None
        or options.prompt_file is None
        or options.dispatch_only
        or options.monitor_only
        or options.resume_apply_with_report
        or options.task_id is not None
        or options.request_id is not None
        or options.worker_receipt is not None
        or options.semantic_kind is not None
        or options.input_result_file is not None
        or options.prior_result is not None
        or (
            options.allow_merged_pr
            and options.policy != MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR
        )
        or (
            options.policy == MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR
            and (not options.apply_with_report or options.report)
        )
        or (
            options.policy
            == MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR
            and (not options.report or options.apply_with_report)
        )
    ):
        raise CloudError(
            "candidate policies require a fresh non-recovery invocation and "
            "never accept application or prior-result identity",
            "policy_rejected",
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
    if options.allow_merged_pr:
        if options.prompt_file is None:
            raise CloudError(
                "historical apply-with-report requires --prompt-file",
                "policy_rejected",
            )
        _require_path_outside_repository(
            resolved_root,
            options.prompt_file,
            "--prompt-file",
        )
        if options.input_result_file is not None:
            _require_path_outside_repository(
                resolved_root,
                options.input_result_file,
                "--input-result-file",
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


def validate_dispatch_result_identity(
    data: Mapping[str, object],
    *,
    repository: str,
    model: str,
    pull_request: PullRequestSnapshot | None,
) -> None:
    repository_data = data.get("repository")
    task = data.get("task")
    prior_pr = data.get("pull_request")
    if (
        not isinstance(repository_data, dict)
        or repository_data.get("name_with_owner") != repository
        or data.get("requested_model") != model
    ):
        raise CloudError(
            "dispatch result repository or model does not match this monitor request",
            "task_identity_mismatch",
        )
    if not isinstance(task, dict):
        raise CloudError("dispatch result task identity is malformed", "malformed_result")
    if pull_request is None:
        if prior_pr is not None:
            raise CloudError(
                "dispatch result pull request does not match this monitor request",
                "task_identity_mismatch",
            )
        return
    if (
        not isinstance(prior_pr, dict)
        or prior_pr.get("number") != pull_request.number
        or prior_pr.get("head_repository") != pull_request.head_repository
        or prior_pr.get("head_ref") != pull_request.head_ref
        or prior_pr.get("head_sha") != pull_request.head_sha
        or task.get("base_ref") != task_base_ref(pull_request)
        or task.get("base_sha") != pull_request.head_sha
    ):
        raise CloudError(
            "dispatch result task or pull request identity does not match the "
            "current pull request head",
            "task_identity_mismatch",
        )


def validate_apply_result_identity(
    data: Mapping[str, object],
    *,
    repository: str,
    model: str,
    pull_request: PullRequestSnapshot,
    snapshot: WorktreeSnapshot,
) -> None:
    repository_data = data.get("repository")
    prior_pr = data.get("pull_request")
    task = data.get("task")
    generated = data.get("generated")
    application = data.get("application")
    receipt = data.get("worker_receipt")
    report = data.get("report")
    if (
        not isinstance(repository_data, dict)
        or repository_data.get("name_with_owner") != repository
        or data.get("requested_model") != model
    ):
        raise CloudError(
            "prior result repository or model does not match this resume request",
            "task_identity_mismatch",
        )
    expected_pr = {
        "number": pull_request.number,
        "url": pull_request.url,
        "base_repository": pull_request.base_repository,
        "base_ref": pull_request.base_ref,
        "base_sha": pull_request.base_sha,
        "head_repository": pull_request.head_repository,
        "head_ref": pull_request.head_ref,
        "head_sha": pull_request.head_sha,
    }
    if prior_pr != expected_pr:
        raise CloudError(
            "prior result pull request identity does not match the merged pull request",
            "task_identity_mismatch",
        )
    if (
        not isinstance(task, dict)
        or task.get("base_ref") != pull_request.head_sha
        or task.get("base_sha") != pull_request.head_sha
    ):
        raise CloudError(
            "prior result task base does not match the merged pull request head",
            "task_identity_mismatch",
        )
    if (
        not isinstance(generated, dict)
        or not isinstance(application, dict)
    ):
        raise CloudError("prior result identity is malformed", "malformed_result")
    if not isinstance(report, dict):
        raise CloudError("prior result report identity is malformed", "malformed_result")
    if isinstance(receipt, dict):
        request_id = Path(str(receipt["path"])).stem
        expected_report_path = f"{REPORT_DIRECTORY}/{request_id}.md"
    else:
        report_path_value = report.get("path")
        if not isinstance(report_path_value, str):
            raise CloudError(
                "prior result report identity is malformed",
                "malformed_result",
            )
        request_id = Path(report_path_value).stem
        expected_report_path = f"{REPORT_DIRECTORY}/{request_id}.md"
    if report.get("path") != expected_report_path:
        raise CloudError(
            "prior result report request identity does not match",
            "task_identity_mismatch",
        )
    commits = generated.get("commits")
    if not isinstance(commits, list):
        raise CloudError("prior generated commits are malformed", "malformed_result")
    allowed_heads = {pull_request.head_sha}
    if commits:
        allowed_heads.add(str(commits[-1]).lower())
    prior_local_head = application.get("final_local_head")
    if (
        prior_local_head not in allowed_heads
        or snapshot.head not in allowed_heads
    ):
        raise CloudError(
            "local HEAD does not match the prior result's allowed historical state",
            "local_drift",
        )


def validate_prior_generated_result(
    data: Mapping[str, object],
    *,
    generated_branch: str,
    generated_head: str,
    code_commits: Sequence[str],
    receipt_path_value: str,
    receipt_commit: str,
    receipt_sha256: str,
    report_path_value: str,
    report_commit: str,
    report_sha256: str,
    validation_outcomes: Sequence[Mapping[str, str]],
) -> None:
    generated = data["generated"]
    receipt = data["worker_receipt"]
    report = data["report"]
    validation = data["validation"]
    if (
        not isinstance(generated, dict)
        or not isinstance(receipt, dict)
        or not isinstance(validation, dict)
    ):
        raise AssertionError("validated prior result lost generated metadata")
    comparisons = (
        (generated.get("branch"), generated_branch, "generated branch"),
        (generated.get("head_sha"), generated_head, "generated head"),
        (receipt.get("path"), receipt_path_value, "receipt path"),
        (receipt.get("commit"), receipt_commit, "receipt commit"),
        (receipt.get("sha256"), receipt_sha256, "receipt SHA-256"),
    )
    for prior, current, field in comparisons:
        if prior is not None and prior != current:
            raise CloudError(
                f"prior result {field} does not match the original task",
                "task_identity_mismatch",
            )
    prior_commits = generated.get("commits")
    if (
        prior_commits or data.get("status") == "success"
    ) and list(prior_commits) != list(code_commits):
        raise CloudError(
            "prior result generated commits do not match the original task",
            "task_identity_mismatch",
        )
    if report is not None:
        expected_report = {
            "path": report_path_value,
            "commit": report_commit,
            "sha256": report_sha256,
        }
        for field, current in expected_report.items():
            prior = report.get(field)
            if prior is not None and prior != current:
                raise CloudError(
                    f"prior result report {field} does not match the original task",
                    "task_identity_mismatch",
                )
    if validation.get("complete") is True and validation.get("outcomes") != list(
        validation_outcomes
    ):
        raise CloudError(
            "prior result validation does not match the original task",
            "task_identity_mismatch",
        )


def validate_prior_structural_generated_result(
    data: Mapping[str, object],
    *,
    generated_branch: str,
    generated_head: str,
    code_commits: Sequence[str],
    report_path_value: str,
    report_commit: str,
    report_sha256: str,
) -> None:
    generated = data["generated"]
    report = data["report"]
    attestation = data["attestation"]
    if not isinstance(generated, dict) or not isinstance(attestation, dict):
        raise AssertionError("validated prior result lost generated metadata")
    for prior, current, field in (
        (generated.get("branch"), generated_branch, "generated branch"),
        (generated.get("head_sha"), generated_head, "generated head"),
    ):
        if prior is not None and prior != current:
            raise CloudError(
                f"prior result {field} does not match the original task",
                "task_identity_mismatch",
            )
    prior_commits = generated.get("commits")
    if (
        prior_commits or data.get("status") == "success"
    ) and list(prior_commits) != list(code_commits):
        raise CloudError(
            "prior result generated commits do not match the original task",
            "task_identity_mismatch",
        )
    if report is not None:
        if not isinstance(report, dict):
            raise AssertionError("validated prior result lost report metadata")
        for field, current in {
            "path": report_path_value,
            "commit": report_commit,
            "sha256": report_sha256,
        }.items():
            prior = report.get(field)
            if prior is not None and prior != current:
                raise CloudError(
                    f"prior result report {field} does not match the original task",
                    "task_identity_mismatch",
                )
    if (
        attestation.get("structural_complete") is True
        and data.get("status") != "success"
    ):
        raise CloudError(
            "prior result structural attestation is inconsistent",
            "task_identity_mismatch",
        )


def build_policy_prompt(
    prompt: str,
    *,
    request_id: str,
    receipt: str,
    mode: str,
    repository: str,
    pull_request: PullRequestSnapshot | None,
) -> str:
    expected_paths = [receipt]
    if mode in {"report", "apply_with_report"}:
        expected_paths.insert(0, f"{REPORT_DIRECTORY}/{request_id}.md")
    return (
        f"{prompt.rstrip()}\n\n"
        f"{POLICY_MARKER}\n"
        f"Policy: {MARKETPLACE_POLICY_SELECTOR}\n"
        f"Policy SHA-256: {MARKETPLACE_POLICY_HASH}\n"
        "Authentication stays in the local dispatcher. Do not request, read, "
        "print, persist, or transmit credentials, tokens, keys, cookies, or "
        "authorization headers. Do not select or invoke a custom_agent. Do not "
        "use a local-execution fallback.\n"
        "Run every required validation on the hosted worker. After validation "
        f"passes, write `{receipt}` as a nonempty JSON array. Every element must "
        "contain exactly `command` and `outcome`; both values must be nonempty "
        "strings and `outcome` must be `passed`. For example: "
        '[{"command":"python -m pytest","outcome":"passed"}]. Record commands as '
        "evidence only. Do not write request, policy, repository, pull request, "
        "task, or completion metadata.\n"
        "Create exactly one final single-parent artifact commit whose changed "
        f"paths are exactly {json.dumps(expected_paths)}. Put code changes in "
        "preceding linear commits, make no preceding commit in report mode, and "
        "do not add commits afterward. Write the final workflow report and validation "
        "directly to the assigned paths above. Do not create, stage, or commit alternate "
        "report, validation, or scratch artifact paths; remove any working files before "
        "the final commit. In apply-with-report mode, every fix commit "
        "must have a concise normal message with one nonempty `Finding: <identifier>` "
        "line, unique among the generated fix commits. Keep the report as nonempty "
        "UTF-8 Markdown for humans. The dispatcher derives and records generated "
        "fix commit order; do not echo commit SHAs for dispatcher attestation.\n"
        f"{POLICY_MARKER}"
    )


def build_report_policy_prompt(prompt: str, *, report_path: str) -> str:
    return (
        f"{prompt.rstrip()}\n\n"
        f"{POLICY_MARKER}\n"
        f"Policy: {MARKETPLACE_REPORT_POLICY_SELECTOR}\n"
        f"Policy SHA-256: {MARKETPLACE_REPORT_POLICY_HASH}\n"
        "Authentication stays in the local dispatcher. Do not request, read, "
        "print, persist, or transmit credentials, tokens, keys, cookies, or "
        "authorization headers. Do not select or invoke a custom_agent. Do not "
        "use a local-execution fallback.\n"
        "Create exactly one final single-parent artifact commit based directly "
        "on the task base. Its only changed path must be "
        f"`{report_path}`. Write the complete report directly to that path as "
        "nonempty UTF-8 Markdown. Do not create, stage, or commit validation, "
        "alternate report, or scratch artifact paths, and do not add commits "
        "afterward. The dispatcher independently verifies task and source "
        "identity, live head, ancestry, linear history, the exact path, and the "
        "report digest. It records structural completion only and does not "
        "claim that the worker performed validation.\n"
        f"{POLICY_MARKER}"
    )


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


def build_apply_report_policy_prompt(
    prompt: str,
    *,
    report_path: str,
    policy: str = MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR,
    semantic_kind: str | None = None,
) -> str:
    if policy == MARKETPLACE_APPLY_REPORT_POLICY_V1_SELECTOR:
        policy_hash = MARKETPLACE_APPLY_REPORT_POLICY_V1_HASH
        correlation = (
            "Every code commit must have a concise normal message with one nonempty "
            "`Finding: <identifier>` line, unique among the generated code commits. "
        )
    elif policy in {
        MARKETPLACE_APPLY_REPORT_POLICY_V2_SELECTOR,
        MARKETPLACE_APPLY_REPORT_POLICY_V3_SELECTOR,
    }:
        policy_hash = str(structural_policy_metadata(policy)["sha256"])
        correlation = (
            "Do not add machine-readable correlation trailers to commit messages. "
            "Record finding-to-commit and changed-path correlation only in the "
            "assigned report. The local workflow coordinator validates that mapping "
            "against the exact request and generated history before publication. "
        )
    elif policy in SEMANTIC_APPLY_REPORT_POLICY_SELECTORS:
        if semantic_kind is None:
            raise CloudError(
                f"{policy} requires a semantic kind",
                "policy_rejected",
            )
        policy_hash = str(structural_policy_metadata(policy)["sha256"])
        legacy_wrapper = policy == MARKETPLACE_APPLY_REPORT_POLICY_V4_SELECTOR
        semantic_shape = (
            {
                "schema": LEGACY_SEMANTIC_OUTPUT_SCHEMA,
                "kind": semantic_kind,
                "payload": {},
            }
            if legacy_wrapper
            else {}
        )
        artifact_instruction = (
            "Write UTF-8 JSON with exactly the wrapper shown below. Replace "
            "`payload` with the workflow-specific semantic object required above; "
            "do not add wrapper keys."
            if legacy_wrapper
            else (
                "Write only the workflow-specific semantic object required above as "
                "one nonempty UTF-8 JSON object. Do not wrap it in schema, kind, "
                "version, or payload fields. The dispatcher owns and adds the "
                "versioned semantic wrapper."
            )
        )
        return (
            f"{prompt.rstrip()}\n\n"
            f"{POLICY_MARKER}\n"
            f"Policy: {policy}\n"
            f"Policy SHA-256: {policy_hash}\n"
            "Authentication and all request, repository, pull request, frozen "
            "head/base, model, policy, task, session, generated-history, and "
            "completion identities belong only to the dispatcher. Do not echo, "
            "reconstruct, or author any of them in the semantic output.\n"
            "Put substantive code changes in zero or more linear single-parent "
            "commits. Then create exactly one final single-parent semantic artifact "
            f"commit whose only changed path is `{report_path}`. "
            f"{artifact_instruction} "
            "Refer to a generated fix commit only with a one-based integer "
            "`commit_index`, where 1 is the oldest generated fix commit. Use null "
            "when no fix commit applies. Do not write commit SHAs or identity fields "
            "such as request_id, repository, pull_request, head, base, model, policy, "
            "task, session, generated, validation, report, or receipt. The dispatcher "
            "derives history, binds trusted identity, resolves commit indices, and "
            "writes the canonical result envelope. Every generated fix commit must be "
            "referenced by at least one commit_index; missing, malformed, out-of-range, "
            "or extra references fail closed.\n"
            + (
                f"{json.dumps(semantic_shape, ensure_ascii=False, sort_keys=True)}\n"
                if legacy_wrapper
                else ""
            )
            + f"{POLICY_MARKER}"
        )
    else:
        raise CloudError(f"unsupported structural apply policy {policy!r}")
    return (
        f"{prompt.rstrip()}\n\n"
        f"{POLICY_MARKER}\n"
        f"Policy: {policy}\n"
        f"Policy SHA-256: {policy_hash}\n"
        "Authentication stays in the local dispatcher. Do not request, read, "
        "print, persist, or transmit credentials, tokens, keys, cookies, or "
        "authorization headers. Do not select or invoke a custom_agent. Do not "
        "use a local-execution fallback.\n"
        "Put code changes in zero or more linear commits. "
        f"{correlation}"
        "Then create exactly one final single-parent report commit. "
        f"Its only changed path must be `{report_path}`. Write the complete "
        "report directly to that path as nonempty UTF-8 Markdown. Do not "
        "create, stage, or commit validation, alternate report, or scratch "
        "artifact paths, and do not add commits afterward. The dispatcher "
        "independently verifies task and source identity, live head, ancestry, "
        "linear commit ordering, changed paths, report encoding and size, and "
        "artifact digests. It records structural completion only and does not "
        "claim that the worker ran or passed validation. Any commands or "
        "results described in the report are untrusted inert evidence.\n"
        f"{POLICY_MARKER}"
    )


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

    def worker_history(
        self,
        root: Path,
        base_sha: str,
        commits: Sequence[str],
        expected_paths: Sequence[str],
    ) -> WorkerHistory:
        if not commits:
            raise CloudError(
                "the generated branch did not contain a worker validation commit",
                "malformed_history",
            )
        receipt_commit = commits[-1]
        parent_line = self._run(
            root,
            "rev-list",
            "--parents",
            "-n",
            "1",
            receipt_commit,
        ).stdout.strip().lower()
        parents = parent_line.split()
        if len(parents) != 2 or parents[0] != receipt_commit:
            raise CloudError(
                "the generated branch tip must be a single-parent worker "
                "receipt commit",
                "malformed_history",
            )
        code_commits = tuple(commits[:-1])
        code_head = code_commits[-1] if code_commits else base_sha
        if parents[1] != code_head:
            raise CloudError(
                "the worker validation commit is not directly based on the "
                "generated code head",
                "malformed_history",
            )
        changed_output = self._run(
            root,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            receipt_commit,
        ).stdout
        changed_paths = tuple(path for path in changed_output.split("\0") if path)
        if tuple(sorted(changed_paths)) != tuple(sorted(expected_paths)):
            rendered = ", ".join(changed_paths) if changed_paths else "no paths"
            raise CloudError(
                "the worker validation commit changed unexpected paths: "
                f"{rendered}; expected {', '.join(expected_paths)}",
                "unexpected_paths",
            )
        expected_parent = base_sha
        for commit in code_commits:
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
            expected_parent = commit
        return WorkerHistory(code_head, code_commits, receipt_commit)

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

    def report_history(
        self,
        root: Path,
        base_sha: str,
        commits: Sequence[str],
        report_path: str,
    ) -> ReportHistory:
        if not commits:
            raise CloudError(
                "the generated branch did not contain a report commit",
                "malformed_history",
            )
        report_commit = commits[-1]
        parent_line = self._run(
            root, "rev-list", "--parents", "-n", "1", report_commit
        ).stdout.strip().lower()
        parents = parent_line.split()
        if len(parents) != 2 or parents[0] != report_commit:
            raise CloudError(
                "the generated branch tip must be a single-parent report commit",
                "malformed_history",
            )
        code_commits = tuple(commits[:-1])
        code_head = code_commits[-1] if code_commits else base_sha
        if parents[1] != code_head:
            raise CloudError(
                "the final report commit is not directly based on the generated "
                "code head",
                "malformed_history",
            )
        changed_output = self._run(
            root,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            report_commit,
        ).stdout
        changed_paths = tuple(path for path in changed_output.split("\0") if path)
        if changed_paths != (report_path,):
            rendered = ", ".join(changed_paths) if changed_paths else "no paths"
            raise CloudError(
                "the final report commit must change only "
                f"{report_path}; it changed {rendered}",
                "unexpected_paths",
            )
        return ReportHistory(code_head, code_commits, report_commit)

    def require_correlated_fix_commits(
        self, root: Path, commits: Sequence[str]
    ) -> None:
        correlations: set[str] = set()
        for commit in commits:
            message = self._run(root, "show", "-s", "--format=%B", commit).stdout
            matches = re.findall(
                rf"(?m)^{FIX_COMMIT_CORRELATION_FIELD}:[ \t]+"
                r"([^\r\n]*\S)[ \t]*$",
                message,
            )
            if len(matches) != 1:
                raise CloudError(
                    f"fix commit {commit} must contain exactly one nonempty "
                    f"{FIX_COMMIT_CORRELATION_FIELD}: correlation",
                    "malformed_history",
                )
            correlation = matches[0].strip()
            if correlation in correlations:
                raise CloudError(
                    f"fix commit {commit} reuses Finding: correlation "
                    f"{correlation!r}",
                    "malformed_history",
                )
            correlations.add(correlation)

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

    def cherry_pick(
        self, snapshot: WorktreeSnapshot, commits: Sequence[str]
    ) -> None:
        if not commits:
            return
        command = ["git", "cherry-pick", *commits]
        result = run_process(self.runner, command, cwd=snapshot.root)
        if result.returncode == 0:
            return

        failure = _command_error(command, result)
        aborted = False
        if self.operation(snapshot.root) in {"cherry-pick", "cherry-pick or revert"}:
            abort = run_process(
                self.runner,
                ["git", "cherry-pick", "--abort"],
                cwd=snapshot.root,
            )
            if abort.returncode != 0:
                raise CloudError(
                    f"{failure}; automatic cherry-pick abort also failed: "
                    f"{abort.stderr.strip() or abort.stdout.strip()}"
                )
            try:
                self.require_clean(snapshot.root)
                self.require_no_operation(snapshot.root)
                restored_head = self.head(snapshot.root)
            except CloudError as error:
                raise CloudError(
                    f"{failure}; cherry-pick abort did not restore the worktree: {error}"
                ) from None
            if restored_head != snapshot.head:
                raise CloudError(
                    f"{failure}; cherry-pick was aborted but HEAD was not restored"
                )
            aborted = True
        if aborted:
            raise CloudError(f"{failure}; the helper's cherry-pick was aborted")
        raise CloudError(failure)


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


def build_report_prompt(prompt: str, report_path: str) -> str:
    return (
        f"{prompt.rstrip()}\n\n"
        f"{REPORT_MARKER}\n"
        "Perform the requested investigation in read-only mode. Do not make any "
        "production, test, or configuration changes. Write the complete "
        f"user-facing report to `{report_path}` and commit that report file to "
        "the generated branch. The committed file must contain the full result, "
        "not a summary or a link.\n"
        f"{REPORT_MARKER}"
    )


def render_artifact_paths(
    prompt: str,
    *,
    report_path: str,
    worker_receipt: str | None,
    semantic: bool,
) -> str:
    placeholder = (
        SEMANTIC_PATH_PLACEHOLDER if semantic else REPORT_PATH_PLACEHOLDER
    )
    rendered = prompt.replace(placeholder, report_path)
    if VALIDATION_PATH_PLACEHOLDER in rendered:
        if worker_receipt is None:
            raise CloudError(
                "the workflow prompt requires a validation path outside policy mode",
                "policy_required",
            )
        rendered = rendered.replace(VALIDATION_PATH_PLACEHOLDER, worker_receipt)
    if (
        REPORT_PATH_PLACEHOLDER in rendered
        or SEMANTIC_PATH_PLACEHOLDER in rendered
        or VALIDATION_PATH_PLACEHOLDER in rendered
    ):
        raise CloudError(
            "the workflow prompt contains an unresolved artifact path placeholder",
            "malformed_report",
        )
    return rendered


def build_apply_with_report_prompt(
    prompt: str,
    report_path: str,
    worker_receipt: str | None = None,
) -> str:
    final_path_instruction = (
        f"whose only changed paths are `{report_path}` and `{worker_receipt}`"
        if worker_receipt is not None
        else "whose only changed path is that report file"
    )
    return (
        f"{prompt.rstrip()}\n\n"
        f"{APPLY_WITH_REPORT_MARKER}\n"
        "Perform the requested review and make every requested code, test, or "
        "configuration change. Keep those changes in their required commits. "
        "After all fixes and validation are complete, write the complete "
        f"user-facing report to `{report_path}` and create exactly one final "
        f"commit {final_path_instruction}. The generated "
        "branch tip must be this single-parent report commit. Base it directly "
        "on the final fix commit, or directly on the task base when there are no "
        "fixes. Do not amend, squash, reorder, or add commits after the report "
        "commit. The report must contain the full result, including findings "
        "that did not produce code changes, not a summary or a link.\n"
        f"{APPLY_WITH_REPORT_MARKER}"
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


def task_payload(
    options: Options,
    report_path: str | None = None,
    pull_request: PullRequestSnapshot | None = None,
    *,
    request_id: str | None = None,
    worker_receipt: str | None = None,
    repository: str | None = None,
) -> dict[str, object]:
    if options.policy in CANDIDATE_POLICY_SELECTORS:
        if report_path != OUTPUT_REPORT_PATH:
            raise AssertionError("candidate policy did not use its fixed report path")
        prompt = render_artifact_paths(
            options.prompt,
            report_path=report_path,
            worker_receipt=None,
            semantic=False,
        )
    elif options.report and report_path is not None:
        prompt = build_report_prompt(
            render_artifact_paths(
                options.prompt,
                report_path=report_path,
                worker_receipt=worker_receipt,
                semantic=False,
            ),
            report_path,
        )
    elif options.apply_with_report:
        if report_path is None:
            raise AssertionError("apply-with-report mode did not allocate a report path")
        rendered = render_artifact_paths(
            options.prompt,
            report_path=report_path,
            worker_receipt=worker_receipt,
            semantic=options.policy in SEMANTIC_APPLY_REPORT_POLICY_SELECTORS,
        )
        prompt = (
            rendered
            if options.policy in SEMANTIC_APPLY_REPORT_POLICY_SELECTORS
            else build_apply_with_report_prompt(
                rendered,
                report_path,
                worker_receipt if options.policy is not None else None,
            )
        )
    else:
        prompt = options.prompt
    if pull_request is not None:
        prompt = build_pr_prompt(prompt, pull_request)
    if options.policy == MARKETPLACE_POLICY_SELECTOR:
        if request_id is None or worker_receipt is None or repository is None:
            raise AssertionError(
                "validation policy mode did not allocate worker metadata"
            )
        prompt = build_policy_prompt(
            prompt,
            request_id=request_id,
            receipt=worker_receipt,
            mode=mode_name(options),
            repository=repository,
            pull_request=pull_request,
        )
    elif options.policy == MARKETPLACE_REPORT_POLICY_SELECTOR:
        if report_path is None:
            raise AssertionError("report policy mode did not allocate a report path")
        prompt = build_report_policy_prompt(prompt, report_path=report_path)
    elif options.policy in MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS:
        if report_path is None:
            raise AssertionError(
                "apply-report policy mode did not allocate a report path"
            )
        prompt = build_apply_report_policy_prompt(
            prompt,
            report_path=report_path,
            policy=options.policy,
            semantic_kind=options.semantic_kind,
        )
    elif options.policy in CANDIDATE_POLICY_SELECTORS:
        prompt = build_candidate_policy_prompt(
            prompt,
            policy=options.policy,
        )
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


def fetch_report(
    api: ApiClient,
    repository: str,
    report_path: str,
    head_ref: str,
) -> str:
    encoded_path = urllib.parse.quote(report_path, safe="/")
    encoded_ref = urllib.parse.quote(head_ref, safe="")
    data = api.request_json(
        "GET",
        f"repos/{repository}/contents/{encoded_path}?ref={encoded_ref}",
        expected_status=200,
        operation=f"fetch committed report {report_path}",
    )
    if not isinstance(data, dict) or data.get("type") != "file":
        raise CloudError(
            f"GitHub did not return {report_path} as a committed file",
            "malformed_report",
        )
    if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
        raise CloudError(
            f"GitHub returned {report_path} in an unsupported format",
            "malformed_report",
        )
    try:
        content = base64.b64decode(
            "".join(data["content"].split()), validate=True
        ).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as error:
        raise CloudError(
            f"GitHub returned an invalid report file: {error}",
            "malformed_report",
        ) from None
    return content


SEMANTIC_IDENTITY_KEYS = {
    "base",
    "base_sha",
    "commit",
    "commits",
    "fix_commit",
    "fix_commits",
    "generated",
    "head",
    "head_sha",
    "model",
    "policy",
    "pull_request",
    "receipt",
    "report",
    "repository",
    "request_id",
    "session",
    "sha",
    "task",
    "validation",
    "validation_complete",
}


def bind_semantic_payload(
    value: object,
    commits: Sequence[str],
) -> tuple[object, set[int]]:
    references: set[int] = set()

    def bind(item: object) -> object:
        if isinstance(item, list):
            return [bind(entry) for entry in item]
        if not isinstance(item, dict):
            return item
        forbidden = sorted(SEMANTIC_IDENTITY_KEYS & set(item))
        if forbidden:
            raise CloudError(
                "semantic output contains dispatcher-owned fields: "
                + ", ".join(forbidden),
                "semantic_output_invalid",
            )
        result: dict[str, object] = {}
        for key, nested in item.items():
            if not isinstance(key, str):
                raise CloudError(
                    "semantic output contains a non-string key",
                    "semantic_output_invalid",
                )
            if key == "commit_index":
                if "commit" in item:
                    raise CloudError(
                        "semantic output contains both commit and commit_index",
                        "semantic_output_invalid",
                    )
                if nested is None:
                    result["commit"] = None
                    continue
                if (
                    isinstance(nested, bool)
                    or not isinstance(nested, int)
                    or nested < 1
                    or nested > len(commits)
                ):
                    raise CloudError(
                        "semantic output contains an invalid commit_index",
                        "semantic_output_invalid",
                    )
                references.add(nested)
                result["commit"] = commits[nested - 1]
                continue
            result[key] = bind(nested)
        return result

    return bind(value), references


def fetch_semantic_output(
    api: ApiClient,
    repository: str,
    path: str,
    head_ref: str,
    *,
    kind: str,
    commits: Sequence[str],
    policy: str = MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR,
) -> tuple[Mapping[str, object], str]:
    try:
        content = fetch_report(api, repository, path, head_ref)
        value = json.loads(content)
    except (CloudError, json.JSONDecodeError) as error:
        message = str(error) if isinstance(error, CloudError) else error.msg
        raise CloudError(
            f"malformed marketplace semantic output {path}: {message}",
            "semantic_output_invalid",
        ) from None
    if policy == MARKETPLACE_APPLY_REPORT_POLICY_V4_SELECTOR:
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "kind", "payload"}
            or value.get("schema") != LEGACY_SEMANTIC_OUTPUT_SCHEMA
            or value.get("kind") != kind
            or not isinstance(value.get("payload"), dict)
            or not value["payload"]
        ):
            raise CloudError(
                "marketplace semantic output has an unsupported wrapper",
                "semantic_output_invalid",
            )
        value = value["payload"]
    elif (
        policy != MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR
        or not isinstance(value, dict)
        or not value
    ):
        raise CloudError(
            "marketplace semantic output has an unsupported payload",
            "semantic_output_invalid",
        )
    payload, references = bind_semantic_payload(value, commits)
    expected_references = set(range(1, len(commits) + 1))
    if references != expected_references:
        raise CloudError(
            "semantic output does not account for every generated fix commit",
            "semantic_output_invalid",
        )
    if not isinstance(payload, dict):
        raise AssertionError("semantic payload binding lost its object shape")
    return payload, hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_worker_receipt(
    api: ApiClient,
    repository: str,
    receipt: str,
    head_ref: str,
) -> tuple[list[dict[str, str]], str]:
    validate_receipt_path(receipt)
    try:
        content = fetch_report(api, repository, receipt, head_ref)
        data = json.loads(content)
    except (CloudError, json.JSONDecodeError) as error:
        message = str(error) if isinstance(error, CloudError) else error.msg
        raise CloudError(
            f"malformed marketplace worker validation {receipt}: {message}",
            "malformed_report",
        ) from None
    if not isinstance(data, list) or not data:
        raise CloudError(
            "marketplace worker validation is incomplete",
            "validation_incomplete",
        )
    outcomes: list[dict[str, str]] = []
    for outcome in data:
        if not isinstance(outcome, dict) or set(outcome) != {"command", "outcome"}:
            raise CloudError(
                "marketplace worker validation outcome is malformed",
                "validation_incomplete",
            )
        command = outcome.get("command")
        result = outcome.get("outcome")
        if (
            not isinstance(command, str)
            or not command.strip()
            or result != "passed"
        ):
            raise CloudError(
                "marketplace worker validation contains a failed, skipped, or "
                "incomplete outcome",
                "validation_incomplete",
            )
        if contains_credentials(command):
            raise CloudError(
                "marketplace worker validation contains credentials",
                "credentials_rejected",
            )
        outcomes.append(
            {
                "command": command,
                "outcome": "passed",
            }
        )
    return outcomes, hashlib.sha256(content.encode("utf-8")).hexdigest()


def _missing_report_context(
    error: CloudError,
    task: Mapping[str, object],
    refs: GeneratedRefs,
) -> CloudError:
    parts = [
        str(error),
        f"task {task['id']}",
        f"branch {refs.head}",
    ]
    link = task.get("html_url") or task.get("url")
    if isinstance(link, str) and link:
        parts.append(str(link))
    return CloudError("; ".join(parts), error.code)


def _bind_requested_artifact_identity(
    result: ResultEnvelope,
    options: Options,
    *,
    report_path: str | None,
    receipt: str | None,
) -> None:
    if receipt is not None:
        result.receipt_path = receipt
    if (
        report_path is not None
        and options.policy
        in {
            MARKETPLACE_REPORT_POLICY_SELECTOR,
            *LEGACY_MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS,
        }
    ):
        result.report_path = report_path
    elif (
        report_path is not None
        and options.policy in SEMANTIC_APPLY_REPORT_POLICY_SELECTORS
    ):
        result.semantic_kind = options.semantic_kind
        result.semantic_path = report_path


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
    if result is not None:
        result.schema_version = (
            CANDIDATE_RESULT_SCHEMA_VERSION
            if options.policy in CANDIDATE_POLICY_SELECTORS
            else SEMANTIC_RESULT_SCHEMA_VERSION
            if options.policy == MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR
            else (
                LEGACY_SEMANTIC_RESULT_SCHEMA_VERSION
                if options.policy == MARKETPLACE_APPLY_REPORT_POLICY_V4_SELECTOR
                else (
                    REPORT_RESULT_SCHEMA_VERSION
                    if options.policy
                    in {
                        MARKETPLACE_REPORT_POLICY_SELECTOR,
                        *LEGACY_MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS,
                    }
                    else RESULT_SCHEMA_VERSION
                )
            )
        )
        if options.policy == MARKETPLACE_APPLY_REPORT_POLICY_V4_SELECTOR:
            result.semantic_schema = LEGACY_SEMANTIC_OUTPUT_SCHEMA
        result.mode = mode_name(options)
        result.requested_model = options.model
        result.policy = policy_metadata(options)
        result.application_status = (
            "not_applicable"
            if options.report or options.dispatch_only or options.monitor_only
            else "not_applied"
        )

    if options.report or options.dispatch_only or options.monitor_only:
        root = git.root(cwd)
        repository = git.repository_name(root)
        snapshot = None
    else:
        snapshot = (
            git.snapshot(cwd, allow_detached=True)
            if options.policy == MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR
            else git.snapshot(cwd)
        )
        root = snapshot.root
        repository = snapshot.repository
    if result is not None:
        result.repository = repository
        result.final_local_head = git.head(root)
    if options.policy is not None:
        if options.result_file is None:
            raise AssertionError("policy mode did not receive a result file")
        validate_policy_before_post(options, root, options.result_file)

    base = (
        None
        if options.dispatch_only
        or options.monitor_only
        or options.allow_merged_pr
        else repository_base(api, repository)
    )
    if options.request_id is not None:
        request_id = options.request_id
    elif options.worker_receipt is not None:
        request_id = Path(options.worker_receipt).stem
    elif (
        options.policy in MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS
        and options.prior_result is not None
        and isinstance(options.prior_result.get("report"), dict)
        and isinstance(options.prior_result["report"].get("path"), str)
    ):
        request_id = Path(options.prior_result["report"]["path"]).stem
    else:
        request_id = str(uuid_factory())
    pull_request: PullRequestSnapshot | None = None
    if options.pull_request is not None:
        pull_request = resolve_pull_request(
            runner,
            root,
            repository,
            options.pull_request,
            allow_merged=options.allow_merged_pr,
        )
        if options.allow_merged_pr:
            if snapshot is None:
                raise AssertionError(
                    "historical apply-with-report did not snapshot the worktree"
                )
            expected_branch = f"trask-pr-audit-{pull_request.number}"
            if snapshot.branch != expected_branch:
                raise CloudError(
                    "historical apply-with-report requires branch "
                    f"{expected_branch}, not {snapshot.branch}",
                    "local_drift",
                )
            refreshed = resolve_pull_request(
                runner,
                root,
                repository,
                options.pull_request,
                allow_merged=True,
            )
            require_pr_unchanged(
                pull_request,
                refreshed,
                full_identity=True,
            )
            pull_request = refreshed
            if options.prior_result is None:
                if snapshot.head != pull_request.head_sha:
                    raise CloudError(
                        "historical apply-with-report requires local HEAD to "
                        "equal the merged pull request head SHA",
                        "local_drift",
                    )
            else:
                validate_apply_result_identity(
                    options.prior_result,
                    repository=repository,
                    model=options.model,
                    pull_request=pull_request,
                    snapshot=snapshot,
                )
            git.require_historical_unchanged(snapshot, {snapshot.head})
        elif options.dispatch_only or options.monitor_only:
            refreshed = resolve_pull_request(
                runner, root, repository, options.pull_request
            )
            require_pr_unchanged(pull_request, refreshed)
            pull_request = refreshed
            git.verify_fork_head(root, repository, pull_request)
        elif snapshot is not None:
            if base is None:
                raise AssertionError("code mode did not resolve the repository base")
            tracking_refs = git.fetch_pr_inputs(
                snapshot, base.branch, pull_request, request_id
            )
            git.require_unchanged(snapshot)
            aligned = git.align_to_pr(snapshot, pull_request, tracking_refs)
            print(
                (
                    f"Validated detached checkout at PR "
                    f"#{pull_request.number} head {pull_request.head_sha}."
                    if aligned.branch is None
                    else f"Aligned local branch {aligned.branch} to PR "
                    f"#{pull_request.number} head {pull_request.head_sha}."
                ),
                file=stderr,
            )
            refreshed = resolve_pull_request(
                runner, root, repository, options.pull_request
            )
            require_pr_unchanged(pull_request, refreshed)
            pull_request = refreshed
            snapshot = aligned
            git.require_unchanged(snapshot)
        else:
            git.verify_fork_head(root, repository, pull_request)
    if result is not None:
        result.pull_request = pull_request
    if options.prior_result is not None:
        if options.allow_merged_pr:
            if snapshot is None or pull_request is None:
                raise AssertionError("historical resume lost local or PR identity")
        else:
            validate_dispatch_result_identity(
                options.prior_result,
                repository=repository,
                model=options.model,
                pull_request=pull_request,
            )
    report_path = (
        (
            OUTPUT_REPORT_PATH
            if options.policy in CANDIDATE_POLICY_SELECTORS
            else f"{SEMANTIC_DIRECTORY}/{request_id}.json"
            if options.policy in SEMANTIC_APPLY_REPORT_POLICY_SELECTORS
            else f"{REPORT_DIRECTORY}/{request_id}.md"
        )
        if options.report or options.apply_with_report
        else None
    )
    receipt = (
        options.worker_receipt
        if options.worker_receipt is not None
        else receipt_path(request_id)
        if options.policy == MARKETPLACE_POLICY_SELECTOR
        else None
    )
    if receipt is not None:
        validate_receipt_path(receipt)
    policy_identity: LocalIdentity | None = None
    if options.policy is not None:
        validate_policy_before_post(options, root, options.result_file)
        policy_identity = git.identity(root)
        if result is not None and options.task_id is not None:
            _bind_requested_artifact_identity(
                result,
                options,
                report_path=report_path,
                receipt=receipt,
            )
    submitted_prompt: str | None = None
    if options.task_id is not None:
        initial = get_task(api, repository, options.task_id)
        if options.resume_apply_with_report:
            if pull_request is None or report_path is None:
                raise AssertionError(
                    "interrupted apply-with-report recovery lost required identity"
                )
            validate_interrupted_apply_task(
                initial,
                task_id=options.task_id,
                repository=repository,
                model=options.model,
                pull_request=pull_request,
                request_id=request_id,
                report_path=report_path,
                worker_receipt=receipt,
                policy=str(options.policy),
                semantic_kind=options.semantic_kind,
            )
    else:
        payload = task_payload(
            options,
            report_path,
            pull_request,
            request_id=request_id,
            worker_receipt=receipt,
            repository=repository,
        )
        submitted_prompt_value = payload.get("prompt")
        if not isinstance(submitted_prompt_value, str):
            raise AssertionError("Agent Task payload lost its prompt")
        submitted_prompt = submitted_prompt_value
        if _EXECUTION is not None and options.result_file is not None:
            _EXECUTION.record_dispatch(options.result_file, request_id, repository)
        initial = start_task(
            api,
            repository,
            payload,
        )
    if result is not None:
        if options.task_id is None:
            _bind_requested_artifact_identity(
                result,
                options,
                report_path=report_path,
                receipt=receipt,
            )
        result.task_id = str(initial["id"])
        result.task_state = str(initial["state"])
        link = initial.get("html_url") or initial.get("url")
        result.task_url = link if isinstance(link, str) and link else None
        result.task_base_ref = (
            task_base_ref(pull_request)
            if pull_request is not None
            else base.branch
            if base is not None
            else None
        )
        result.task_base_sha = (
            pull_request.head_sha
            if pull_request is not None
            else base.sha
            if base is not None
            else None
        )
        if _EXECUTION is not None and options.result_file is not None:
            _EXECUTION.record_dispatch(options.result_file, request_id, repository, {
                "id": result.task_id, "url": result.task_url, "state": result.task_state,
            })
    if options.dispatch_only:
        report_metadata(initial, stderr)
        json.dump(initial, stdout, ensure_ascii=False, sort_keys=True)
        stdout.write("\n")
        stdout.flush()
        if result is not None:
            result.application_status = "not_applicable"
            result.final_local_head = git.head(root)
            result.status = "success"
        return 0
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
    if pull_request is not None:
        expected_base = task_base_ref(pull_request)
    else:
        if base is None:
            raise AssertionError("monitored mode did not resolve the repository base")
        expected_base = base.branch
    if pull_request is not None and refs.base is None:
        raise CloudError(
            f"completed task {final['id']} did not report its base branch; "
            f"expected {expected_base}"
        )
    if refs.base is not None and refs.base != expected_base:
        raise CloudError(
            f"task {final['id']} used base branch {refs.base}, but the recorded "
            f"base was {expected_base}"
        )

    tracking_ref: str | None = None
    commits: list[str] | None = None
    worker_history: WorkerHistory | None = None
    policy_report: str | None = None
    semantic_payload: Mapping[str, object] | None = None
    if options.policy is not None:
        if policy_identity is None:
            raise AssertionError("policy mode did not record local identity")
        if options.policy == MARKETPLACE_POLICY_SELECTOR and receipt is None:
            raise AssertionError("validation policy did not allocate a receipt")
        if (
            options.policy
            in {
                MARKETPLACE_REPORT_POLICY_SELECTOR,
                *MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS,
                *CANDIDATE_POLICY_SELECTORS,
            }
            and report_path is None
        ):
            raise AssertionError("structural policy did not allocate a report path")
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
                raise AssertionError("historical apply lost its worktree snapshot")
            git.require_historical_unchanged(snapshot, {snapshot.head})
        if snapshot is None:
            snapshot = WorktreeSnapshot(
                root,
                repository,
                git.matching_remote(root, repository),
                policy_identity.branch or "(detached)",
                policy_identity.head,
            )
        tracking_ref = git.fetch_generated(snapshot, refs.head, request_id)
        generated_head = git.ref_sha(root, tracking_ref)
        recorded_base_sha = (
            pull_request.head_sha if pull_request is not None else base.sha
        )
        all_commits = git.cloud_commits(root, recorded_base_sha, tracking_ref)
        if result is not None:
            result.generated_head = generated_head
            result.cloud_commits = list(all_commits)
        if options.policy in CANDIDATE_POLICY_SELECTORS:
            if submitted_prompt is None:
                raise AssertionError("fresh candidate policy lost its submitted prompt")
            completion = validate_fresh_completion(
                final,
                expected_task_id=str(initial["id"]),
                repository=repository,
                requested_model=options.model,
                expected_prompt=submitted_prompt,
                expected_base_ref=expected_base,
                generated_ref=refs.head,
                raw_task_response_sha256=getattr(
                    api,
                    "last_response_sha256",
                    None,
                ),
            )
            history = git.candidate_history(
                root,
                recorded_base_sha,
                all_commits,
                report_only=(
                    options.policy
                    == MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR
                ),
            )
            expected_generated_head = (
                all_commits[-1] if all_commits else recorded_base_sha
            )
            if generated_head != expected_generated_head:
                raise CloudError(
                    "fetched generated head does not match the derived candidate "
                    "history",
                    "malformed_history",
                )
            code_commit_shas = [
                str(commit["sha"]) for commit in history.code_commits
            ]
            manifest = {
                "schema": CANDIDATE_MANIFEST_SCHEMA,
                "repository": {
                    "name_with_owner": repository,
                },
                "task": {
                    "id": completion["task"]["id"],
                    "session_id": completion["session"]["id"],
                },
                "base": {
                    "ref": expected_base,
                    "sha": recorded_base_sha,
                },
                "generated": {
                    "ref": refs.head,
                    "head_sha": generated_head,
                    "code_tip_sha": history.code_head,
                },
                "code_commits": list(history.code_commits),
                "artifact_commit": history.artifact_commit,
            }
            if result is not None:
                result.cloud_commits = code_commit_shas
                result.candidate_manifest = manifest
                result.completion_evidence = completion
                result.structural_complete = True
                result.application_status = (
                    "not_applicable"
                    if options.policy
                    == MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR
                    else "not_applied"
                )
                result.final_local_head = git.head(root)
                result.status = "success"
            print(
                f"Derived {len(code_commit_shas)} candidate code commit(s) from "
                f"{refs.head} without importing or applying them.",
                file=stderr,
            )
            return 0
        expected_paths = (
            [report_path]
            if options.policy
            in {
                MARKETPLACE_REPORT_POLICY_SELECTOR,
                *MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS,
            }
            else [receipt]
        )
        if (
            options.policy == MARKETPLACE_POLICY_SELECTOR
            and report_path is not None
        ):
            expected_paths.append(report_path)
        worker_history = git.worker_history(
            root,
            recorded_base_sha,
            all_commits,
            expected_paths,
        )
        commits = list(worker_history.code_commits)
        if result is not None:
            result.cloud_commits = commits
            result.receipt_commit = worker_history.receipt_commit
            if (
                report_path is not None
                and options.policy not in SEMANTIC_APPLY_REPORT_POLICY_SELECTORS
            ):
                result.report_path = report_path
                result.report_commit = worker_history.receipt_commit
            elif report_path is not None:
                result.semantic_path = report_path
                result.semantic_commit = worker_history.receipt_commit
        if (
            options.allow_merged_pr
            and generated_head != worker_history.receipt_commit
        ):
            raise CloudError(
                "the generated branch head is not the final artifact commit",
                "malformed_history",
            )
        if options.report and worker_history.code_commits:
            raise CloudError(
                "report-mode marketplace worker created unexpected commits "
                "before its report receipt",
                "unexpected_commits",
            )
        outcomes: list[dict[str, str]] = []
        validation_digest: str | None = None
        if options.policy == MARKETPLACE_POLICY_SELECTOR:
            if receipt is None:
                raise AssertionError("validation policy lost its receipt path")
            outcomes, validation_digest = fetch_worker_receipt(
                api,
                repository,
                receipt,
                worker_history.receipt_commit,
            )
        if (
            report_path is not None
            and options.policy in SEMANTIC_APPLY_REPORT_POLICY_SELECTORS
        ):
            if options.semantic_kind is None:
                raise AssertionError("semantic policy lost its kind")
            try:
                semantic_payload, semantic_digest = fetch_semantic_output(
                    api,
                    repository,
                    report_path,
                    worker_history.receipt_commit,
                    kind=options.semantic_kind,
                    commits=worker_history.code_commits,
                    policy=options.policy,
                )
            except CloudError as error:
                raise _missing_report_context(error, final, refs) from None
            if result is not None:
                result.semantic_payload = semantic_payload
                result.semantic_sha256 = semantic_digest
        elif report_path is not None:
            try:
                policy_report = fetch_report(
                    api,
                    repository,
                    report_path,
                    worker_history.receipt_commit,
                )
                if not policy_report.strip():
                    raise CloudError(
                        "the committed report is empty",
                        "malformed_report",
                    )
            except CloudError as error:
                raise _missing_report_context(error, final, refs) from None
        report_digest = (
            hashlib.sha256(policy_report.encode("utf-8")).hexdigest()
            if policy_report is not None
            else None
        )
        if options.allow_merged_pr and options.prior_result is not None:
            if report_path is None or report_digest is None:
                raise AssertionError("historical resume did not retrieve its report")
            if options.policy in MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS:
                validate_prior_structural_generated_result(
                    options.prior_result,
                    generated_branch=refs.head,
                    generated_head=generated_head,
                    code_commits=worker_history.code_commits,
                    report_path_value=report_path,
                    report_commit=worker_history.receipt_commit,
                    report_sha256=report_digest,
                )
            else:
                if receipt is None or validation_digest is None:
                    raise AssertionError("historical resume lost validation metadata")
                validate_prior_generated_result(
                    options.prior_result,
                    generated_branch=refs.head,
                    generated_head=generated_head,
                    code_commits=worker_history.code_commits,
                    receipt_path_value=receipt,
                    receipt_commit=worker_history.receipt_commit,
                    receipt_sha256=validation_digest,
                    report_path_value=report_path,
                    report_commit=worker_history.receipt_commit,
                    report_sha256=report_digest,
                    validation_outcomes=outcomes,
                )
        if result is not None:
            if options.policy in {
                MARKETPLACE_REPORT_POLICY_SELECTOR,
                *MARKETPLACE_APPLY_REPORT_POLICY_SELECTORS,
            }:
                result.structural_complete = True
            else:
                result.validation_complete = True
                result.validation_outcomes = outcomes
                result.receipt_sha256 = validation_digest
            if (
                report_path is not None
                and policy_report is not None
                and options.policy not in SEMANTIC_APPLY_REPORT_POLICY_SELECTORS
            ):
                result.report_sha256 = report_digest

    if options.monitor_only:
        json.dump(final, stdout, ensure_ascii=False, sort_keys=True)
        stdout.write("\n")
        stdout.flush()
        if result is not None:
            result.application_status = "not_applicable"
            result.final_local_head = git.head(root)
            result.status = "success"
        return 0
    if options.report:
        if policy_report is not None:
            report = policy_report
        else:
            try:
                report = fetch_report(api, repository, str(report_path), refs.head)
            except CloudError as error:
                raise _missing_report_context(error, final, refs) from None
        stdout.write(report)
        stdout.flush()
        if result is not None:
            result.report_path = str(report_path)
            result.report_sha256 = hashlib.sha256(
                report.encode("utf-8")
            ).hexdigest()
            result.application_status = "not_applicable"
            result.final_local_head = git.head(root)
            result.status = "success"
        return 0

    if snapshot is None:
        raise AssertionError("code mode did not record a worktree snapshot")
    recorded_base_sha = (
        pull_request.head_sha if pull_request is not None else base.sha
    )
    if tracking_ref is None:
        tracking_ref = git.fetch_generated(snapshot, refs.head, request_id)
        commits = git.cloud_commits(snapshot.root, recorded_base_sha, tracking_ref)
        if result is not None:
            result.generated_head = git.ref_sha(snapshot.root, tracking_ref)
            result.cloud_commits = commits
    if commits is None:
        raise AssertionError("code mode did not resolve generated commits")
    if options.apply_with_report:
        if pull_request is None:
            raise AssertionError("apply-with-report mode did not resolve a pull request")
        if report_path is None:
            raise AssertionError("apply-with-report mode did not allocate a report path")
        if worker_history is not None:
            history = ReportHistory(
                worker_history.code_head,
                worker_history.code_commits,
                worker_history.receipt_commit,
            )
            report = policy_report
            if (
                options.policy in SEMANTIC_APPLY_REPORT_POLICY_SELECTORS
                and semantic_payload is None
            ):
                raise AssertionError("policy mode did not retrieve semantic output")
            if (
                options.policy not in SEMANTIC_APPLY_REPORT_POLICY_SELECTORS
                and report is None
            ):
                raise AssertionError("policy mode did not retrieve its report")
            if options.policy in {
                MARKETPLACE_POLICY_SELECTOR,
                MARKETPLACE_APPLY_REPORT_POLICY_V1_SELECTOR,
            }:
                git.require_correlated_fix_commits(
                    snapshot.root, history.code_commits
                )
        else:
            try:
                history = git.report_history(
                    snapshot.root, recorded_base_sha, commits, report_path
                )
                report = fetch_report(api, repository, report_path, refs.head)
                if not report.strip():
                    raise CloudError("the committed report is empty")
                git.require_correlated_fix_commits(
                    snapshot.root, history.code_commits
                )
            except CloudError as error:
                raise _missing_report_context(error, final, refs) from None
        if (
            result is not None
            and options.policy not in SEMANTIC_APPLY_REPORT_POLICY_SELECTORS
        ):
            result.cloud_commits = list(history.code_commits)
            result.report_path = report_path
            result.report_commit = history.report_commit
            result.report_sha256 = hashlib.sha256(
                report.encode("utf-8")
            ).hexdigest()
        try:
            if policy_identity is not None:
                validate_policy_before_mutation(
                    git,
                    policy_identity,
                    runner,
                    snapshot.root,
                    repository,
                    options.pull_request,
                    pull_request,
                    allow_merged_pr=options.allow_merged_pr,
                )
            if options.allow_merged_pr:
                git.require_historical_unchanged(snapshot, {snapshot.head})
            git.require_unchanged(snapshot)
        except CloudError as error:
            raise CloudError(
                f"{error}; generated branch {refs.head}; report commit "
                f"{history.report_commit}; fix commits: "
                f"{', '.join(history.code_commits) or 'none'}",
                error.code,
            ) from None
        if options.policy in {
            MARKETPLACE_APPLY_REPORT_POLICY_V3_SELECTOR,
            MARKETPLACE_APPLY_REPORT_POLICY_V4_SELECTOR,
            MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR,
        }:
            print(
                f"Validated {len(history.code_commits)} fix commit(s) and semantic "
                f"commit {history.report_commit} without modifying "
                f"{snapshot.branch}.",
                file=stderr,
            )
            if result is not None:
                result.application_status = "not_applied"
                result.final_local_head = snapshot.head
                result.status = "success"
            return 0
        already_applied = (
            options.allow_merged_pr
            and snapshot.head == history.code_head
            and snapshot.head != recorded_base_sha
        )
        if options.allow_merged_pr and snapshot.head not in {
            recorded_base_sha,
            history.code_head,
        }:
            raise CloudError(
                "historical audit local HEAD is neither the task base nor the "
                "verified last code commit",
                "local_drift",
            )
        if history.code_commits and not already_applied:
            try:
                git.fast_forward(snapshot, history.code_head)
            except CloudError as error:
                raise CloudError(
                    f"{error}; generated branch {refs.head}; report commit "
                    f"{history.report_commit}; fix commits: "
                    f"{', '.join(history.code_commits)}",
                    error.code,
                ) from None
            print(
                f"Fast-forwarded {snapshot.branch} by "
                f"{len(history.code_commits)} fix commit(s) from {refs.head}; "
                f"excluded report commit {history.report_commit}.",
                file=stderr,
            )
        elif already_applied:
            print(
                f"Verified {snapshot.branch} already at the last code commit "
                f"{history.code_head}; excluded report commit "
                f"{history.report_commit}.",
                file=stderr,
            )
        else:
            print(
                f"Agent Task {final['id']} completed with no fix commits; "
                f"excluded report commit {history.report_commit}.",
                file=stderr,
            )
        stdout.write(report)
        stdout.flush()
        if result is not None:
            result.application_status = (
                "applied" if history.code_commits else "no_changes"
            )
            result.final_local_head = (
                history.code_head
                if history.code_commits
                else recorded_base_sha
                if options.allow_merged_pr
                else snapshot.head
            )
            result.status = "success"
        return 0
    if not commits:
        if pull_request is not None:
            try:
                git.require_unchanged(snapshot)
            except CloudError as error:
                raise CloudError(
                    f"{error}; generated branch {refs.head}; no cloud-only commits",
                    error.code,
                ) from None
        print(
            f"Agent Task {final['id']} completed with no cloud-only commits.",
            file=stdout,
        )
        if result is not None:
            result.application_status = "no_changes"
            result.final_local_head = snapshot.head
            result.status = "success"
        return 0

    try:
        if policy_identity is not None:
            validate_policy_before_mutation(
                git,
                policy_identity,
                runner,
                snapshot.root,
                repository,
                options.pull_request,
                pull_request,
            )
        git.require_unchanged(snapshot)
    except CloudError as error:
        raise CloudError(
            f"{error}; generated branch {refs.head}; cloud commits: "
            f"{', '.join(commits)}",
            error.code,
        ) from None
    if pull_request is not None:
        try:
            git.fast_forward(snapshot, tracking_ref)
        except CloudError as error:
            raise CloudError(
                f"{error}; generated branch {refs.head}; cloud commits: "
                f"{', '.join(commits)}",
                error.code,
            ) from None
        print(
            f"Fast-forwarded {snapshot.branch} by {len(commits)} cloud "
            f"commit(s) from {refs.head}.",
            file=stdout,
        )
    else:
        try:
            git.cherry_pick(snapshot, commits)
        except CloudError as error:
            raise CloudError(
                f"{error}; generated branch {refs.head}; cloud commits: "
                f"{', '.join(commits)}",
                error.code,
            ) from None
        print(
            f"Cherry-picked {len(commits)} cloud commit(s) from {refs.head}.",
            file=stdout,
        )
    if result is not None:
        result.application_status = "applied"
        result.final_local_head = git.head(snapshot.root)
        result.status = "success"
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
    if MARKETPLACE_CODE_CANDIDATE_POLICY_SELECTOR in args:
        result.schema_version = CANDIDATE_RESULT_SCHEMA_VERSION
        result.policy = {
            "id": MARKETPLACE_CODE_CANDIDATE_POLICY_ID,
            "version": MARKETPLACE_CODE_CANDIDATE_POLICY_VERSION,
            "sha256": MARKETPLACE_CODE_CANDIDATE_POLICY_HASH,
        }
    elif MARKETPLACE_REPORT_RECOMMENDATION_POLICY_SELECTOR in args:
        result.schema_version = CANDIDATE_RESULT_SCHEMA_VERSION
        result.policy = {
            "id": MARKETPLACE_REPORT_RECOMMENDATION_POLICY_ID,
            "version": MARKETPLACE_REPORT_RECOMMENDATION_POLICY_VERSION,
            "sha256": MARKETPLACE_REPORT_RECOMMENDATION_POLICY_HASH,
        }
    elif MARKETPLACE_POLICY_SELECTOR in args:
        result.policy = {
            "id": MARKETPLACE_POLICY_ID,
            "version": MARKETPLACE_POLICY_VERSION,
            "sha256": MARKETPLACE_POLICY_HASH,
        }
    elif MARKETPLACE_APPLY_REPORT_POLICY_SELECTOR in args:
        result.schema_version = REPORT_RESULT_SCHEMA_VERSION
        result.policy = {
            "id": MARKETPLACE_APPLY_REPORT_POLICY_ID,
            "version": MARKETPLACE_APPLY_REPORT_POLICY_VERSION,
            "sha256": MARKETPLACE_APPLY_REPORT_POLICY_HASH,
        }
    elif MARKETPLACE_APPLY_REPORT_POLICY_V2_SELECTOR in args:
        result.schema_version = REPORT_RESULT_SCHEMA_VERSION
        result.policy = {
            "id": MARKETPLACE_APPLY_REPORT_POLICY_ID,
            "version": MARKETPLACE_APPLY_REPORT_POLICY_V2_VERSION,
            "sha256": MARKETPLACE_APPLY_REPORT_POLICY_V2_HASH,
        }
    elif MARKETPLACE_APPLY_REPORT_POLICY_V1_SELECTOR in args:
        result.schema_version = REPORT_RESULT_SCHEMA_VERSION
        result.policy = {
            "id": MARKETPLACE_APPLY_REPORT_POLICY_ID,
            "version": MARKETPLACE_APPLY_REPORT_POLICY_V1_VERSION,
            "sha256": MARKETPLACE_APPLY_REPORT_POLICY_V1_HASH,
        }
    elif MARKETPLACE_REPORT_POLICY_SELECTOR in args:
        result.schema_version = REPORT_RESULT_SCHEMA_VERSION
        result.policy = {
            "id": MARKETPLACE_REPORT_POLICY_ID,
            "version": MARKETPLACE_REPORT_POLICY_VERSION,
            "sha256": MARKETPLACE_REPORT_POLICY_HASH,
        }
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
                f"Monitoring stopped. Agent Task {progress.task_id} remains remote; "
                f"last state: {state}.",
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
    if result_path is not None:
        try:
            atomic_write_json(result_path, result.as_dict())
        except CloudError as error:
            print(f"error: {error}", file=stderr)
            return 2
    return code


_EXECUTION = None
EXECUTION_SHA256 = "4190af0dcc27e127a88203f67e27ecaa590356fb376db559cec584aa87d92714"


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
    return _load_execution().controller_main(
        lambda: main(stdout=sys.stdout, stderr=sys.stderr), globals(),
    )


if __name__ == "__main__":
    raise SystemExit(execution_main())
