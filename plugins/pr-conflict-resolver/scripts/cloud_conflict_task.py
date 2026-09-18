#!/usr/bin/env python3
"""Run a quarantined GitHub Agent Task for a pinned PR conflict request."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence, TextIO


API_VERSION = "2026-03-10"
ACCEPT = "application/vnd.github+json"
POLL_SECONDS = 60
AGENT_TASK_PROMPT_MAX_CHARACTERS = 28_000
AGENT_TASK_PROMPT_MAX_UTF8_BYTES = 28_000
TASK_PROMPT_HEADROOM_CHARACTERS = 1_000
TASK_PROMPT_HEADROOM_UTF8_BYTES = 1_000
TASK_PROMPT_MAX_CHARACTERS = (
    AGENT_TASK_PROMPT_MAX_CHARACTERS - TASK_PROMPT_HEADROOM_CHARACTERS
)
TASK_PROMPT_MAX_UTF8_BYTES = (
    AGENT_TASK_PROMPT_MAX_UTF8_BYTES - TASK_PROMPT_HEADROOM_UTF8_BYTES
)
EXACT_PATH_EVIDENCE_MAX_COUNT = 64
EXACT_PATH_EVIDENCE_MAX_BYTES = 4_096
PATH_EVIDENCE_BOUNDARY_COUNT = 8
PATH_EVIDENCE_VALUE_MAX_BYTES = 256
MODE = "conflict_with_report"
REQUEST_SCHEMA = {"id": "github.copilot.agent-task-conflict-request", "version": 1}
RESULT_SCHEMA = {"id": "github.copilot.agent-task-conflict-result", "version": 3}
LEGACY_RESULT_SCHEMA = {"id": "github.copilot.agent-task-conflict-result", "version": 1}
RECEIPT_SCHEMA = {"id": "github.copilot.agent-task-conflict-receipt", "version": 2}
LEGACY_RECEIPT_SCHEMA = {
    "id": "github.copilot.agent-task-conflict-receipt",
    "version": 1,
}
SEMANTIC_SCHEMA = {
    "id": "github.copilot.agent-task-conflict-semantic-output",
    "version": 2,
}
POLICY_ID = "marketplace-conflict-worker"
LEGACY_POLICY_VERSION = 1
LEGACY_POLICY_SHA256 = (
    "30c96b070bed7b652ffd9181fd4f74b052f670226dab9693d595338aaf0a9d6a"
)
LEGACY_POLICY = {
    "id": POLICY_ID,
    "version": LEGACY_POLICY_VERSION,
    "sha256": LEGACY_POLICY_SHA256,
}
POLICY_VERSION = 5
POLICY_SPEC = {
    "id": POLICY_ID,
    "version": POLICY_VERSION,
    "execution_backend": "github-agent-tasks-rest",
    "authentication": "local-gh-api",
    "custom_agent": False,
    "local_fallback": False,
    "single_role_code_tip": "verified-task-artifact-parent",
    "multi_role_code_refs": "dispatcher-assigned-request-scoped",
    "user_branch_publication": False,
    "quarantined_refs_only": True,
    "worker_identity_fields": False,
    "semantic_artifact": "minimal-payload-json",
    "semantic_wrapper_owner": "dispatcher",
    "merge_commit_annotations": "dispatcher-derived",
    "semantic_validation_evidence": "command-result",
    "dispatcher_generated_report_receipt": True,
    "require_exact_request_identity": True,
    "require_exact_target_identity": True,
    "require_mechanical_history_proof": True,
    "safe_direct_base_sync_merge_omission": True,
    "require_separate_artifact_commit": True,
    "require_complete_successful_validation": True,
}
POLICY_SHA256 = hashlib.sha256(
    json.dumps(POLICY_SPEC, sort_keys=True, separators=(",", ":")).encode("ascii")
).hexdigest()
POLICY_SELECTOR = f"{POLICY_ID}@{POLICY_VERSION}"
LEGACY_POLICY_SELECTOR = f"{POLICY_ID}@{LEGACY_POLICY_VERSION}"
POLICY = {"id": POLICY_ID, "version": POLICY_VERSION, "sha256": POLICY_SHA256}
MODEL_IDS = {
    "luna": "gpt-5.6-luna",
    "terra": "gpt-5.6-terra",
    "sol": "gpt-5.6-sol",
    "astra": "gpt-6-astra",
}
STRATEGIES = {"merge", "rebase", "native-stack"}
ACTIVE_STATES = {"queued", "in_progress"}
SUCCESS_STATES = {"completed"}
TERMINAL_STATES = {"failed", "timed_out", "cancelled", "waiting_for_user", "idle"}
KNOWN_STATES = ACTIVE_STATES | SUCCESS_STATES | TERMINAL_STATES
ERROR_CODES = {
    "task_failed",
    "interrupted",
    "policy_rejected",
    "credentials_rejected",
    "malformed_result",
    "unexpected_history",
    "validation_failed",
    "stale_target",
    "unsupported_strategy",
    "prompt_too_large",
}
SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
TASK_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
REPO_RE = re.compile(r"\A[^/\s]+/[^/\s]+\Z")
REPORT_DIRECTORY = ".github/agent-task-conflict-reports"
RECEIPT_DIRECTORY = ".github/agent-task-conflict-receipts"
SEMANTIC_DIRECTORY = ".github/agent-task-conflict-semantic"
Runner = Callable[..., subprocess.CompletedProcess[str]]


class ConflictError(RuntimeError):
    """A fail-closed conflict task error."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MalformedCompletedReplacement:
    request_file: Path
    prompt_file: Path
    original_result_file: Path
    resumed_result_file: Path
    request: Mapping[str, object]
    prompt: str
    result: Mapping[str, object]
    task_prompt_sha256: str


@dataclass(frozen=True)
class Options:
    strategy: str
    model: str
    pull_request_url: str
    request_file: Path
    prompt_file: Path
    result_file: Path
    input_result_file: Path | None
    request: Mapping[str, object]
    prompt: str
    prior_result: Mapping[str, object] | None
    replacement: MalformedCompletedReplacement | None = None


@dataclass(frozen=True)
class LocalSnapshot:
    root: Path
    control_root: Path
    repository: str
    remote: str
    branch: str
    head: str
    status: str
    operation: str | None


@dataclass(frozen=True)
class LivePr:
    number: int
    url: str
    state: str
    repository: str
    head_repository: str
    head_ref: str
    head_sha: str
    base_repository: str
    base_ref: str
    base_sha: str


@dataclass(frozen=True)
class RemoteRef:
    role: str
    pr_number: int | None
    repository: str
    ref: str


@dataclass
class Progress:
    task_id: str | None = None
    task_state: str | None = None


@dataclass
class Result:
    schema: Mapping[str, object] = field(default_factory=lambda: RESULT_SCHEMA)
    status: str = "error"
    error: dict[str, str] | None = None
    model: str | None = None
    repository: str | None = None
    task_id: str | None = None
    task_url: str | None = None
    task_state: str | None = None
    task_base_ref: str | None = None
    task_base_sha: str | None = None
    strategy: str | None = None
    request_id: str | None = None
    request_sha256: str | None = None
    pull_request: Mapping[str, object] | None = None
    artifact: Mapping[str, object] | None = None
    code_refs: list[Mapping[str, object]] = field(default_factory=list)
    application_status: str = "not_started"
    validations: list[Mapping[str, str]] = field(default_factory=list)
    policy: Mapping[str, object] = field(default_factory=lambda: POLICY)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": self.status,
            "error": self.error,
            "model": self.model,
            "policy": self.policy,
            "repository": self.repository,
            "task": {
                "id": self.task_id,
                "url": self.task_url,
                "state": self.task_state,
                "base_ref": self.task_base_ref,
                "base_sha": self.task_base_sha,
            },
            "mode": MODE,
            "strategy": self.strategy,
            "request": {
                "id": self.request_id,
                "sha256": self.request_sha256,
            },
            "pull_request": self.pull_request,
            "generated": {
                "artifact": self.artifact,
                "code_refs": self.code_refs,
            },
            "application": {"status": self.application_status},
            "validation": {
                "complete": bool(self.validations),
                "outcomes": self.validations,
            },
        }


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def request_digest(data: Mapping[str, object]) -> str:
    normalized = deepcopy(dict(data))
    normalized["request_sha256"] = ""
    return hashlib.sha256(canonical_json(normalized)).hexdigest()


def object_digest(data: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json(data)).hexdigest()


def contains_credentials(value: str) -> bool:
    patterns = (
        r"(?i)\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{16,}\b",
        r"(?i)\b(?:xox[baprs]|sk-[A-Za-z0-9]+)-[A-Za-z0-9-]{12,}\b",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"(?i)\bAuthorization\s*:\s*(?:Bearer|Basic)\s+\S+",
        r"(?i)\b(?:password|passwd|token|api[_-]?key|secret)\s*[:=]\s*\S+",
        r"(?i)https?://[^/\s:@]+:[^/\s@]+@",
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    )
    return any(re.search(pattern, value) for pattern in patterns)


def require_exact_keys(
    value: object, expected: set[str], description: str
) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ConflictError(
            f"{description} has unexpected or missing fields",
            "policy_rejected",
        )
    return value


def require_sha(value: object, description: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise ConflictError(f"{description} is not a full SHA", "policy_rejected")
    return value


def require_sha256(value: object, description: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ConflictError(f"{description} is not a SHA-256", "policy_rejected")
    return value


def require_repo(value: object, description: str) -> str:
    if not isinstance(value, str) or not REPO_RE.fullmatch(value):
        raise ConflictError(
            f"{description} is not an owner/repository identity",
            "policy_rejected",
        )
    return value


def require_ref(value: object, description: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("-")
        or ".." in value
        or any(character.isspace() for character in value)
    ):
        raise ConflictError(f"{description} is not a valid ref", "policy_rejected")
    return value


def require_path(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConflictError(f"{description} is invalid", "policy_rejected")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ConflictError(f"{description} is unsafe", "policy_rejected")
    return value


def validate_commit_identity(value: object, description: str) -> Mapping[str, object]:
    commit = require_exact_keys(
        value,
        {"sha", "subject", "trailers", "patch_sha256", "paths"},
        description,
    )
    require_sha(commit["sha"], f"{description}.sha")
    if not isinstance(commit["subject"], str) or not commit["subject"].strip():
        raise ConflictError(f"{description}.subject is invalid", "policy_rejected")
    trailers = commit["trailers"]
    if not isinstance(trailers, list) or any(
        not isinstance(item, str) or not item.strip() for item in trailers
    ):
        raise ConflictError(f"{description}.trailers is invalid", "policy_rejected")
    require_sha256(commit["patch_sha256"], f"{description}.patch_sha256")
    paths = commit["paths"]
    if (
        not isinstance(paths, list)
        or paths != sorted(set(paths))
        or any(not isinstance(path, str) for path in paths)
    ):
        raise ConflictError(f"{description}.paths is invalid", "policy_rejected")
    for path in paths:
        require_path(path, f"{description}.paths")
    return commit


def validate_sync_merge_identity(
    value: object, description: str
) -> Mapping[str, object]:
    merge = require_exact_keys(
        value,
        {
            "sha",
            "position",
            "parents",
            "subject",
            "trailers",
            "tree",
            "remerge_diff_sha256",
        },
        description,
    )
    require_sha(merge["sha"], f"{description}.sha")
    if (
        not isinstance(merge["position"], int)
        or isinstance(merge["position"], bool)
        or merge["position"] < 0
    ):
        raise ConflictError(
            f"{description}.position is invalid", "policy_rejected"
        )
    merge_parents = merge["parents"]
    if not isinstance(merge_parents, list) or len(merge_parents) != 2:
        raise ConflictError(
            f"{description}.parents is invalid", "policy_rejected"
        )
    for parent in merge_parents:
        require_sha(parent, f"{description}.parents")
    if not isinstance(merge["subject"], str) or not merge["subject"].strip():
        raise ConflictError(
            f"{description}.subject is invalid", "policy_rejected"
        )
    trailers = merge["trailers"]
    if not isinstance(trailers, list) or any(
        not isinstance(item, str) or not item.strip() for item in trailers
    ):
        raise ConflictError(
            f"{description}.trailers is invalid", "policy_rejected"
        )
    require_sha(merge["tree"], f"{description}.tree")
    require_sha256(
        merge["remerge_diff_sha256"],
        f"{description}.remerge_diff_sha256",
    )
    if merge["remerge_diff_sha256"] != hashlib.sha256(b"").hexdigest():
        raise ConflictError(
            f"{description} carries conflict-resolution changes",
            "policy_rejected",
        )
    return merge


def validate_pr_snapshot(value: object, description: str) -> Mapping[str, object]:
    snapshot = require_exact_keys(
        value,
        {
            "number",
            "url",
            "head_repository",
            "head_ref",
            "head_sha",
            "base_repository",
            "base_ref",
            "base_sha",
        },
        description,
    )
    if (
        not isinstance(snapshot["number"], int)
        or isinstance(snapshot["number"], bool)
        or snapshot["number"] < 1
    ):
        raise ConflictError(f"{description}.number is invalid", "policy_rejected")
    expected_url = (
        f"https://github.com/{snapshot['base_repository']}/pull/"
        f"{snapshot['number']}"
    )
    if snapshot["url"] != expected_url:
        raise ConflictError(
            f"{description}.url is not canonical",
            "policy_rejected",
        )
    require_repo(snapshot["head_repository"], f"{description}.head_repository")
    require_ref(snapshot["head_ref"], f"{description}.head_ref")
    require_sha(snapshot["head_sha"], f"{description}.head_sha")
    require_repo(snapshot["base_repository"], f"{description}.base_repository")
    require_ref(snapshot["base_ref"], f"{description}.base_ref")
    require_sha(snapshot["base_sha"], f"{description}.base_sha")
    return snapshot


def validate_native_stack(value: object) -> Mapping[str, object]:
    stack = require_exact_keys(
        value,
        {"trunk", "members", "outside_dependents"},
        "native_stack",
    )
    trunk = require_exact_keys(
        stack["trunk"], {"ref", "sha"}, "native_stack.trunk"
    )
    require_ref(trunk["ref"], "native_stack.trunk.ref")
    require_sha(trunk["sha"], "native_stack.trunk.sha")
    members = stack["members"]
    if not isinstance(members, list) or not members:
        raise ConflictError("native_stack.members is empty", "policy_rejected")
    numbers: list[int] = []
    for index, member_value in enumerate(members):
        member = require_exact_keys(
            member_value,
            {
                "pr_number",
                "repository",
                "head_ref",
                "head_sha",
                "direct_base_ref",
                "direct_base_sha",
                "retained_base_sha",
                "direct_merge_base",
                "expected_new_parent",
                "old_commits",
                "sync_merges",
                "lease_sha",
            },
            f"native_stack.members[{index}]",
        )
        number = member["pr_number"]
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ConflictError("native stack PR number is invalid", "policy_rejected")
        numbers.append(number)
        require_repo(member["repository"], "native stack repository")
        require_ref(member["head_ref"], "native stack head ref")
        require_sha(member["head_sha"], "native stack head SHA")
        require_ref(member["direct_base_ref"], "native stack direct base ref")
        require_sha(member["direct_base_sha"], "native stack direct base SHA")
        require_sha(member["retained_base_sha"], "native stack retained base SHA")
        require_sha(member["direct_merge_base"], "native stack merge base SHA")
        expected_parent = require_exact_keys(
            member["expected_new_parent"],
            {"role", "old_sha"},
            "native stack expected parent",
        )
        if (
            not isinstance(expected_parent["role"], str)
            or (
                expected_parent["role"] != "trunk"
                and not re.fullmatch(r"member:[1-9][0-9]*", expected_parent["role"])
            )
        ):
            raise ConflictError(
                "native stack expected parent role is invalid",
                "policy_rejected",
            )
        require_sha(expected_parent["old_sha"], "native stack expected parent old SHA")
        require_sha(member["lease_sha"], "native stack lease SHA")
        if member["lease_sha"] != member["head_sha"]:
            raise ConflictError(
                "native stack lease does not match frozen head",
                "policy_rejected",
            )
        commits = member["old_commits"]
        if not isinstance(commits, list) or not commits:
            raise ConflictError(
                "native stack member old_commits is empty",
                "policy_rejected",
            )
        for commit_index, commit in enumerate(commits):
            validate_commit_identity(
                commit,
                f"native_stack.members[{index}].old_commits[{commit_index}]",
            )
        sync_merges = member["sync_merges"]
        if not isinstance(sync_merges, list):
            raise ConflictError(
                "native stack member sync_merges is invalid",
                "policy_rejected",
            )
        for merge_index, merge in enumerate(sync_merges):
            validate_sync_merge_identity(
                merge,
                f"native_stack.members[{index}].sync_merges[{merge_index}]",
            )
        positions = [merge["position"] for merge in sync_merges]
        if positions != sorted(set(positions)):
            raise ConflictError(
                "native stack synchronization merge positions are invalid",
                "policy_rejected",
            )
        if commits[-1]["sha"] != member["head_sha"]:
            raise ConflictError(
                "native stack unique range does not end at member head",
                "policy_rejected",
            )
    if len(numbers) != len(set(numbers)):
        raise ConflictError("native stack PR numbers are duplicated", "policy_rejected")
    dependents = stack["outside_dependents"]
    if not isinstance(dependents, list):
        raise ConflictError(
            "native_stack.outside_dependents is invalid",
            "policy_rejected",
        )
    for index, dependent_value in enumerate(dependents):
        dependent = require_exact_keys(
            dependent_value,
            {
                "pr_number",
                "repository",
                "head_ref",
                "head_sha",
                "base_ref",
                "base_sha",
            },
            f"native_stack.outside_dependents[{index}]",
        )
        if (
            not isinstance(dependent["pr_number"], int)
            or isinstance(dependent["pr_number"], bool)
            or dependent["pr_number"] < 1
        ):
            raise ConflictError(
                "outside dependent PR number is invalid",
                "policy_rejected",
            )
        require_repo(dependent["repository"], "outside dependent repository")
        require_ref(dependent["head_ref"], "outside dependent head ref")
        require_sha(dependent["head_sha"], "outside dependent head SHA")
        require_ref(dependent["base_ref"], "outside dependent base ref")
        require_sha(dependent["base_sha"], "outside dependent base SHA")
    for index, member in enumerate(members):
        expected_ref = trunk["ref"] if index == 0 else members[index - 1]["head_ref"]
        expected_sha = trunk["sha"] if index == 0 else members[index - 1]["head_sha"]
        expected_role = (
            "trunk" if index == 0 else f"member:{members[index - 1]['pr_number']}"
        )
        if (
            member["direct_base_ref"] != expected_ref
            or member["direct_base_sha"] != expected_sha
            or member["expected_new_parent"]
            != {"role": expected_role, "old_sha": expected_sha}
        ):
            raise ConflictError(
                "native stack direct-base relation is inconsistent",
                "policy_rejected",
            )
    member_numbers = set(numbers)
    member_heads = {
        (member["head_ref"], member["head_sha"]) for member in members
    }
    dependent_numbers = [dependent["pr_number"] for dependent in dependents]
    if (
        len(dependent_numbers) != len(set(dependent_numbers))
        or member_numbers & set(dependent_numbers)
        or any(
            (dependent["base_ref"], dependent["base_sha"]) not in member_heads
            for dependent in dependents
        )
    ):
        raise ConflictError(
            "outside dependent guard is inconsistent",
            "policy_rejected",
        )
    return stack


def strategy_can_land(strategy: str, methods: Mapping[str, object]) -> bool:
    if strategy == "merge":
        return methods["merge_commit"] is True or methods["squash_merge"] is True
    return methods["rebase_merge"] is True or methods["squash_merge"] is True


def validate_request(
    data: object,
    *,
    expected_strategy: str,
    expected_model: str,
    expected_pr_url: str,
    expected_policy: Mapping[str, object] = POLICY,
) -> Mapping[str, object]:
    request = require_exact_keys(
        data,
        {
            "schema",
            "request_id",
            "request_sha256",
            "model",
            "policy",
            "repository",
            "pull_request",
            "merge_base",
            "strategy",
            "allowed_paths",
            "iteration",
            "guards",
            "head_commits",
            "native_stack",
        },
        "conflict request",
    )
    if request["schema"] != REQUEST_SCHEMA:
        raise ConflictError("unsupported conflict request schema", "policy_rejected")
    if not isinstance(request["request_id"], str) or not ID_RE.fullmatch(
        request["request_id"]
    ):
        raise ConflictError("invalid conflict request ID", "policy_rejected")
    require_sha256(request["request_sha256"], "request_sha256")
    if request_digest(request) != request["request_sha256"]:
        raise ConflictError("conflict request digest mismatch", "policy_rejected")
    if request["model"] != expected_model or request["policy"] != expected_policy:
        raise ConflictError(
            "conflict request model or policy mismatch",
            "policy_rejected",
        )
    repository = require_repo(request["repository"], "repository")
    pull_request = validate_pr_snapshot(request["pull_request"], "pull_request")
    if (
        pull_request["url"] != expected_pr_url
        or pull_request["base_repository"] != repository
    ):
        raise ConflictError(
            "conflict request PR does not match invocation",
            "policy_rejected",
        )
    require_sha(request["merge_base"], "merge_base")
    if request["strategy"] != expected_strategy:
        raise ConflictError("conflict request strategy mismatch", "unsupported_strategy")
    allowed_paths = request["allowed_paths"]
    if (
        not isinstance(allowed_paths, list)
        or allowed_paths != sorted(set(allowed_paths))
        or any(not isinstance(path, str) for path in allowed_paths)
    ):
        raise ConflictError("allowed_paths is not exact and ordered", "policy_rejected")
    for path in allowed_paths:
        require_path(path, "allowed path")
    iteration = require_exact_keys(
        request["iteration"], {"id", "number", "budget"}, "iteration"
    )
    if (
        not isinstance(iteration["id"], str)
        or not ID_RE.fullmatch(iteration["id"])
        or not isinstance(iteration["number"], int)
        or isinstance(iteration["number"], bool)
        or iteration["number"] < 1
        or not isinstance(iteration["budget"], int)
        or isinstance(iteration["budget"], bool)
        or iteration["budget"] < iteration["number"]
    ):
        raise ConflictError("iteration identity is invalid", "policy_rejected")
    guards = require_exact_keys(
        request["guards"],
        {"merge_methods", "frozen_conflict", "already_satisfied"},
        "guards",
    )
    methods = require_exact_keys(
        guards["merge_methods"],
        {"merge_commit", "rebase_merge", "squash_merge"},
        "guards.merge_methods",
    )
    if any(not isinstance(value, bool) for value in methods.values()) or any(
        not isinstance(guards[name], bool)
        for name in ("frozen_conflict", "already_satisfied")
    ):
        raise ConflictError("repository guards are invalid", "policy_rejected")
    if not strategy_can_land(expected_strategy, methods):
        raise ConflictError(
            f"repository does not allow {expected_strategy} publication",
            "unsupported_strategy",
        )
    head_commits = request["head_commits"]
    if not isinstance(head_commits, list):
        raise ConflictError("head_commits is invalid", "policy_rejected")
    for index, commit in enumerate(head_commits):
        validate_commit_identity(commit, f"head_commits[{index}]")
    if expected_strategy in {"merge", "rebase"}:
        if (
            not head_commits
            or head_commits[-1]["sha"] != pull_request["head_sha"]
            or request["native_stack"] is not None
        ):
            raise ConflictError(
                "single-PR strategy requires head_commits and no native_stack",
                "policy_rejected",
            )
    else:
        if head_commits or request["native_stack"] is None:
            raise ConflictError(
                "native-stack requires only native_stack commit ranges",
                "policy_rejected",
            )
        stack = validate_native_stack(request["native_stack"])
        if any(
            item["repository"] != repository
            for item in [*stack["members"], *stack["outside_dependents"]]
        ):
            raise ConflictError(
                "native stack members and guards must use the target repository",
                "policy_rejected",
            )
    if contains_credentials(canonical_json(request).decode("utf-8")):
        raise ConflictError(
            "conflict request contains credentials",
            "credentials_rejected",
        )
    return request


def read_external_text(path: Path, description: str) -> str:
    if not path.is_absolute():
        raise ConflictError(f"{description} must be absolute", "policy_rejected")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ConflictError(
            f"could not read {description} {path}: {error}",
            "policy_rejected",
        ) from None
    if not content.strip():
        raise ConflictError(f"{description} is empty", "policy_rejected")
    if contains_credentials(content):
        raise ConflictError(
            f"{description} contains credentials",
            "credentials_rejected",
        )
    return content


def read_json_file(path: Path, description: str) -> object:
    content = read_external_text(path, description)
    try:
        return json.loads(content)
    except json.JSONDecodeError as error:
        raise ConflictError(
            f"{description} is not valid JSON: {error.msg}",
            "policy_rejected" if description == "request file" else "malformed_result",
        ) from None


def parse_args(args: Sequence[str]) -> Options:
    values: dict[str, str | bool] = {}
    index = 0
    flags = {"--conflict-with-report"}
    options = {
        "--strategy",
        "--model",
        "--pr",
        "--request-file",
        "--prompt-file",
        "--result-file",
        "--input-result-file",
        "--replace-malformed-request-file",
        "--replace-malformed-prompt-file",
        "--replace-malformed-original-result-file",
        "--replace-malformed-resumed-result-file",
        "--expected-malformed-task-prompt-sha256",
        "--policy",
    }
    while index < len(args):
        token = args[index]
        if token in flags:
            if token in values:
                raise ConflictError(f"{token} may be specified only once", "policy_rejected")
            values[token] = True
            index += 1
            continue
        if token not in options:
            raise ConflictError(f"unknown option {token}", "policy_rejected")
        if token in values or index + 1 >= len(args):
            raise ConflictError(f"{token} requires one value", "policy_rejected")
        values[token] = args[index + 1]
        index += 2
    replacement_options = {
        "--replace-malformed-request-file",
        "--replace-malformed-prompt-file",
        "--replace-malformed-original-result-file",
        "--replace-malformed-resumed-result-file",
        "--expected-malformed-task-prompt-sha256",
    }
    required = flags | (options - {"--input-result-file"} - replacement_options)
    missing = sorted(required - values.keys())
    if missing:
        raise ConflictError(
            f"missing required options: {', '.join(missing)}",
            "policy_rejected",
        )
    if "--input-result-file" in values or replacement_options & values.keys():
        raise ConflictError(
            "resume and malformed-task replacement are disabled; start a fresh "
            "invocation",
            "recovery_disabled",
        )
    if values["--policy"] not in {POLICY_SELECTOR, LEGACY_POLICY_SELECTOR}:
        raise ConflictError("unsupported conflict worker policy", "policy_rejected")
    strategy = str(values["--strategy"])
    if strategy not in STRATEGIES:
        raise ConflictError(f"unsupported strategy {strategy!r}", "unsupported_strategy")
    alias = str(values["--model"])
    if alias not in MODEL_IDS:
        raise ConflictError(f"unsupported model {alias!r}", "policy_rejected")
    pr_url = str(values["--pr"])
    if not re.fullmatch(
        r"https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9][0-9]*",
        pr_url,
    ):
        raise ConflictError("--pr must be a canonical GitHub PR URL", "policy_rejected")
    paths: dict[str, Path | None] = {}
    for name in (
        "--request-file",
        "--prompt-file",
        "--result-file",
        "--input-result-file",
        "--replace-malformed-request-file",
        "--replace-malformed-prompt-file",
        "--replace-malformed-original-result-file",
        "--replace-malformed-resumed-result-file",
    ):
        raw = values.get(name)
        path = Path(str(raw)) if raw is not None else None
        if path is not None and not path.is_absolute():
            raise ConflictError(f"{name} must be absolute", "policy_rejected")
        paths[name] = path
    present_paths = [path.resolve() for path in paths.values() if path is not None]
    if len(present_paths) != len(set(present_paths)):
        raise ConflictError(
            "request, prompt, prior result, and next result files must differ",
            "policy_rejected",
        )
    request_data = read_json_file(paths["--request-file"], "request file")
    request = validate_request(
        request_data,
        expected_strategy=strategy,
        expected_model=MODEL_IDS[alias],
        expected_pr_url=pr_url,
        expected_policy=(
            POLICY
            if values["--policy"] == POLICY_SELECTOR
            else LEGACY_POLICY
        ),
    )
    prompt = read_external_text(paths["--prompt-file"], "prompt file")
    prior = (
        validate_prior_result(
            read_json_file(paths["--input-result-file"], "input result file"),
            request,
        )
        if paths["--input-result-file"] is not None
        else None
    )
    supplied_replacement = replacement_options & values.keys()
    if supplied_replacement and supplied_replacement != replacement_options:
        raise ConflictError(
            "malformed completed replacement options must be supplied together",
            "policy_rejected",
        )
    if supplied_replacement and prior is not None:
        raise ConflictError(
            "malformed completed replacement cannot resume a prior result",
            "policy_rejected",
        )
    replacement = None
    if supplied_replacement:
        expected_prompt_sha256 = str(
            values["--expected-malformed-task-prompt-sha256"]
        )
        if not SHA256_RE.fullmatch(expected_prompt_sha256):
            raise ConflictError(
                "expected malformed task prompt hash is invalid",
                "policy_rejected",
            )
        replacement_request_data = read_json_file(
            paths["--replace-malformed-request-file"],
            "replacement request file",
        )
        replacement_request = validate_request(
            replacement_request_data,
            expected_strategy=strategy,
            expected_model=MODEL_IDS[alias],
            expected_pr_url=pr_url,
        )
        replacement_prompt = read_external_text(
            paths["--replace-malformed-prompt-file"],
            "replacement prompt file",
        )
        original_path = paths["--replace-malformed-original-result-file"]
        resumed_path = paths["--replace-malformed-resumed-result-file"]
        if original_path.read_bytes() != resumed_path.read_bytes():
            raise ConflictError(
                "malformed task resume results are not byte-identical",
                "malformed_result",
            )
        replacement_result = validate_prior_result(
            read_json_file(resumed_path, "replacement resumed result file"),
            replacement_request,
        )
        validate_prior_result(
            read_json_file(original_path, "replacement original result file"),
            replacement_request,
        )
        comparable_keys = set(request) - {
            "request_id",
            "request_sha256",
            "iteration",
        }
        if any(
            request[key] != replacement_request[key]
            for key in comparable_keys
        ):
            raise ConflictError(
                "malformed completed replacement request identity changed",
                "policy_rejected",
            )
        old_iteration = replacement_request["iteration"]
        new_iteration = request["iteration"]
        if (
            new_iteration["number"] != old_iteration["number"] + 1
            or new_iteration["budget"] != old_iteration["budget"]
        ):
            raise ConflictError(
                "malformed completed replacement iteration is invalid",
                "policy_rejected",
            )
        replacement = MalformedCompletedReplacement(
            paths["--replace-malformed-request-file"],
            paths["--replace-malformed-prompt-file"],
            original_path,
            resumed_path,
            replacement_request,
            replacement_prompt,
            replacement_result,
            expected_prompt_sha256,
        )
    return Options(
        strategy,
        MODEL_IDS[alias],
        pr_url,
        paths["--request-file"],
        paths["--prompt-file"],
        paths["--result-file"],
        paths["--input-result-file"],
        request,
        prompt,
        prior,
        replacement,
    )


def validate_prior_result(
    data: object, request: Mapping[str, object]
) -> Mapping[str, object]:
    try:
        return _validate_prior_result(data, request)
    except ConflictError:
        raise ConflictError("prior result is malformed", "malformed_result") from None


def _validate_prior_result(
    data: object, request: Mapping[str, object]
) -> Mapping[str, object]:
    result = require_exact_keys(
        data,
        {
            "schema",
            "status",
            "error",
            "model",
            "policy",
            "repository",
            "task",
            "mode",
            "strategy",
            "request",
            "pull_request",
            "generated",
            "application",
            "validation",
        },
        "prior conflict result",
    )
    if (
        result["schema"] != RESULT_SCHEMA
        or result["status"] not in {"success", "error", "interrupted"}
        or result["model"] != request["model"]
        or result["policy"] != POLICY
        or result["repository"] != request["repository"]
        or result["mode"] != MODE
        or result["strategy"] != request["strategy"]
        or result["request"]
        != {"id": request["request_id"], "sha256": request["request_sha256"]}
        or result["pull_request"] != request["pull_request"]
    ):
        raise ConflictError("prior result identity mismatch", "malformed_result")
    task = require_exact_keys(
        result["task"], {"id", "url", "state", "base_ref", "base_sha"}, "prior task"
    )
    if not isinstance(task["id"], str) or not TASK_ID_RE.fullmatch(task["id"]):
        raise ConflictError(
            "resume has no reusable Agent Task identity",
            "malformed_result",
        )
    if (
        task["state"] not in KNOWN_STATES
        or (
            task["url"] is not None
            and (
                not isinstance(task["url"], str)
                or not task["url"].strip()
                or not re.fullmatch(
                    r"https://(?:api\.)?github\.com/\S+", task["url"]
                )
                or contains_credentials(task["url"])
            )
        )
    ):
        raise ConflictError("prior task identity is malformed", "malformed_result")
    if task["base_ref"] != request["pull_request"]["head_sha"] or task[
        "base_sha"
    ] != request["pull_request"]["head_sha"]:
        raise ConflictError("prior task base mismatch", "malformed_result")
    generated = require_exact_keys(
        result["generated"], {"artifact", "code_refs"}, "prior generated result"
    )
    if not isinstance(generated["code_refs"], list):
        raise ConflictError("prior code refs are malformed", "malformed_result")
    expected_role_order = [role for role, _, _ in expected_roles(request)]
    if [
        code_ref.get("role") if isinstance(code_ref, dict) else None
        for code_ref in generated["code_refs"]
    ] != expected_role_order and generated["code_refs"]:
        raise ConflictError("prior code refs are malformed", "malformed_result")
    try:
        if generated["code_refs"]:
            code_refs_from_receipt(
                {
                    "generated_refs": [
                        {"ref": code_ref, "sha256": object_digest(code_ref)}
                        for code_ref in generated["code_refs"]
                    ]
                },
                request,
            )
            for code_ref in generated["code_refs"]:
                validate_prior_code_ref(code_ref, request)
        elif result["status"] == "success":
            raise ConflictError("prior code refs are missing", "malformed_result")
        validate_prior_artifact(generated["artifact"], request, result["status"])
    except (ConflictError, TypeError, ValueError):
        raise ConflictError("prior generated identity is malformed", "malformed_result") from None
    application = require_exact_keys(
        result["application"], {"status"}, "prior application"
    )
    if application["status"] not in {
        "not_started",
        "quarantined_refs",
        "no_changes",
    }:
        raise ConflictError("prior application is malformed", "malformed_result")
    validation = require_exact_keys(
        result["validation"], {"complete", "outcomes"}, "prior validation"
    )
    if not isinstance(validation["complete"], bool) or not isinstance(
        validation["outcomes"], list
    ):
        raise ConflictError("prior validation is malformed", "malformed_result")
    if result["status"] == "success":
        if (
            result["error"] is not None
            or application["status"] not in {"quarantined_refs", "no_changes"}
            or validation["complete"] is not True
        ):
            raise ConflictError("prior success is malformed", "malformed_result")
        try:
            if result["schema"] == RESULT_SCHEMA and result["policy"] == POLICY:
                validate_semantic_validations(validation["outcomes"])
            else:
                validate_validations(validation["outcomes"])
        except ConflictError:
            raise ConflictError(
                "prior validation is malformed", "malformed_result"
            ) from None
    else:
        error = require_exact_keys(
            result["error"], {"code", "message"}, "prior error"
        )
        if (
            error["code"] not in ERROR_CODES
            or not isinstance(error["message"], str)
            or not error["message"].strip()
            or (result["status"] == "interrupted" and error["code"] != "interrupted")
            or application["status"] != "not_started"
            or validation != {"complete": False, "outcomes": []}
        ):
            raise ConflictError("prior error is malformed", "malformed_result")
    return result


def validate_prior_code_ref(
    code_ref: Mapping[str, object], request: Mapping[str, object]
) -> None:
    commits = code_ref["commits"]
    if request["strategy"] == "merge":
        if not commits or any(
            not isinstance(commit, str) or not SHA_RE.fullmatch(commit)
            for commit in commits
        ):
            raise ConflictError("prior merge commits are malformed", "malformed_result")
        return
    if not commits:
        raise ConflictError("prior commit mappings are missing", "malformed_result")
    for item in commits:
        mapping = require_exact_keys(
            item,
            {
                "old_sha",
                "new_sha",
                "subject",
                "trailers",
                "patch_sha256",
                "conflict_paths",
                "companion_paths",
                "unaffected_path_digests",
                "rationale",
            },
            "prior commit mapping",
        )
        require_sha(mapping["old_sha"], "prior mapping old SHA")
        require_sha(mapping["new_sha"], "prior mapping new SHA")
        require_sha256(mapping["patch_sha256"], "prior mapping patch digest")
        if (
            not isinstance(mapping["subject"], str)
            or not mapping["subject"]
            or not isinstance(mapping["trailers"], list)
            or any(not isinstance(trailer, str) for trailer in mapping["trailers"])
            or not isinstance(mapping["conflict_paths"], list)
            or not isinstance(mapping["companion_paths"], list)
            or any(
                not isinstance(path, str)
                for path in [
                    *mapping["conflict_paths"],
                    *mapping["companion_paths"],
                ]
            )
            or not isinstance(mapping["unaffected_path_digests"], dict)
            or any(
                not isinstance(path, str)
                or not isinstance(digest, str)
                or not SHA256_RE.fullmatch(digest)
                for path, digest in mapping["unaffected_path_digests"].items()
            )
            or not isinstance(mapping["rationale"], str)
        ):
            raise ConflictError("prior commit mapping is malformed", "malformed_result")


def validate_prior_artifact(
    artifact: object,
    request: Mapping[str, object],
    status: object,
) -> None:
    if artifact is None:
        if status == "success":
            raise ConflictError("prior artifact is missing", "malformed_result")
        return
    value = require_exact_keys(
        artifact, {"branch", "head_sha", "report", "receipt"}, "prior artifact"
    )
    if not isinstance(value["branch"], str) or not value["branch"].strip():
        raise ConflictError("prior artifact branch is malformed", "malformed_result")
    head_sha = require_sha(value["head_sha"], "prior artifact head")
    expected_paths = artifact_paths(request["request_id"])
    for name, identity, expected_path in zip(
        ("report", "receipt"),
        (value["report"], value["receipt"]),
        expected_paths,
    ):
        expected_keys = (
            {"path", "commit", "sha256"}
            if name == "report"
            else {"path", "commit"}
        )
        item = require_exact_keys(
            identity, expected_keys, "prior artifact file"
        )
        if (
            item["path"] != expected_path
            or item["commit"] != head_sha
            or (
                name == "report"
                and item["sha256"] is not None
                and (
                    not isinstance(item["sha256"], str)
                    or not SHA256_RE.fullmatch(item["sha256"])
                )
            )
            or (
                name == "report"
                and status == "success"
                and item["sha256"] is None
            )
        ):
            raise ConflictError("prior artifact file is malformed", "malformed_result")


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def stable_process_directory() -> Path:
    directory = Path(sys.executable).resolve().parent
    if not directory.is_dir():
        raise ConflictError(
            "Python executable directory is unavailable",
            "stale_target",
        )
    return directory


def run_process(
    runner: Runner,
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    process_command = list(command)
    process_cwd = cwd
    if process_command and process_command[0] == "git" and cwd is not None:
        process_command = ["git", "-C", str(cwd), *process_command[1:]]
        process_cwd = stable_process_directory()
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "check": False,
        "cwd": str(process_cwd) if process_cwd is not None else None,
    }
    if input_text is not None:
        kwargs["input"] = input_text
    if os.name == "nt":
        kwargs["creationflags"] = _creation_flags()
    try:
        return runner(process_command, **kwargs)
    except UnicodeError as error:
        raise ConflictError(
            f"{command[0]} returned invalid UTF-8: {error}",
            "malformed_result",
        ) from None
    except OSError as error:
        raise ConflictError(
            f"could not run {command[0]}: {error}",
            "stale_target",
        ) from None


def checked(
    runner: Runner,
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    code: str = "stale_target",
) -> str:
    result = run_process(runner, command, cwd=cwd, input_text=input_text)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ConflictError(
            f"{' '.join(command)} failed: {detail}",
            code,
        )
    return result.stdout


def git(
    runner: Runner, root: Path, *args: str, code: str = "unexpected_history"
) -> str:
    return checked(runner, ["git", *args], cwd=root, code=code)


def parse_git_root_output(value: str) -> Path:
    if value.endswith("\r\n"):
        path = value[:-2]
    elif value.endswith("\n"):
        path = value[:-1]
    else:
        raise ConflictError(
            "git returned a repository root without a terminal line ending",
            "stale_target",
        )
    if (
        not path
        or "\0" in path
        or "\r" in path
        or "\n" in path
        or path.endswith((" ", "\t"))
    ):
        raise ConflictError(
            "git returned a malformed repository root",
            "stale_target",
        )
    return Path(path).resolve()


def repository_root(runner: Runner, cwd: Path) -> Path:
    root = parse_git_root_output(
        git(runner, cwd, "rev-parse", "--show-toplevel")
    )
    verified = parse_git_root_output(
        git(runner, root, "rev-parse", "--show-toplevel")
    )
    if verified != root:
        raise ConflictError(
            "repository root changed during preflight",
            "stale_target",
        )
    return root


def repository_from_url(value: str) -> str:
    patterns = (
        r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?\Z",
        r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?\Z",
        r"ssh://git@github\.com/([^/]+/[^/]+?)(?:\.git)?/?\Z",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, value.strip(), re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def operation(runner: Runner, root: Path) -> str | None:
    for marker, name in (
        ("MERGE_HEAD", "merge"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
    ):
        result = run_process(
            runner,
            ["git", "rev-parse", "--verify", "--quiet", marker],
            cwd=root,
        )
        if result.returncode == 0:
            return name
    for marker, name in (
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
        ("sequencer", "sequencer"),
    ):
        path = git(runner, root, "rev-parse", "--git-path", marker).strip()
        resolved = Path(path) if Path(path).is_absolute() else root / path
        if resolved.exists():
            return name
    return None


def local_snapshot(
    runner: Runner,
    cwd: Path,
    *,
    control_root: Path,
    expected_repository: str,
    expected_head: str,
    expected_branch: str | None,
    allow_detached: bool,
) -> LocalSnapshot:
    require_sha(expected_head, "expected local HEAD")
    if expected_branch is not None:
        require_ref(expected_branch, "expected local branch")
    root = repository_root(runner, cwd)
    repository = checked(
        runner,
        [
            "gh",
            "repo",
            "view",
            expected_repository,
            "--json",
            "nameWithOwner",
            "--jq",
            ".nameWithOwner",
        ],
        cwd=control_root,
    ).strip()
    require_repo(repository, "authenticated repository")
    if repository.casefold() != expected_repository.casefold():
        raise ConflictError("authenticated repository changed", "stale_target")
    remote = ""
    for candidate in git(runner, root, "remote").splitlines():
        url = git(runner, root, "remote", "get-url", candidate).strip()
        if repository_from_url(url).casefold() == repository.casefold():
            remote = candidate
            break
    if not remote:
        raise ConflictError("no matching authenticated remote", "stale_target")
    branch_result = run_process(
        runner,
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=root,
    )
    branch = branch_result.stdout.strip()
    head = git(runner, root, "rev-parse", "--verify", "HEAD").strip().lower()
    require_sha(head, "local HEAD")
    if head != expected_head:
        raise ConflictError("local HEAD does not match the pinned source", "stale_target")
    if branch:
        if branch_result.returncode != 0 or (
            expected_branch is not None and branch != expected_branch
        ):
            raise ConflictError(
                "local branch does not match the pinned source",
                "stale_target",
            )
    elif branch_result.returncode == 0 or not allow_detached:
        raise ConflictError("a checked-out branch is required", "stale_target")
    status = git(
        runner,
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
        code="stale_target",
    )
    current_operation = operation(runner, root)
    if status or current_operation:
        raise ConflictError(
            "conflict task requires a clean operation-free worktree",
            "stale_target",
        )
    return LocalSnapshot(
        root,
        control_root,
        repository,
        remote,
        branch,
        head,
        status,
        current_operation,
    )


def require_local_unchanged(
    runner: Runner, expected: LocalSnapshot, quarantined: Sequence[str] = ()
) -> None:
    current = local_snapshot(
        runner,
        expected.root,
        control_root=expected.control_root,
        expected_repository=expected.repository,
        expected_head=expected.head,
        expected_branch=expected.branch or None,
        allow_detached=not bool(expected.branch),
    )
    if current != expected:
        raise ConflictError("local repository state changed", "stale_target")
    for ref in quarantined:
        if not ref.startswith("refs/cloud-conflict-tasks/"):
            raise ConflictError("unsafe quarantine ref", "policy_rejected")


def resolve_pr(
    runner: Runner, root: Path, repository: str, number: int
) -> LivePr:
    fields = (
        "number,url,state,baseRefName,baseRefOid,headRepository,"
        "headRefName,headRefOid"
    )
    output = checked(
        runner,
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            repository,
            "--json",
            fields,
        ],
        cwd=root,
        code="stale_target",
    )
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        raise ConflictError("GitHub returned malformed PR data", "stale_target") from None
    if not isinstance(data, dict):
        raise ConflictError("GitHub returned malformed PR data", "stale_target")
    head_repository = data.get("headRepository")
    name_with_owner = (
        head_repository.get("nameWithOwner")
        if isinstance(head_repository, dict)
        else None
    )
    if not isinstance(name_with_owner, str):
        owner = (
            head_repository.get("owner", {}).get("login")
            if isinstance(head_repository, dict)
            and isinstance(head_repository.get("owner"), dict)
            else None
        )
        name = head_repository.get("name") if isinstance(head_repository, dict) else None
        name_with_owner = (
            f"{owner}/{name}"
            if isinstance(owner, str) and isinstance(name, str)
            else ""
        )
    base_ref = data.get("baseRefName")
    if not isinstance(base_ref, str) or not base_ref:
        raise ConflictError("open pull request identity is invalid", "stale_target")
    base_data = parse_json_output(
        checked(
            runner,
            [
                "gh",
                "api",
                f"repos/{repository}/git/ref/heads/"
                f"{urllib.parse.quote(base_ref, safe='')}",
            ],
            cwd=root,
            code="stale_target",
        ),
        "base branch data",
    )
    base_object = base_data.get("object")
    base_sha = (
        base_object.get("sha")
        if isinstance(base_object, dict)
        else None
    )
    live = LivePr(
        data.get("number"),
        data.get("url"),
        data.get("state"),
        repository,
        name_with_owner,
        data.get("headRefName"),
        str(data.get("headRefOid", "")).lower(),
        repository,
        base_ref,
        str(base_sha or "").lower(),
    )
    if (
        live.number != number
        or live.state != "OPEN"
        or live.url != f"https://github.com/{repository}/pull/{number}"
        or not REPO_RE.fullmatch(live.head_repository)
        or not isinstance(live.head_ref, str)
        or not live.head_ref
        or not SHA_RE.fullmatch(live.head_sha)
        or not isinstance(live.base_ref, str)
        or not live.base_ref
        or not SHA_RE.fullmatch(live.base_sha)
    ):
        raise ConflictError("open pull request identity is invalid", "stale_target")
    return live


def request_pr_as_live(request: Mapping[str, object]) -> LivePr:
    value = request["pull_request"]
    return LivePr(
        value["number"],
        value["url"],
        "OPEN",
        request["repository"],
        value["head_repository"],
        value["head_ref"],
        value["head_sha"],
        value["base_repository"],
        value["base_ref"],
        value["base_sha"],
    )


def parse_json_output(value: str, description: str) -> Mapping[str, object]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        raise ConflictError(
            f"GitHub returned malformed {description}",
            "stale_target",
        ) from None
    if not isinstance(data, dict):
        raise ConflictError(
            f"GitHub returned non-object {description}",
            "stale_target",
        )
    return data


def require_target_fresh(
    runner: Runner, snapshot: LocalSnapshot, request: Mapping[str, object]
) -> None:
    expected = request_pr_as_live(request)
    current = resolve_pr(
        runner, snapshot.control_root, expected.repository, expected.number
    )
    if current != expected:
        raise ConflictError("pull request target changed", "stale_target")
    repository_data = parse_json_output(
        checked(
            runner,
            [
                "gh",
                "repo",
                "view",
                request["repository"],
                "--json",
                "mergeCommitAllowed,rebaseMergeAllowed,squashMergeAllowed",
            ],
            cwd=snapshot.control_root,
            code="stale_target",
        ),
        "repository merge-method data",
    )
    methods = request["guards"]["merge_methods"]
    actual_methods = {
        "merge_commit": repository_data.get("mergeCommitAllowed"),
        "rebase_merge": repository_data.get("rebaseMergeAllowed"),
        "squash_merge": repository_data.get("squashMergeAllowed"),
    }
    if actual_methods != methods:
        raise ConflictError("repository merge-method guards changed", "stale_target")
    compare = parse_json_output(
        checked(
            runner,
            [
                "gh",
                "api",
                f"repos/{request['repository']}/compare/"
                f"{request['pull_request']['base_sha']}..."
                f"{request['pull_request']['head_sha']}",
            ],
            cwd=snapshot.control_root,
            code="stale_target",
        ),
        "merge-base data",
    )
    merge_base_commit = compare.get("merge_base_commit")
    if (
        not isinstance(merge_base_commit, dict)
        or merge_base_commit.get("sha") != request["merge_base"]
    ):
        raise ConflictError("merge base changed", "stale_target")
    if request["strategy"] == "native-stack":
        stack = request["native_stack"]
        expected_trunk = stack["trunk"]
        trunk = parse_json_output(
            checked(
                runner,
                [
                    "gh",
                    "api",
                    f"repos/{request['repository']}/git/ref/heads/"
                    f"{urllib.parse.quote(expected_trunk['ref'], safe='')}",
                ],
                cwd=snapshot.control_root,
                code="stale_target",
            ),
            "native stack trunk data",
        )
        trunk_object = trunk.get("object")
        if (
            not isinstance(trunk_object, dict)
            or trunk_object.get("sha") != expected_trunk["sha"]
        ):
            raise ConflictError("native stack trunk changed", "stale_target")
        for item in [*stack["members"], *stack["outside_dependents"]]:
            live = resolve_pr(
                runner,
                snapshot.control_root,
                item["repository"],
                item["pr_number"],
            )
            expected_head_ref = item["head_ref"]
            expected_head_sha = item["head_sha"]
            expected_base_ref = item.get("direct_base_ref", item.get("base_ref"))
            expected_base_sha = item.get("direct_base_sha", item.get("base_sha"))
            if (
                live.head_ref != expected_head_ref
                or live.head_sha != expected_head_sha
                or live.base_ref != expected_base_ref
                or live.base_sha != expected_base_sha
                or (
                    "lease_sha" in item
                    and item["lease_sha"] != live.head_sha
                )
            ):
                raise ConflictError("native stack identity changed", "stale_target")


def already_satisfied(
    runner: Runner, snapshot: LocalSnapshot, request: Mapping[str, object]
) -> bool:
    guards = request["guards"]
    if not guards["already_satisfied"] or guards["frozen_conflict"]:
        return False
    if request["strategy"] in {"merge", "rebase"}:
        return parents(
            runner,
            snapshot.root,
            request["pull_request"]["head_sha"],
        ) == [request["pull_request"]["base_sha"]]
    stack = request["native_stack"]
    return all(
        parents(runner, snapshot.root, member["head_sha"])
        == [member["expected_new_parent"]["old_sha"]]
        for member in stack["members"]
    )


def fetch_pinned_inputs(
    runner: Runner,
    snapshot: LocalSnapshot,
    request: Mapping[str, object],
) -> list[str]:
    request_id = request["request_id"]
    inputs: list[tuple[str, str, str]] = [
        (
            "source-head",
            f"refs/pull/{request['pull_request']['number']}/head",
            request["pull_request"]["head_sha"],
        ),
        (
            "source-base",
            f"refs/heads/{request['pull_request']['base_ref']}",
            request["pull_request"]["base_sha"],
        ),
    ]
    if request["strategy"] == "native-stack":
        stack = request["native_stack"]
        inputs.append(
            ("trunk", f"refs/heads/{stack['trunk']['ref']}", stack["trunk"]["sha"])
        )
        inputs.extend(
            (
                f"member-{member['pr_number']}",
                f"refs/pull/{member['pr_number']}/head",
                member["head_sha"],
            )
            for member in stack["members"]
        )
    targets: list[str] = []
    for role, source, expected_sha in inputs:
        target = quarantine_ref(request_id, f"input-{role}")
        git(
            runner,
            snapshot.root,
            "fetch",
            "--no-tags",
            snapshot.remote,
            f"+{source}:{target}",
            code="stale_target",
        )
        actual = git(
            runner,
            snapshot.root,
            "rev-parse",
            "--verify",
            target,
            code="stale_target",
        ).strip().lower()
        if actual != expected_sha:
            raise ConflictError(
                f"pinned input {role} changed while fetched",
                "stale_target",
            )
        targets.append(target)
    require_local_unchanged(runner, snapshot, targets)
    return targets


def api_json(
    runner: Runner,
    root: Path,
    method: str,
    endpoint: str,
    payload: Mapping[str, object] | None = None,
) -> object:
    command = [
        "gh",
        "api",
        "--method",
        method,
        "-H",
        f"Accept: {ACCEPT}",
        "-H",
        f"X-GitHub-Api-Version: {API_VERSION}",
    ]
    input_text = None
    if payload is not None:
        command.extend(["--input", "-"])
        input_text = canonical_json(payload).decode("utf-8")
    command.append(endpoint)
    output = checked(
        runner,
        command,
        cwd=root,
        input_text=input_text,
        code="task_failed",
    )
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        raise ConflictError("GitHub returned malformed task JSON", "task_failed") from None


def validate_task(value: object, expected_id: str | None = None) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ConflictError("Agent Task response is malformed", "task_failed")
    task_id = value.get("id")
    state = value.get("state")
    if (
        not isinstance(task_id, str)
        or not TASK_ID_RE.fullmatch(task_id)
        or state not in KNOWN_STATES
        or (expected_id is not None and task_id != expected_id)
    ):
        raise ConflictError("Agent Task identity is malformed", "task_failed")
    return value


def artifact_paths(request_id: str) -> tuple[str, str]:
    return (
        f"{REPORT_DIRECTORY}/{request_id}.md",
        f"{RECEIPT_DIRECTORY}/{request_id}.json",
    )


def value_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def compact_path_evidence(paths: Sequence[str]) -> Mapping[str, object]:
    values = list(paths)
    digest = value_digest(values)
    encoded = canonical_json(values)
    if (
        len(values) <= EXACT_PATH_EVIDENCE_MAX_COUNT
        and len(encoded) <= EXACT_PATH_EVIDENCE_MAX_BYTES
    ):
        return {
            "representation": "exact",
            "count": len(values),
            "sha256": digest,
            "paths": values,
        }

    def marker(path: str) -> Mapping[str, object]:
        encoded_path = path.encode("utf-8")
        return {
            "path": (
                path
                if len(encoded_path) <= PATH_EVIDENCE_VALUE_MAX_BYTES
                else None
            ),
            "utf8_sha256": hashlib.sha256(encoded_path).hexdigest(),
            "utf8_bytes": len(encoded_path),
        }

    boundary = [
        *values[:PATH_EVIDENCE_BOUNDARY_COUNT],
        *values[-PATH_EVIDENCE_BOUNDARY_COUNT:],
    ]
    return {
        "representation": "digest_with_boundary_samples",
        "count": len(values),
        "sha256": digest,
        "boundary": [marker(path) for path in dict.fromkeys(boundary)],
        "complete_values_in_retained_request": True,
    }


def compact_commit_evidence(
    commit: Mapping[str, object],
    *,
    include_paths: bool = False,
) -> Mapping[str, object]:
    evidence: dict[str, object] = {
        "sha": commit["sha"],
        "patch_sha256": commit["patch_sha256"],
        "retained_evidence_sha256": value_digest(commit),
    }
    if include_paths:
        evidence["paths"] = {
            "count": len(commit["paths"]),
            "sha256": value_digest(commit["paths"]),
        }
    return evidence


def semantic_path(request_id: str) -> str:
    return f"{SEMANTIC_DIRECTORY}/{request_id}.json"


def assigned_code_ref(request_id: str, role: str) -> str:
    safe_request = re.sub(r"[^A-Za-z0-9._-]", "-", request_id)
    safe_role = re.sub(r"[^A-Za-z0-9._-]", "-", role)
    return f"copilot/conflict-{safe_request}-{safe_role}"


def assigned_code_refs(request: Mapping[str, object]) -> list[RemoteRef]:
    return [
        RemoteRef(
            role,
            pr_number,
            repository,
            assigned_code_ref(str(request["request_id"]), role),
        )
        for role, pr_number, repository in expected_roles(request)
    ]


def compact_request_contract(
    request: Mapping[str, object],
    *,
    include_per_commit_paths: bool = False,
) -> Mapping[str, object]:
    stack = request["native_stack"]
    compact_stack = None
    if isinstance(stack, Mapping):
        compact_stack = {
            "trunk": stack["trunk"],
            "members": [
                {
                    **{
                        key: member[key]
                        for key in (
                            "pr_number",
                            "repository",
                            "head_ref",
                            "head_sha",
                            "direct_base_ref",
                            "direct_base_sha",
                            "retained_base_sha",
                            "direct_merge_base",
                            "expected_new_parent",
                            "lease_sha",
                        )
                    },
                    "old_commits": [
                        compact_commit_evidence(
                            commit,
                            include_paths=include_per_commit_paths,
                        )
                        for commit in member["old_commits"]
                    ],
                    "sync_merges": member["sync_merges"],
                }
                for member in stack["members"]
            ],
            "outside_dependents": stack["outside_dependents"],
        }
    return {
        "schema": request["schema"],
        "request_id": request["request_id"],
        "request_sha256": request["request_sha256"],
        "retained_request_utf8_bytes": len(canonical_json(request)),
        "model": request["model"],
        "policy": request["policy"],
        "repository": request["repository"],
        "pull_request": request["pull_request"],
        "merge_base": request["merge_base"],
        "strategy": request["strategy"],
        "iteration": request["iteration"],
        "guards": request["guards"],
        "allowed_paths": compact_path_evidence(request["allowed_paths"]),
        "head_commits": [
            compact_commit_evidence(
                commit,
                include_paths=include_per_commit_paths,
            )
            for commit in request["head_commits"]
        ],
        "native_stack": compact_stack,
    }


def compact_receipt_contract(
    request: Mapping[str, object],
) -> Mapping[str, object]:
    generated_refs = []
    for role, pr_number, repository in expected_roles(request):
        old_sha, base_sha, base_ref, lease_sha, old_commits = code_ref_base(
            request, role
        )
        generated_refs.append(
            {
                "role": role,
                "pr_number": pr_number,
                "repository": repository,
                "old_sha": old_sha,
                "base_ref": base_ref,
                "base_sha": base_sha,
                "lease_sha": lease_sha,
                "old_commit_count": len(old_commits),
                "commits_contract": (
                    {
                        "kind": "ordered_publishable_commit_shas",
                    }
                    if request["strategy"] == "merge"
                    else {
                        "kind": "ordered_old_to_new_mappings",
                        "count": len(old_commits),
                    }
                ),
            }
        )
    return {
        "top_level_keys": [
            "schema",
            "request",
            "policy",
            "model",
            "mode",
            "strategy",
            "repository",
            "pull_request",
            "generated_refs",
            "validation_complete",
            "validation",
        ],
        "schema": LEGACY_RECEIPT_SCHEMA,
        "request": {
            "id": request["request_id"],
            "sha256": request["request_sha256"],
        },
        "policy": LEGACY_POLICY,
        "model": request["model"],
        "mode": MODE,
        "strategy": request["strategy"],
        "repository": request["repository"],
        "pull_request": request["pull_request"],
        "generated_refs": generated_refs,
        "generated_ref_wrapper_keys": ["ref", "sha256"],
        "generated_ref_keys": [
            "role",
            "pr_number",
            "repository",
            "ref",
            "old_sha",
            "new_sha",
            "base_ref",
            "base_sha",
            "lease_sha",
            "commits",
        ],
        "commit_mapping_keys": [
            "old_sha",
            "new_sha",
            "subject",
            "trailers",
            "patch_sha256",
            "conflict_paths",
            "companion_paths",
            "unaffected_path_digests",
            "rationale",
        ],
        "validation_complete": True,
        "validation_item_keys": ["command", "status", "detail"],
        "validation_required_status": "passed",
    }


def receipt_contract_template(
    request: Mapping[str, object],
) -> Mapping[str, object]:
    generated_refs: list[Mapping[str, object]] = []
    for role, pr_number, repository in expected_roles(request):
        old_sha, base_sha, base_ref, lease_sha, old_commits = code_ref_base(
            request, role
        )
        if request["strategy"] == "merge":
            commits: list[object] = ["<oldest-to-newest publishable commit SHA>"]
        else:
            commits = [
                {
                    "old_sha": old["sha"],
                    "new_sha": "<rewritten commit SHA>",
                    "subject": old["subject"],
                    "trailers": old["trailers"],
                    "patch_sha256": "<helper-compatible whitespace-preserving digest>",
                    "conflict_paths": [],
                    "companion_paths": [],
                    "unaffected_path_digests": {},
                    "rationale": "",
                }
                for old in old_commits
            ]
        generated_refs.append(
            {
                "ref": {
                    "role": role,
                    "pr_number": pr_number,
                    "repository": repository,
                    "ref": "<generated branch>",
                    "old_sha": old_sha,
                    "new_sha": "<generated tip SHA>",
                    "base_ref": base_ref,
                    "base_sha": (
                        base_sha
                        if request["strategy"] != "native-stack"
                        else "<actual preceding generated tip, or pinned trunk SHA>"
                    ),
                    "lease_sha": lease_sha,
                    "commits": commits,
                },
                "sha256": "<SHA-256 of canonical compact sorted ref object>",
            }
        )
    return {
        "schema": LEGACY_RECEIPT_SCHEMA,
        "request": {
            "id": request["request_id"],
            "sha256": request["request_sha256"],
        },
        "policy": LEGACY_POLICY,
        "model": request["model"],
        "mode": MODE,
        "strategy": request["strategy"],
        "repository": request["repository"],
        "pull_request": request["pull_request"],
        "generated_refs": generated_refs,
        "validation_complete": True,
        "validation": [
            {
                "command": "<exact command or deterministic proof>",
                "status": "passed",
                "detail": "<concise result>",
            }
        ],
    }


def policy_prompt(
    options: Options,
    *,
    include_per_commit_paths: bool = False,
) -> str:
    if options.request["policy"] == POLICY:
        path = semantic_path(options.request["request_id"])
        compact_request = compact_request_contract(
            options.request,
            include_per_commit_paths=include_per_commit_paths,
        )
        refs = (
            [
                {
                    "role_index": index,
                    "role": remote.role,
                    "branch": remote.ref,
                    "annotation_count": len(
                        code_ref_base(options.request, remote.role)[4]
                    ),
                }
                for index, remote in enumerate(
                    assigned_code_refs(options.request),
                    start=1,
                )
            ]
            if options.request["strategy"] == "native-stack"
            else []
        )
        code_locator_policy = (
            "Publish each resolved member tip to the exact request-scoped branch "
            "assigned below, in role order. These branch names are locators only; "
            "the dispatcher fetches them into quarantine and derives every SHA and "
            "ordered mapping from Git history. Do not publish any other code branch.\n"
            f"{json.dumps(refs, ensure_ascii=False, sort_keys=True)}\n"
            if refs
            else (
                "Do not publish a duplicate code branch. Commit the resolved "
                "single-role history directly before the final semantic artifact "
                "commit on the Agent Task branch. The dispatcher derives the code "
                "tip only from that final commit's verified sole parent and derives "
                "every SHA and ordered mapping from Git history.\n"
            )
        )
        shape = {
            "summary": "<nonempty explanation of the resolution>",
            "validation": [
                {
                    "command": "<exact command or deterministic proof>",
                    "result": "passed",
                }
            ],
        }
        if options.strategy != "merge":
            shape["commit_annotations"] = [
                [
                    {
                        "conflict_paths": [],
                        "companion_paths": [],
                        "rationale": "",
                    }
                ]
            ]
        return (
            f"{options.prompt.rstrip()}\n\n"
            "----- marketplace conflict worker policy -----\n"
            f"Policy: {POLICY_SELECTOR}\n"
            f"Policy SHA-256: {POLICY_SHA256}\n"
            f"Mode: {MODE}\n"
            f"Strategy: {options.strategy}\n"
            "The dispatcher owns and binds every request, repository, pull request, "
            "frozen head/base, model, policy, task, session, generated-ref, commit, "
            "report, receipt, and completion identity. Do not author or echo those "
            "fields in the semantic artifact.\n"
            "Compact immutable task contract (input evidence only): "
            f"{canonical_json(compact_request).decode('utf-8')}\n"
            f"{code_locator_policy}"
            "Create one final single-parent task artifact commit on the Agent Task "
            f"branch. Its only changed path must be `{path}` and its parent must be "
            "the final mechanically verified code tip. Write exactly the minimal "
            "JSON payload below. The dispatcher adds the semantic schema and kind; "
            "do not author them. For merge, omit `commit_annotations`; the dispatcher "
            "derives the empty positional annotation from verified history. For "
            "rebase/native-stack, `commit_annotations` is positional: one array per "
            "assigned role and one entry per frozen old commit. An unchanged rewritten commit uses empty "
            "path arrays and an empty rationale; a conflict-touched commit must name "
            "its conflict paths, any companion paths, and a concrete rationale. Do "
            "not include SHAs, refs, roles, request identity, validation completion, "
            "or any report/receipt/envelope fields. Missing or malformed semantic "
            "output fails closed. Validation entries are untrusted evidence and must "
            "contain only the exact command and explicit `result: passed`; the "
            "dispatcher never invents validation status or detail.\n"
            f"{json.dumps(shape, ensure_ascii=False, sort_keys=True)}\n"
            "----- marketplace conflict worker policy -----"
        )
    if options.request["policy"] != LEGACY_POLICY:
        raise ConflictError("unsupported conflict worker policy", "policy_rejected")
    report_path, receipt_path = artifact_paths(options.request["request_id"])
    compact_request = compact_request_contract(
        options.request,
        include_per_commit_paths=include_per_commit_paths,
    )
    compact_receipt = compact_receipt_contract(options.request)
    return (
        f"{options.prompt.rstrip()}\n\n"
        "----- marketplace conflict worker policy -----\n"
        f"Policy: {POLICY_SELECTOR}\n"
        f"Policy SHA-256: {POLICY_SHA256}\n"
        f"Mode: {MODE}\n"
        f"Strategy: {options.strategy}\n"
        "The full retained local request is immutable evidence identified by "
        f"request SHA-256 {options.request['request_sha256']}. The compact contract "
        "below contains every execution identity plus explicit exact or digest "
        "representations of larger retained evidence. Never infer a replacement "
        "identity or treat a digest summary as omitted permission.\n"
        "Canonical evidence digests use SHA-256 over UTF-8 JSON with sorted keys, "
        "comma and colon separators, and non-ASCII values preserved. Boundary "
        "sample utf8_sha256 values hash the raw UTF-8 path bytes.\n"
        "Compact immutable task contract: "
        f"{canonical_json(compact_request).decode('utf-8')}\n"
        "Compact required receipt contract: "
        f"{canonical_json(compact_receipt).decode('utf-8')}\n"
        "Reconstruct retained commit subjects, trailers, parents, paths, and patches "
        "from the exact pinned SHAs with git show, git rev-list, git diff-tree, git "
        "diff --binary --full-index, and git merge-tree as applicable. Verify their "
        "compact evidence hashes before resolving. The dispatcher validates the "
        "complete retained request, generated history, report, receipt, paths, and "
        "patch identities after the task; compact prompt evidence never weakens that "
        "validation.\n"
        "A native-stack member may list direct-base synchronization merges. The "
        "dispatcher proved each has exactly two parents, its second parent belongs "
        "to the current direct-base ancestry, and its remerge diff is empty. These "
        "merges are topology-only base updates: omit their merge commits while "
        "rebasing, map every listed old linear commit one-to-one and in order, and "
        "never omit or flatten any other merge.\n"
        "Do not read, request, print, persist, or transmit credentials, local "
        "environment values, cookies, tokens, keys, or authorization headers. "
        "Do not invoke a custom_agent or local fallback. Do not update any user "
        "branch or pull request, reply to reviews, resolve threads, or choose a "
        "strategy. Publish only the receipt-declared generated code refs and the "
        "distinct task artifact ref.\n"
        f"Commit the complete report at {report_path} and the exact v1 receipt "
        f"at {receipt_path} in the sole final artifact commit. The artifact "
        "commit must be a single-parent child of the final requested code tip "
        "and change exactly those two paths. Do not add commits afterward.\n"
        "Every validation outcome must be complete, ordered, and passed. "
        "Account for every old and new commit in the report and receipt.\n"
        "----- marketplace conflict worker policy -----"
    )


def validated_task_prompt(options: Options) -> str:
    prompt = policy_prompt(options)
    characters = len(prompt)
    utf8_bytes = len(prompt.encode("utf-8"))
    if (
        characters > TASK_PROMPT_MAX_CHARACTERS
        or utf8_bytes > TASK_PROMPT_MAX_UTF8_BYTES
    ):
        raise ConflictError(
            "Agent Task prompt_too_large: compact problem statement is "
            f"{characters} characters and {utf8_bytes} UTF-8 bytes; submission "
            "limits with reserved headroom are "
            f"{TASK_PROMPT_MAX_CHARACTERS} characters and "
            f"{TASK_PROMPT_MAX_UTF8_BYTES} UTF-8 bytes below Agent Task limits of "
            f"{AGENT_TASK_PROMPT_MAX_CHARACTERS} characters and "
            f"{AGENT_TASK_PROMPT_MAX_UTF8_BYTES} UTF-8 bytes",
            "prompt_too_large",
        )
    return prompt


def validate_malformed_completed_replacement(
    runner: Runner,
    snapshot: LocalSnapshot,
    replacement: MalformedCompletedReplacement,
) -> None:
    request = replacement.request
    result = replacement.result
    task_result = result["task"]
    report_path, receipt_path = artifact_paths(request["request_id"])
    recorded_artifact = result["generated"]["artifact"]
    expected_artifact = {
        "branch": recorded_artifact["branch"],
        "head_sha": request["pull_request"]["head_sha"],
        "report": {
            "path": report_path,
            "commit": request["pull_request"]["head_sha"],
            "sha256": None,
        },
        "receipt": {
            "path": receipt_path,
            "commit": request["pull_request"]["head_sha"],
        },
    }
    error = result["error"]
    if (
        result["status"] != "error"
        or error["code"] != "unexpected_history"
        or receipt_path not in error["message"]
        or "does not exist" not in error["message"]
        or result["generated"]
        != {"artifact": expected_artifact, "code_refs": []}
        or result["application"] != {"status": "not_started"}
        or result["validation"] != {"complete": False, "outcomes": []}
        or task_result["state"] != "completed"
    ):
        raise ConflictError(
            "replacement result is not an exact malformed completed task",
            "policy_rejected",
        )
    prompt_options = Options(
        request["strategy"],
        request["model"],
        request["pull_request"]["url"],
        replacement.request_file,
        replacement.prompt_file,
        replacement.resumed_result_file,
        None,
        request,
        replacement.prompt,
        None,
    )
    current_prompt = policy_prompt(prompt_options)
    legacy_prompt = policy_prompt(
        prompt_options,
        include_per_commit_paths=True,
    )
    expected_prompt = next(
        (
            prompt
            for prompt in (current_prompt, legacy_prompt)
            if hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            == replacement.task_prompt_sha256
        ),
        None,
    )
    if expected_prompt is None:
        raise ConflictError(
            "replacement task prompt hash does not match",
            "policy_rejected",
        )
    task = get_task(runner, snapshot, task_result["id"])
    sessions = task.get("sessions")
    if (
        task["state"] != "completed"
        or not isinstance(sessions, list)
        or len(sessions) != 1
        or not isinstance(sessions[0], dict)
    ):
        raise ConflictError(
            "replacement Agent Task is not exactly completed",
            "task_failed",
        )
    session = sessions[0]
    if (
        session.get("task_id") != task_result["id"]
        or session.get("state") != "completed"
        or session.get("model") != f"sweagent-capi:{request['model']}"
        or session.get("base_ref") != request["pull_request"]["head_sha"]
        or session.get("prompt") != expected_prompt
    ):
        raise ConflictError(
            "replacement Agent Task session identity changed",
            "task_failed",
        )
    remote_artifact = discover_artifact_ref(task, request)
    expected_task_artifacts = [
        {
            "provider": "github",
            "type": "branch",
            "data": {
                "base_ref": request["pull_request"]["head_sha"],
                "head_ref": remote_artifact.ref,
            },
        }
    ]
    if (
        remote_artifact.ref != recorded_artifact["branch"]
        or task.get("artifacts") != expected_task_artifacts
        or session.get("head_ref") != remote_artifact.ref
    ):
        raise ConflictError(
            "replacement Agent Task artifact identity changed",
            "task_failed",
        )
    _, artifact_sha = fetch_quarantined(
        runner,
        snapshot,
        remote_artifact,
        request["request_id"],
    )
    if artifact_sha != request["pull_request"]["head_sha"]:
        raise ConflictError(
            "replacement Agent Task generated repository changes",
            "unexpected_history",
        )
    artifact_paths_present = [
        value
        for value in git(
            runner,
            snapshot.root,
            "ls-tree",
            "-r",
            "--name-only",
            artifact_sha,
            "--",
            report_path,
            receipt_path,
        ).splitlines()
        if value
    ]
    if artifact_paths_present:
        raise ConflictError(
            "replacement Agent Task artifact paths exist",
            "unexpected_history",
        )


def start_task(
    runner: Runner, snapshot: LocalSnapshot, options: Options
) -> Mapping[str, object]:
    payload = {
        "prompt": validated_task_prompt(options),
        "model": options.model,
        "create_pull_request": False,
        "base_ref": options.request["pull_request"]["head_sha"],
    }
    return validate_task(
        api_json(
            runner,
            snapshot.control_root,
            "POST",
            f"agents/repos/{snapshot.repository}/tasks",
            payload,
        )
    )


def get_task(
    runner: Runner, snapshot: LocalSnapshot, task_id: str
) -> Mapping[str, object]:
    return validate_task(
        api_json(
            runner,
            snapshot.control_root,
            "GET",
            f"agents/repos/{snapshot.repository}/tasks/"
            f"{urllib.parse.quote(task_id, safe='')}",
        ),
        task_id,
    )


def monitor_task(
    runner: Runner,
    snapshot: LocalSnapshot,
    initial: Mapping[str, object],
    progress: Progress,
    sleep: Callable[[float], None],
) -> Mapping[str, object]:
    task = initial
    progress.task_id = str(task["id"])
    while True:
        state = str(task["state"])
        progress.task_state = state
        if state in SUCCESS_STATES:
            return task
        if state in TERMINAL_STATES:
            raise ConflictError(
                f"Agent Task {task['id']} ended in state {state}",
                "task_failed",
            )
        sleep(POLL_SECONDS)
        task = get_task(runner, snapshot, str(task["id"]))


def expected_roles(request: Mapping[str, object]) -> list[tuple[str, int | None, str]]:
    if request["strategy"] == "native-stack":
        return [
            (f"member:{member['pr_number']}", member["pr_number"], member["repository"])
            for member in request["native_stack"]["members"]
        ]
    return [("code", request["pull_request"]["number"], request["repository"])]


def discover_artifact_ref(
    task: Mapping[str, object], request: Mapping[str, object]
) -> RemoteRef:
    heads: set[str] = set()
    for artifact in task.get("artifacts") or []:
        if (
            not isinstance(artifact, dict)
            or artifact.get("provider") != "github"
            or artifact.get("type") != "branch"
            or not isinstance(artifact.get("data"), dict)
        ):
            continue
        data = artifact["data"]
        ref = data.get("head_ref")
        if not isinstance(ref, str) or not ref:
            raise ConflictError("artifact branch identity is malformed", "unexpected_history")
        heads.add(ref.removeprefix("refs/heads/"))
    for session in task.get("sessions") or []:
        if not isinstance(session, dict):
            raise ConflictError("task session is malformed", "unexpected_history")
        ref = session.get("head_ref")
        if isinstance(ref, str) and ref:
            heads.add(ref.removeprefix("refs/heads/"))
    if len(heads) != 1:
        raise ConflictError(
            "task did not return one exact artifact branch",
            "unexpected_history",
        )
    return RemoteRef(
        "artifact",
        None,
        request["repository"],
        next(iter(heads)),
    )


def discover_semantic_artifact_ref(
    task: Mapping[str, object], request: Mapping[str, object]
) -> RemoteRef:
    artifacts = task.get("artifacts")
    sessions = task.get("sessions")
    if (
        task.get("state") != "completed"
        or not isinstance(artifacts, list)
        or len(artifacts) != 1
        or not isinstance(artifacts[0], dict)
        or not isinstance(sessions, list)
        or len(sessions) != 1
        or not isinstance(sessions[0], dict)
    ):
        raise ConflictError(
            "completed task artifact identity is malformed",
            "task_failed",
        )
    artifact = artifacts[0]
    data = artifact.get("data")
    session = sessions[0]
    task_id = task.get("id")
    if (
        artifact.get("provider") != "github"
        or artifact.get("type") != "branch"
        or not isinstance(data, dict)
        or data.get("base_ref") != request["pull_request"]["head_sha"]
        or session.get("task_id") != task_id
        or session.get("state") != "completed"
        or session.get("model") != f"sweagent-capi:{request['model']}"
        or session.get("base_ref") != request["pull_request"]["head_sha"]
    ):
        raise ConflictError(
            "completed task session identity changed",
            "task_failed",
        )
    artifact_head_ref = require_ref(
        data.get("head_ref"),
        "task artifact head ref",
    ).removeprefix("refs/heads/")
    session_head_ref = require_ref(
        session.get("head_ref"),
        "task session head ref",
    ).removeprefix("refs/heads/")
    task_head_ref = task.get("head_ref")
    if task_head_ref is not None:
        task_head_ref = require_ref(
            task_head_ref,
            "task head ref",
        ).removeprefix("refs/heads/")
    if (
        artifact_head_ref != session_head_ref
        or (
            isinstance(task_head_ref, str)
            and task_head_ref != artifact_head_ref
        )
    ):
        raise ConflictError(
            "task artifact and session branches differ",
            "unexpected_history",
        )
    return RemoteRef(
        "artifact",
        None,
        request["repository"],
        artifact_head_ref,
    )


def code_refs_from_receipt(
    receipt: Mapping[str, object],
    request: Mapping[str, object],
) -> tuple[list[RemoteRef], dict[str, Mapping[str, object]]]:
    entries = receipt.get("generated_refs")
    if not isinstance(entries, list):
        raise ConflictError("receipt generated refs are malformed", "validation_failed")
    by_role: dict[str, Mapping[str, object]] = {}
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"ref", "sha256"}
            or not isinstance(entry["ref"], dict)
            or entry["sha256"] != object_digest(entry["ref"])
        ):
            raise ConflictError(
                "receipt generated ref or digest is malformed",
                "validation_failed",
            )
        ref = require_exact_keys(
            entry["ref"],
            {
                "role",
                "pr_number",
                "repository",
                "ref",
                "old_sha",
                "new_sha",
                "base_ref",
                "base_sha",
                "lease_sha",
                "commits",
            },
            "receipt generated code ref",
        )
        role = ref["role"]
        if not isinstance(role, str) or role in by_role:
            raise ConflictError(
                "receipt generated ref role is invalid",
                "validation_failed",
            )
        if (
            not isinstance(ref["pr_number"], int)
            or isinstance(ref["pr_number"], bool)
            or ref["pr_number"] < 1
            or not isinstance(ref["ref"], str)
            or not ref["ref"]
            or not isinstance(ref["commits"], list)
        ):
            raise ConflictError(
                "receipt generated ref identity is malformed",
                "validation_failed",
            )
        require_repo(ref["repository"], "receipt generated repository")
        require_sha(ref["old_sha"], "receipt generated old SHA")
        require_sha(ref["new_sha"], "receipt generated new SHA")
        require_ref(ref["base_ref"], "receipt generated base ref")
        require_sha(ref["base_sha"], "receipt generated base SHA")
        require_sha(ref["lease_sha"], "receipt generated lease SHA")
        by_role[role] = ref
    expected = expected_roles(request)
    if set(by_role) != {role for role, _, _ in expected}:
        raise ConflictError(
            "receipt generated refs are missing or contain extras",
            "unexpected_history",
        )
    code_refs: list[RemoteRef] = []
    for role, pr_number, repository in expected:
        ref = by_role[role]
        if ref["pr_number"] != pr_number or ref["repository"] != repository:
            raise ConflictError("generated code ref identity mismatch", "unexpected_history")
        code_refs.append(
            RemoteRef(role, pr_number, repository, str(ref["ref"]))
        )
    return code_refs, by_role


def quarantine_ref(request_id: str, role: str) -> str:
    safe_role = re.sub(r"[^A-Za-z0-9._-]", "-", role)
    return f"refs/cloud-conflict-tasks/{request_id}/{safe_role}"


def fetch_quarantined(
    runner: Runner,
    snapshot: LocalSnapshot,
    remote_ref: RemoteRef,
    request_id: str,
    *,
    allow_commit_sha: bool = False,
) -> tuple[str, str]:
    if remote_ref.repository.casefold() != snapshot.repository.casefold():
        raise ConflictError(
            "generated ref repository is not the authenticated repository",
            "unexpected_history",
        )
    target = quarantine_ref(request_id, remote_ref.role)
    if allow_commit_sha and SHA_RE.fullmatch(remote_ref.ref):
        resolved = git(
            runner,
            snapshot.root,
            "rev-parse",
            "--verify",
            f"{remote_ref.ref}^{{commit}}",
        ).strip().lower()
        if resolved != remote_ref.ref:
            raise ConflictError(
                "generated commit SHA does not resolve exactly",
                "unexpected_history",
            )
        git(runner, snapshot.root, "update-ref", target, resolved)
    else:
        check = run_process(
            runner,
            ["git", "check-ref-format", f"refs/heads/{remote_ref.ref}"],
            cwd=snapshot.root,
        )
        if check.returncode != 0:
            raise ConflictError(
                "generated branch name is invalid",
                "unexpected_history",
            )
        git(
            runner,
            snapshot.root,
            "fetch",
            "--no-tags",
            snapshot.remote,
            f"+refs/heads/{remote_ref.ref}:{target}",
        )
    sha = git(runner, snapshot.root, "rev-parse", "--verify", target).strip().lower()
    require_sha(sha, "quarantined ref")
    return target, sha


def parents(runner: Runner, root: Path, commit: str) -> list[str]:
    line = git(runner, root, "rev-list", "--parents", "-n", "1", commit).strip()
    parts = line.lower().split()
    if not parts or parts[0] != commit:
        raise ConflictError("commit parent identity is malformed", "unexpected_history")
    return parts[1:]


def prove_native_stack_member_input(
    runner: Runner,
    root: Path,
    member: Mapping[str, object],
) -> None:
    chain = [
        value.strip().lower()
        for value in git(
            runner,
            root,
            "rev-list",
            "--reverse",
            "--first-parent",
            f"{member['retained_base_sha']}..{member['head_sha']}",
        ).splitlines()
        if value.strip()
    ]
    sync_by_position = {
        merge["position"]: merge for merge in member["sync_merges"]
    }
    linear = iter(member["old_commits"])
    for position, commit in enumerate(chain):
        merge = sync_by_position.get(position)
        commit_parents = parents(runner, root, commit)
        if merge is None:
            try:
                old = next(linear)
            except StopIteration as error:
                raise ConflictError(
                    "native stack linear history has extra commits",
                    "unexpected_history",
                ) from error
            if commit != old["sha"] or len(commit_parents) != 1:
                raise ConflictError(
                    "native stack linear commit identity changed",
                    "unexpected_history",
                )
            continue
        if (
            commit != merge["sha"]
            or commit_parents != merge["parents"]
            or len(commit_parents) != 2
        ):
            raise ConflictError(
                "native stack synchronization merge identity changed",
                "unexpected_history",
            )
        ancestry = run_process(
            runner,
            [
                "git",
                "merge-base",
                "--is-ancestor",
                commit_parents[1],
                member["direct_base_sha"],
            ],
            cwd=root,
        )
        remerge_diff = git(
            runner,
            root,
            "show",
            "--remerge-diff",
            "--format=",
            "--no-ext-diff",
            "--binary",
            commit,
        )
        if (
            ancestry.returncode != 0
            or remerge_diff
            or merge["tree"]
            != git(runner, root, "show", "-s", "--format=%T", commit).strip()
            or merge["subject"]
            != git(runner, root, "show", "-s", "--format=%s", commit).strip()
            or merge["trailers"]
            != [
                line
                for line in git(
                    runner,
                    root,
                    "show",
                    "-s",
                    "--format=%(trailers:only,unfold)",
                    commit,
                ).splitlines()
                if line
            ]
            or merge["remerge_diff_sha256"]
            != hashlib.sha256(remerge_diff.encode("utf-8")).hexdigest()
        ):
            raise ConflictError(
                "native stack synchronization merge proof changed",
                "unexpected_history",
            )
    try:
        next(linear)
    except StopIteration:
        pass
    else:
        raise ConflictError(
            "native stack linear history is incomplete",
            "unexpected_history",
        )
    if set(sync_by_position) != {
        position
        for position, commit in enumerate(chain)
        if len(parents(runner, root, commit)) == 2
    }:
        raise ConflictError(
            "native stack synchronization merge set is incomplete",
            "unexpected_history",
        )


def ordered_commits(
    runner: Runner, root: Path, base: str, tip: str
) -> list[str]:
    ancestry = run_process(
        runner,
        ["git", "merge-base", "--is-ancestor", base, tip],
        cwd=root,
    )
    if ancestry.returncode != 0:
        raise ConflictError("generated ref is not rooted at pinned base", "unexpected_history")
    values = [
        value.strip().lower()
        for value in git(
            runner,
            root,
            "rev-list",
            "--reverse",
            "--topo-order",
            f"{base}..{tip}",
        ).splitlines()
        if value.strip()
    ]
    if any(not SHA_RE.fullmatch(value) for value in values):
        raise ConflictError("generated commit list is malformed", "unexpected_history")
    return values


def first_parent_chain(
    runner: Runner, root: Path, base: str, tip: str
) -> list[str]:
    chain: list[str] = []
    current = tip
    while current != base:
        require_sha(current, "generated first-parent commit")
        chain.append(current)
        commit_parents = parents(runner, root, current)
        if not commit_parents:
            raise ConflictError(
                "generated first-parent history does not reach pinned head",
                "unexpected_history",
            )
        current = commit_parents[0]
    chain.reverse()
    return chain


def commit_subject(runner: Runner, root: Path, commit: str) -> str:
    return git(runner, root, "show", "-s", "--format=%s", commit).rstrip("\n")


def commit_trailers(runner: Runner, root: Path, commit: str) -> list[str]:
    output = git(
        runner,
        root,
        "show",
        "-s",
        "--format=%(trailers:only,unfold)",
        commit,
    )
    return [line for line in output.splitlines() if line]


def changed_paths(runner: Runner, root: Path, commit: str) -> list[str]:
    output = git(
        runner,
        root,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "-z",
        commit,
    )
    return sorted(path for path in output.split("\0") if path)


def patch_sha256(runner: Runner, root: Path, parent: str, commit: str) -> str:
    output = git(
        runner,
        root,
        "diff",
        "--no-ext-diff",
        "--no-renames",
        "--binary",
        "--full-index",
        "--unified=0",
        parent,
        commit,
    )
    return hashlib.sha256(normalize_patch(output).encode("utf-8")).hexdigest()


def normalize_patch(output: str) -> str:
    normalized: list[str] = []
    for line in output.splitlines(keepends=True):
        if line.startswith("index "):
            continue
        if line.startswith("@@ "):
            suffix = line.split("@@", 2)[-1]
            normalized.append(f"@@ @@{suffix}")
            continue
        normalized.append(line)
    return "".join(normalized)


def path_patch_sha256(
    runner: Runner, root: Path, parent: str, commit: str, path: str
) -> str:
    output = git(
        runner,
        root,
        "diff",
        "--no-ext-diff",
        "--no-renames",
        "--binary",
        "--full-index",
        "--unified=0",
        parent,
        commit,
        "--",
        path,
    )
    return hashlib.sha256(normalize_patch(output).encode("utf-8")).hexdigest()


def verify_commit_identity(
    runner: Runner,
    root: Path,
    expected: Mapping[str, object],
    *,
    require_linear: bool,
) -> None:
    commit_parents = parents(runner, root, expected["sha"])
    if require_linear and len(commit_parents) != 1:
        raise ConflictError("frozen old commit is not linear", "stale_target")
    if not commit_parents:
        raise ConflictError("frozen old commit has no parent", "stale_target")
    if (
        commit_subject(runner, root, expected["sha"]) != expected["subject"]
        or commit_trailers(runner, root, expected["sha"]) != expected["trailers"]
        or changed_paths(runner, root, expected["sha"]) != expected["paths"]
        or patch_sha256(
            runner,
            root,
            commit_parents[0],
            expected["sha"],
        )
        != expected["patch_sha256"]
    ):
        raise ConflictError("frozen old commit identity changed", "stale_target")


def verify_frozen_ranges(
    runner: Runner,
    snapshot: LocalSnapshot,
    request: Mapping[str, object],
) -> None:
    if request["strategy"] in {"merge", "rebase"}:
        expected = request["head_commits"]
        commits = ordered_commits(
            runner,
            snapshot.root,
            request["merge_base"],
            request["pull_request"]["head_sha"],
        )
        if commits != [item["sha"] for item in expected]:
            raise ConflictError(
                "frozen old unique commit range changed",
                "stale_target",
            )
        for item in expected:
            verify_commit_identity(
                runner,
                snapshot.root,
                item,
                require_linear=request["strategy"] == "rebase",
            )
        return
    for member in request["native_stack"]["members"]:
        merge_bases = [
            value.strip().lower()
            for value in git(
                runner,
                snapshot.root,
                "merge-base",
                "--all",
                member["direct_base_sha"],
                member["head_sha"],
            ).splitlines()
            if value.strip()
        ]
        if merge_bases != [member["direct_merge_base"]]:
            raise ConflictError(
                "frozen old unique commit range changed",
                "stale_target",
            )
        prove_native_stack_member_input(
            runner,
            snapshot.root,
            member,
        )
        for item in member["old_commits"]:
            verify_commit_identity(
                runner,
                snapshot.root,
                item,
                require_linear=True,
            )


def validate_mapping(
    mapping: object,
    old: Mapping[str, object],
    new_sha: str,
    runner: Runner,
    root: Path,
    parent: str,
    allowed_paths: set[str],
) -> Mapping[str, object]:
    value = require_exact_keys(
        mapping,
        {
            "old_sha",
            "new_sha",
            "subject",
            "trailers",
            "patch_sha256",
            "conflict_paths",
            "companion_paths",
            "unaffected_path_digests",
            "rationale",
        },
        "commit mapping",
    )
    if value["old_sha"] != old["sha"] or value["new_sha"] != new_sha:
        raise ConflictError("commit mapping identity mismatch", "unexpected_history")
    subject = commit_subject(runner, root, new_sha)
    trailers = commit_trailers(runner, root, new_sha)
    digest = patch_sha256(runner, root, parent, new_sha)
    if (
        value["subject"] != old["subject"]
        or value["subject"] != subject
        or value["trailers"] != old["trailers"]
        or value["trailers"] != trailers
        or value["patch_sha256"] != digest
    ):
        raise ConflictError(
            "rewritten commit subject, trailers, or digest mismatch",
            "unexpected_history",
        )
    conflict_paths = value["conflict_paths"]
    companion_paths = value["companion_paths"]
    unaffected = value["unaffected_path_digests"]
    if (
        not isinstance(conflict_paths, list)
        or not isinstance(companion_paths, list)
        or not isinstance(unaffected, dict)
        or any(not isinstance(path, str) for path in [*conflict_paths, *companion_paths])
        or set(conflict_paths) & set(companion_paths)
        or not (set(conflict_paths) | set(companion_paths)) <= allowed_paths
    ):
        raise ConflictError("commit mapping paths are invalid", "unexpected_history")
    old_paths = set(old["paths"])
    new_paths = set(changed_paths(runner, root, new_sha))
    conflict_path_set = set(conflict_paths)
    companion_path_set = set(companion_paths)
    if (
        not conflict_path_set <= old_paths
        or new_paths - old_paths != companion_path_set
        or not old_paths - new_paths <= conflict_path_set
    ):
        raise ConflictError(
            "rewritten commit changed undeclared paths",
            "unexpected_history",
        )
    if digest == old["patch_sha256"]:
        if conflict_paths or companion_paths or value["rationale"] not in {"", None}:
            raise ConflictError(
                "untouched commit has conflict metadata",
                "unexpected_history",
            )
    else:
        if (
            not conflict_paths
            or not isinstance(value["rationale"], str)
            or not value["rationale"].strip()
        ):
            raise ConflictError(
                "conflict-touched commit lacks explicit rationale",
                "unexpected_history",
            )
        unaffected_paths = old_paths - conflict_path_set
        if set(unaffected) != unaffected_paths:
            raise ConflictError(
                "unaffected path digest set is incomplete",
                "unexpected_history",
            )
        if unaffected_paths:
            old_parent = parents(runner, root, old["sha"])
            if len(old_parent) != 1:
                raise ConflictError("old commit is not linear", "unexpected_history")
            for path in unaffected_paths:
                old_digest = path_patch_sha256(
                    runner, root, old_parent[0], old["sha"], path
                )
                new_digest = path_patch_sha256(runner, root, parent, new_sha, path)
                if unaffected[path] != old_digest or new_digest != old_digest:
                    raise ConflictError(
                        "unaffected path digest changed",
                        "unexpected_history",
                    )
    return value


def mapping_from_annotation(
    annotation: object,
    old: Mapping[str, object],
    new_sha: str,
    runner: Runner,
    root: Path,
    parent: str,
    allowed_paths: set[str],
) -> Mapping[str, object]:
    value = require_exact_keys(
        annotation,
        {"conflict_paths", "companion_paths", "rationale"},
        "semantic commit annotation",
    )
    conflict_paths = value["conflict_paths"]
    companion_paths = value["companion_paths"]
    rationale = value["rationale"]
    if (
        not isinstance(conflict_paths, list)
        or not isinstance(companion_paths, list)
        or any(not isinstance(path, str) for path in [*conflict_paths, *companion_paths])
        or not isinstance(rationale, str)
    ):
        raise ConflictError(
            "semantic commit annotation is malformed",
            "validation_failed",
        )
    old_parent = parents(runner, root, old["sha"])
    if len(old_parent) != 1:
        raise ConflictError("old commit is not linear", "unexpected_history")
    unaffected_paths = set(old["paths"]) - set(conflict_paths)
    mapping = {
        "old_sha": old["sha"],
        "new_sha": new_sha,
        "subject": commit_subject(runner, root, new_sha),
        "trailers": commit_trailers(runner, root, new_sha),
        "patch_sha256": patch_sha256(runner, root, parent, new_sha),
        "conflict_paths": conflict_paths,
        "companion_paths": companion_paths,
        "unaffected_path_digests": {
            path: path_patch_sha256(
                runner,
                root,
                old_parent[0],
                old["sha"],
                path,
            )
            for path in sorted(unaffected_paths)
        },
        "rationale": rationale,
    }
    return validate_mapping(
        mapping,
        old,
        new_sha,
        runner,
        root,
        parent,
        allowed_paths,
    )


def prove_rebase_range(
    runner: Runner,
    root: Path,
    base_sha: str,
    tip: str,
    old_commits: Sequence[Mapping[str, object]],
    mappings: Sequence[object],
    allowed_paths: set[str],
) -> list[str]:
    commits = ordered_commits(runner, root, base_sha, tip)
    if len(commits) != len(old_commits) or len(mappings) != len(old_commits):
        raise ConflictError(
            "rewritten range dropped, squashed, reordered, or added commits",
            "unexpected_history",
        )
    parent = base_sha
    for old, new_sha, mapping in zip(old_commits, commits, mappings):
        if parents(runner, root, new_sha) != [parent]:
            raise ConflictError("rewritten range is not linear", "unexpected_history")
        validate_mapping(
            mapping, old, new_sha, runner, root, parent, allowed_paths
        )
        parent = new_sha
    return commits


def prove_rebase_range_from_annotations(
    runner: Runner,
    root: Path,
    base_sha: str,
    tip: str,
    old_commits: Sequence[Mapping[str, object]],
    annotations: Sequence[object],
    allowed_paths: set[str],
) -> tuple[list[str], list[Mapping[str, object]]]:
    commits = ordered_commits(runner, root, base_sha, tip)
    if len(commits) != len(old_commits) or len(annotations) != len(old_commits):
        raise ConflictError(
            "rewritten range dropped, squashed, reordered, added, or omitted "
            "semantic annotations",
            "unexpected_history",
        )
    mappings: list[Mapping[str, object]] = []
    parent = base_sha
    for old, new_sha, annotation in zip(old_commits, commits, annotations):
        if parents(runner, root, new_sha) != [parent]:
            raise ConflictError("rewritten range is not linear", "unexpected_history")
        mappings.append(
            mapping_from_annotation(
                annotation,
                old,
                new_sha,
                runner,
                root,
                parent,
                allowed_paths,
            )
        )
        parent = new_sha
    return commits, mappings


def code_ref_base(
    request: Mapping[str, object], role: str
) -> tuple[str, str, str, str, Sequence[Mapping[str, object]]]:
    if request["strategy"] != "native-stack":
        pr = request["pull_request"]
        if request["strategy"] == "merge":
            return (
                pr["head_sha"],
                pr["head_sha"],
                pr["head_ref"],
                pr["head_sha"],
                request["head_commits"],
            )
        return (
            pr["head_sha"],
            pr["base_sha"],
            pr["base_ref"],
            pr["head_sha"],
            request["head_commits"],
        )
    number = int(role.split(":", 1)[1])
    member = next(
        member
        for member in request["native_stack"]["members"]
        if member["pr_number"] == number
    )
    return (
        member["head_sha"],
        member["direct_base_sha"],
        member["direct_base_ref"],
        member["lease_sha"],
        member["old_commits"],
    )


def build_code_ref(
    request: Mapping[str, object],
    remote: RemoteRef,
    new_sha: str,
    commits: list[str],
    mappings: Sequence[object],
    *,
    generated_base_sha: str | None = None,
) -> dict[str, object]:
    old_sha, base_sha, base_ref, lease_sha, _ = code_ref_base(request, remote.role)
    return {
        "role": remote.role,
        "pr_number": remote.pr_number,
        "repository": remote.repository,
        "ref": remote.ref,
        "old_sha": old_sha,
        "new_sha": new_sha,
        "base_ref": base_ref,
        "base_sha": generated_base_sha or base_sha,
        "lease_sha": lease_sha,
        "commits": list(mappings) if mappings else commits,
    }


def validate_validations(value: object) -> list[Mapping[str, str]]:
    if not isinstance(value, list) or not value:
        raise ConflictError("validation is incomplete", "validation_failed")
    outcomes: list[Mapping[str, str]] = []
    for item in value:
        outcome = require_exact_keys(
            item, {"command", "status", "detail"}, "validation outcome"
        )
        if (
            not isinstance(outcome["command"], str)
            or not outcome["command"].strip()
            or outcome["status"] != "passed"
            or not isinstance(outcome["detail"], str)
            or not outcome["detail"].strip()
            or contains_credentials(canonical_json(outcome).decode("utf-8"))
        ):
            raise ConflictError("validation did not pass", "validation_failed")
        outcomes.append(outcome)
    return outcomes


def validate_semantic_validations(value: object) -> list[Mapping[str, str]]:
    if not isinstance(value, list) or not value:
        raise ConflictError("validation is incomplete", "validation_failed")
    outcomes: list[Mapping[str, str]] = []
    for item in value:
        outcome = require_exact_keys(
            item, {"command", "result"}, "semantic validation outcome"
        )
        if (
            not isinstance(outcome["command"], str)
            or not outcome["command"].strip()
            or outcome["result"] != "passed"
            or contains_credentials(canonical_json(outcome).decode("utf-8"))
        ):
            raise ConflictError("validation did not pass", "validation_failed")
        outcomes.append(outcome)
    return outcomes


def git_show_file(
    runner: Runner, root: Path, commit: str, path: str
) -> str:
    return git(runner, root, "show", f"{commit}:{path}")


def validate_artifact(
    runner: Runner,
    snapshot: LocalSnapshot,
    request: Mapping[str, object],
    artifact_remote: RemoteRef,
    artifact_ref: str,
    artifact_head: str,
    code_refs: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], list[Mapping[str, str]]]:
    report_path, receipt_path = artifact_paths(request["request_id"])
    if artifact_head in {ref["new_sha"] for ref in code_refs}:
        raise ConflictError("artifact head equals a code head", "unexpected_history")
    final_code_head = code_refs[-1]["new_sha"]
    if parents(runner, snapshot.root, artifact_head) != [final_code_head]:
        raise ConflictError(
            "artifact commit is not the sole single-parent child of code head",
            "unexpected_history",
        )
    if changed_paths(runner, snapshot.root, artifact_head) != sorted(
        [report_path, receipt_path]
    ):
        raise ConflictError(
            "artifact commit changed unexpected paths",
            "unexpected_history",
        )
    report = git_show_file(runner, snapshot.root, artifact_head, report_path)
    receipt_text = git_show_file(runner, snapshot.root, artifact_head, receipt_path)
    if not report.strip():
        raise ConflictError("conflict report is empty", "validation_failed")
    if (
        request["request_id"] not in report
        or request["request_sha256"] not in report
    ):
        raise ConflictError(
            "conflict report request identity mismatch",
            "validation_failed",
        )
    try:
        receipt = json.loads(receipt_text)
    except json.JSONDecodeError:
        raise ConflictError("conflict receipt is malformed", "validation_failed") from None
    value = require_exact_keys(
        receipt,
        {
            "schema",
            "request",
            "policy",
            "model",
            "mode",
            "strategy",
            "repository",
            "pull_request",
            "generated_refs",
            "validation_complete",
            "validation",
        },
        "conflict receipt",
    )
    expected_refs = [
        {"ref": ref, "sha256": object_digest(ref)}
        for ref in code_refs
    ]
    if (
        value["schema"] != LEGACY_RECEIPT_SCHEMA
        or value["request"]
        != {"id": request["request_id"], "sha256": request["request_sha256"]}
        or value["policy"] != LEGACY_POLICY
        or value["model"] != request["model"]
        or value["mode"] != MODE
        or value["strategy"] != request["strategy"]
        or value["repository"] != request["repository"]
        or value["pull_request"] != request["pull_request"]
        or value["generated_refs"] != expected_refs
        or value["validation_complete"] is not True
    ):
        raise ConflictError("conflict receipt identity mismatch", "validation_failed")
    validations = validate_validations(value["validation"])
    for code_ref in code_refs:
        for mapping in code_ref["commits"]:
            if isinstance(mapping, dict) and mapping.get("old_sha"):
                if (
                    mapping["old_sha"] not in report
                    or mapping["new_sha"] not in report
                ):
                    raise ConflictError(
                        "report does not account for every old/new commit",
                        "validation_failed",
                    )
    artifact = {
        "branch": artifact_remote.ref,
        "head_sha": artifact_head,
        "report": {
            "path": report_path,
            "commit": artifact_head,
            "sha256": hashlib.sha256(report.encode("utf-8")).hexdigest(),
        },
        "receipt": {
            "path": receipt_path,
            "commit": artifact_head,
        },
    }
    return artifact, validations


def validate_semantic_artifact(
    runner: Runner,
    snapshot: LocalSnapshot,
    request: Mapping[str, object],
    artifact_remote: RemoteRef,
    artifact_head: str,
    final_code_head: str,
) -> tuple[str, Sequence[Sequence[object]], list[Mapping[str, str]], str]:
    path = semantic_path(str(request["request_id"]))
    if parents(runner, snapshot.root, artifact_head) != [final_code_head]:
        raise ConflictError(
            "semantic artifact is not the sole child of the final code head",
            "unexpected_history",
        )
    if changed_paths(runner, snapshot.root, artifact_head) != [path]:
        raise ConflictError(
            "semantic artifact commit changed unexpected paths",
            "unexpected_history",
        )
    content = git_show_file(runner, snapshot.root, artifact_head, path)
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        raise ConflictError(
            "conflict semantic output is malformed",
            "validation_failed",
        ) from None
    expected_keys = (
        {"summary", "validation"}
        if request["strategy"] == "merge"
        else {"summary", "commit_annotations", "validation"}
    )
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or not isinstance(value.get("summary"), str)
        or not value["summary"].strip()
        or contains_credentials(value["summary"])
        or (
            request["strategy"] != "merge"
            and not isinstance(value.get("commit_annotations"), list)
        )
    ):
        raise ConflictError(
            "conflict semantic output has an unsupported shape",
            "validation_failed",
        )
    annotations = (
        [[]] if request["strategy"] == "merge" else value["commit_annotations"]
    )
    expected_role_count = len(expected_roles(request))
    if (
        len(annotations) != expected_role_count
        or any(not isinstance(items, list) for items in annotations)
    ):
        raise ConflictError(
            "conflict semantic annotations do not match assigned roles",
            "validation_failed",
        )
    validations = validate_semantic_validations(value["validation"])
    return (
        value["summary"],
        annotations,
        validations,
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def canonical_conflict_artifacts(
    request: Mapping[str, object],
    code_refs: Sequence[Mapping[str, object]],
    validations: Sequence[Mapping[str, str]],
    summary: str,
) -> tuple[str, Mapping[str, object]]:
    lines = [
        "# Conflict resolution",
        "",
        f"Request: `{request['request_id']}`",
        f"Request SHA-256: `{request['request_sha256']}`",
        "",
        summary.strip(),
        "",
        "## Generated history",
        "",
    ]
    for code_ref in code_refs:
        lines.append(
            f"- `{code_ref['role']}`: `{code_ref['old_sha']}` -> "
            f"`{code_ref['new_sha']}`"
        )
        for mapping in code_ref["commits"]:
            if isinstance(mapping, dict):
                lines.append(
                    f"  - `{mapping['old_sha']}` -> `{mapping['new_sha']}`"
                )
    report = "\n".join(lines).rstrip() + "\n"
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "request": {
            "id": request["request_id"],
            "sha256": request["request_sha256"],
        },
        "policy": request["policy"],
        "model": request["model"],
        "mode": MODE,
        "strategy": request["strategy"],
        "repository": request["repository"],
        "pull_request": request["pull_request"],
        "generated_refs": [
            {"ref": ref, "sha256": object_digest(ref)}
            for ref in code_refs
        ],
        "validation_complete": True,
        "validation": list(validations),
    }
    return report, receipt


def prove_generated_semantic(
    runner: Runner,
    snapshot: LocalSnapshot,
    request: Mapping[str, object],
    task: Mapping[str, object],
    recovery_result: Result | None = None,
) -> tuple[list[Mapping[str, object]], Mapping[str, object], list[Mapping[str, str]]]:
    artifact_remote = discover_semantic_artifact_ref(task, request)
    request_id = str(request["request_id"])
    quarantine: list[str] = []
    artifact_ref, artifact_head = fetch_quarantined(
        runner,
        snapshot,
        artifact_remote,
        request_id,
    )
    quarantine.append(artifact_ref)
    fetched_code: list[tuple[RemoteRef, str, str]] = []
    if request["strategy"] == "native-stack":
        for remote in assigned_code_refs(request):
            target, tip = fetch_quarantined(
                runner,
                snapshot,
                remote,
                request_id,
            )
            quarantine.append(target)
            fetched_code.append((remote, target, tip))
        final_code_head = fetched_code[-1][2]
    else:
        artifact_parents = parents(
            runner,
            snapshot.root,
            artifact_head,
        )
        if len(artifact_parents) != 1:
            raise ConflictError(
                "semantic artifact commit must have exactly one parent",
                "unexpected_history",
            )
        final_code_head = artifact_parents[0]
    summary, annotations, validations, semantic_sha256 = validate_semantic_artifact(
        runner,
        snapshot,
        request,
        artifact_remote,
        artifact_head,
        final_code_head,
    )
    if request["strategy"] != "native-stack":
        target = quarantine_ref(request_id, "code")
        git(
            runner,
            snapshot.root,
            "update-ref",
            target,
            final_code_head,
        )
        quarantine.append(target)
        fetched_code.append(
            (
                RemoteRef(
                    "code",
                    request["pull_request"]["number"],
                    request["repository"],
                    artifact_remote.ref,
                ),
                target,
                final_code_head,
            )
        )
    require_local_unchanged(runner, snapshot, quarantine)
    allowed_paths = set(request["allowed_paths"])
    code_refs: list[Mapping[str, object]] = []
    if request["strategy"] == "merge":
        if annotations != [[]]:
            raise ConflictError(
                "merge semantic annotations must be one empty array",
                "validation_failed",
            )
        remote, _, tip = fetched_code[0]
        commits = first_parent_chain(
            runner,
            snapshot.root,
            request["pull_request"]["head_sha"],
            tip,
        )
        if not commits or parents(runner, snapshot.root, commits[0]) != [
            request["pull_request"]["head_sha"],
            request["pull_request"]["base_sha"],
        ]:
            raise ConflictError(
                "merge integration parents are not [head, base]",
                "unexpected_history",
            )
        parent = commits[0]
        for commit in commits[1:]:
            if parents(runner, snapshot.root, commit) != [parent]:
                raise ConflictError("merge fixes are not linear", "unexpected_history")
            parent = commit
        code_ref = build_code_ref(request, remote, tip, commits, [])
        code_ref["base_ref"] = request["pull_request"]["base_ref"]
        code_ref["base_sha"] = request["pull_request"]["base_sha"]
        code_refs.append(code_ref)
    elif request["strategy"] == "rebase":
        remote, _, tip = fetched_code[0]
        commits, mappings = prove_rebase_range_from_annotations(
            runner,
            snapshot.root,
            request["pull_request"]["base_sha"],
            tip,
            request["head_commits"],
            annotations[0],
            allowed_paths,
        )
        code_refs.append(build_code_ref(request, remote, tip, commits, mappings))
    else:
        previous_tip = request["native_stack"]["trunk"]["sha"]
        for index, (remote, _, tip) in enumerate(fetched_code):
            member = request["native_stack"]["members"][index]
            prove_native_stack_member_input(runner, snapshot.root, member)
            commits, mappings = prove_rebase_range_from_annotations(
                runner,
                snapshot.root,
                previous_tip,
                tip,
                member["old_commits"],
                annotations[index],
                allowed_paths,
            )
            code_ref = build_code_ref(
                request,
                remote,
                tip,
                commits,
                mappings,
                generated_base_sha=previous_tip,
            )
            if (
                code_ref["old_sha"] != member["head_sha"]
                or code_ref["lease_sha"] != member["lease_sha"]
            ):
                raise ConflictError(
                    "native stack lease identity mismatch",
                    "stale_target",
                )
            code_refs.append(code_ref)
            previous_tip = tip
    report, receipt = canonical_conflict_artifacts(
        request,
        code_refs,
        validations,
        summary,
    )
    artifact = {
        "branch": artifact_remote.ref,
        "head_sha": artifact_head,
        "semantic": {
            "schema": SEMANTIC_SCHEMA,
            "kind": "conflict-resolution",
            "path": semantic_path(request_id),
            "commit": artifact_head,
            "sha256": semantic_sha256,
        },
        "report": {
            "sha256": hashlib.sha256(report.encode("utf-8")).hexdigest(),
            "content": report,
        },
        "receipt": {
            "sha256": object_digest(receipt),
            "value": receipt,
        },
    }
    if recovery_result is not None:
        recovery_result.code_refs = list(code_refs)
        recovery_result.artifact = artifact
    return code_refs, artifact, validations


def prove_generated(
    runner: Runner,
    snapshot: LocalSnapshot,
    request: Mapping[str, object],
    task: Mapping[str, object],
    recovery_result: Result | None = None,
) -> tuple[list[Mapping[str, object]], Mapping[str, object], list[Mapping[str, str]]]:
    if request["policy"] == POLICY:
        return prove_generated_semantic(
            runner,
            snapshot,
            request,
            task,
            recovery_result,
        )
    remote_artifact = discover_artifact_ref(task, request)
    request_id = request["request_id"]
    quarantine: list[str] = []
    artifact_ref, artifact_sha = fetch_quarantined(
        runner, snapshot, remote_artifact, request_id
    )
    quarantine.append(artifact_ref)
    if recovery_result is not None:
        report_path, receipt_path = artifact_paths(request_id)
        recovery_result.artifact = {
            "branch": remote_artifact.ref,
            "head_sha": artifact_sha,
            "report": {
                "path": report_path,
                "commit": artifact_sha,
                "sha256": None,
            },
            "receipt": {
                "path": receipt_path,
                "commit": artifact_sha,
            },
        }
    receipt_path = artifact_paths(request_id)[1]
    receipt_text = git_show_file(runner, snapshot.root, artifact_sha, receipt_path)
    try:
        receipt_preview = json.loads(receipt_text)
    except json.JSONDecodeError:
        raise ConflictError("conflict receipt is malformed", "validation_failed") from None
    if not isinstance(receipt_preview, dict):
        raise ConflictError("conflict receipt is malformed", "validation_failed")
    remote_code, receipt_refs = code_refs_from_receipt(
        receipt_preview,
        request,
    )
    if (
        len({remote.ref for remote in remote_code}) != len(remote_code)
        or len({ref["new_sha"] for ref in receipt_refs.values()})
        != len(receipt_refs)
    ):
        raise ConflictError(
            "generated code refs and heads are not distinct",
            "unexpected_history",
        )
    if recovery_result is not None:
        recovery_result.code_refs = [
            receipt_refs[role]
            for role, _, _ in expected_roles(request)
        ]
    if remote_artifact.ref in {ref.ref for ref in remote_code}:
        raise ConflictError("artifact and code refs are not distinct", "unexpected_history")
    fetched_code: list[tuple[RemoteRef, str]] = []
    for remote in remote_code:
        target, sha = fetch_quarantined(
            runner,
            snapshot,
            remote,
            request_id,
            allow_commit_sha=True,
        )
        quarantine.append(target)
        fetched_code.append((remote, sha))
    require_local_unchanged(runner, snapshot, quarantine)
    mappings_by_role: dict[str, Sequence[object]] = {}
    for role, ref in receipt_refs.items():
        mappings_by_role[role] = ref["commits"]
    allowed_paths = set(request["allowed_paths"])
    code_refs: list[Mapping[str, object]] = []
    if request["strategy"] == "merge":
        remote, tip = fetched_code[0]
        commits = first_parent_chain(
            runner, snapshot.root, request["pull_request"]["head_sha"], tip
        )
        if not commits or parents(runner, snapshot.root, commits[0]) != [
            request["pull_request"]["head_sha"],
            request["pull_request"]["base_sha"],
        ]:
            raise ConflictError(
                "merge integration parents are not [head, base]",
                "unexpected_history",
            )
        parent = commits[0]
        for commit in commits[1:]:
            if parents(runner, snapshot.root, commit) != [parent]:
                raise ConflictError(
                    "merge fixes are not a linear chain",
                    "unexpected_history",
                )
            parent = commit
        mappings = mappings_by_role.get(remote.role, [])
        if list(mappings) != commits:
            raise ConflictError(
                "merge receipt commit list does not match history",
                "unexpected_history",
            )
        code_ref = build_code_ref(request, remote, tip, commits, [])
        if code_ref != receipt_refs[remote.role]:
            raise ConflictError(
                "merge receipt code ref does not match history",
                "unexpected_history",
            )
        code_refs.append(code_ref)
    elif request["strategy"] == "rebase":
        remote, tip = fetched_code[0]
        mappings = mappings_by_role.get(remote.role, [])
        commits = prove_rebase_range(
            runner,
            snapshot.root,
            request["pull_request"]["base_sha"],
            tip,
            request["head_commits"],
            mappings,
            allowed_paths,
        )
        code_ref = build_code_ref(request, remote, tip, commits, mappings)
        if code_ref != receipt_refs[remote.role]:
            raise ConflictError(
                "rebase receipt code ref does not match history",
                "unexpected_history",
            )
        code_refs.append(code_ref)
    else:
        previous_tip = request["native_stack"]["trunk"]["sha"]
        member_by_number = {
            member["pr_number"]: member for member in request["native_stack"]["members"]
        }
        represented = {remote.pr_number for remote, _ in fetched_code}
        outside = {
            item["pr_number"] for item in request["native_stack"]["outside_dependents"]
        }
        if represented & outside:
            raise ConflictError("outside dependent was represented", "unexpected_history")
        for remote, tip in fetched_code:
            member = member_by_number[remote.pr_number]
            prove_native_stack_member_input(
                runner,
                snapshot.root,
                member,
            )
            expected_parent = member["expected_new_parent"]
            expected_role = (
                "trunk"
                if not code_refs
                else f"member:{code_refs[-1]['pr_number']}"
            )
            expected_old_sha = (
                request["native_stack"]["trunk"]["sha"]
                if not code_refs
                else member_by_number[code_refs[-1]["pr_number"]]["head_sha"]
            )
            if (
                expected_parent["role"] != expected_role
                or expected_parent["old_sha"] != expected_old_sha
            ):
                raise ConflictError(
                    "native stack expected parent chain is inconsistent",
                    "stale_target",
                )
            mappings = mappings_by_role.get(remote.role, [])
            commits = prove_rebase_range(
                runner,
                snapshot.root,
                previous_tip,
                tip,
                member["old_commits"],
                mappings,
                allowed_paths,
            )
            code_ref = build_code_ref(
                request,
                remote,
                tip,
                commits,
                mappings,
                generated_base_sha=previous_tip,
            )
            if (
                code_ref["old_sha"] != member["head_sha"]
                or code_ref["lease_sha"] != member["lease_sha"]
            ):
                raise ConflictError("native stack lease identity mismatch", "stale_target")
            if code_ref != receipt_refs[remote.role]:
                raise ConflictError(
                    "native stack receipt ref does not match history",
                    "unexpected_history",
                )
            code_refs.append(code_ref)
            previous_tip = tip
    artifact, validations = validate_artifact(
        runner,
        snapshot,
        request,
        remote_artifact,
        artifact_ref,
        artifact_sha,
        code_refs,
    )
    return code_refs, artifact, validations


def validate_prior_generated(
    prior: Mapping[str, object],
    code_refs: Sequence[Mapping[str, object]],
    artifact: Mapping[str, object],
    validations: Sequence[Mapping[str, str]],
) -> None:
    generated = prior["generated"]
    validation = prior["validation"]
    if prior["status"] == "success":
        if (
            generated["code_refs"] != list(code_refs)
            or generated["artifact"] != artifact
            or prior["application"] != {"status": "quarantined_refs"}
            or validation
            != {"complete": True, "outcomes": list(validations)}
        ):
            raise ConflictError(
                "prior success does not match rediscovered generated result",
                "malformed_result",
            )
        return
    if generated["code_refs"] and generated["code_refs"] != list(code_refs):
        raise ConflictError("prior generated code refs changed", "malformed_result")
    if generated["artifact"] is not None and not prior_artifact_matches(
        generated["artifact"], artifact
    ):
        raise ConflictError("prior artifact identity changed", "malformed_result")
    if validation["complete"] and validation["outcomes"] != list(validations):
        raise ConflictError("prior validation identity changed", "malformed_result")


def prior_artifact_matches(
    prior: Mapping[str, object], current: Mapping[str, object]
) -> bool:
    if (
        prior.get("branch") != current.get("branch")
        or prior.get("head_sha") != current.get("head_sha")
        or prior.get("receipt") != current.get("receipt")
    ):
        return False
    prior_report = prior.get("report")
    current_report = current.get("report")
    if not isinstance(prior_report, dict) or not isinstance(current_report, dict):
        return False
    return (
        prior_report.get("path") == current_report.get("path")
        and prior_report.get("commit") == current_report.get("commit")
        and (
            prior_report.get("sha256") is None
            or prior_report.get("sha256") == current_report.get("sha256")
        )
    )


def pull_request_result(request: Mapping[str, object]) -> Mapping[str, object]:
    return request["pull_request"]


def task_link(task: Mapping[str, object]) -> str | None:
    value = task.get("html_url") or task.get("url")
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"https://(?:api\.)?github\.com/\S+", value)
        or contains_credentials(value)
    ):
        raise ConflictError("Agent Task URL is malformed", "task_failed")
    return value


def execute(
    options: Options,
    *,
    cwd: Path,
    runner: Runner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    progress: Progress | None = None,
    result: Result | None = None,
) -> int:
    progress = progress or Progress()
    request = options.request
    replacement = getattr(options, "replacement", None)
    control_root = options.result_file.parent.resolve()
    if not control_root.is_dir():
        raise ConflictError(
            "result artifact directory is unavailable",
            "stale_target",
        )
    snapshot = local_snapshot(
        runner,
        cwd,
        control_root=control_root,
        expected_repository=request["repository"],
        expected_head=request["pull_request"]["head_sha"],
        expected_branch=request["pull_request"]["head_ref"],
        allow_detached=True,
    )
    if snapshot.repository != request["repository"]:
        raise ConflictError("request repository does not match cwd", "stale_target")
    for path, description in (
        (options.request_file, "--request-file"),
        (options.prompt_file, "--prompt-file"),
        (options.result_file, "--result-file"),
        (options.input_result_file, "--input-result-file"),
        (
            (
                replacement.request_file
                if replacement is not None
                else None
            ),
            "--replace-malformed-request-file",
        ),
        (
            (
                replacement.prompt_file
                if replacement is not None
                else None
            ),
            "--replace-malformed-prompt-file",
        ),
        (
            (
                replacement.original_result_file
                if replacement is not None
                else None
            ),
            "--replace-malformed-original-result-file",
        ),
        (
            (
                replacement.resumed_result_file
                if replacement is not None
                else None
            ),
            "--replace-malformed-resumed-result-file",
        ),
    ):
        if path is None:
            continue
        try:
            path.resolve().relative_to(snapshot.root)
        except ValueError:
            pass
        else:
            raise ConflictError(
                f"{description} must be outside the repository",
                "policy_rejected",
            )
    require_target_fresh(runner, snapshot, request)
    require_local_unchanged(runner, snapshot)
    if result is not None:
        request_policy = request.get("policy", POLICY)
        result.schema = (
            LEGACY_RESULT_SCHEMA
            if request_policy == LEGACY_POLICY
            else RESULT_SCHEMA
        )
        result.policy = request_policy
        result.model = options.model
        result.repository = snapshot.repository
        result.strategy = options.strategy
        result.request_id = request["request_id"]
        result.request_sha256 = request["request_sha256"]
        result.pull_request = pull_request_result(request)
    if already_satisfied(runner, snapshot, request):
        require_target_fresh(runner, snapshot, request)
        require_local_unchanged(runner, snapshot)
        if result is not None:
            result.status = "success"
            result.application_status = "no_changes"
            result.validations = (
                [
                    {
                        "command": "local-history-proof",
                        "status": "passed",
                        "detail": (
                            "all requested heads already have their exact expected "
                            "parent"
                        ),
                    }
                ]
                if request.get("policy", POLICY) == LEGACY_POLICY
                else [{"command": "local-history-proof", "result": "passed"}]
            )
        return 0
    fetch_pinned_inputs(runner, snapshot, request)
    verify_frozen_ranges(runner, snapshot, request)
    require_target_fresh(runner, snapshot, request)
    require_local_unchanged(runner, snapshot)
    if replacement is not None:
        validate_malformed_completed_replacement(
            runner,
            snapshot,
            replacement,
        )
        require_target_fresh(runner, snapshot, request)
        require_local_unchanged(runner, snapshot)
    if options.prior_result is not None:
        task_id = options.prior_result["task"]["id"]
        initial = get_task(runner, snapshot, task_id)
    else:
        initial = start_task(runner, snapshot, options)
    progress.task_id = str(initial["id"])
    progress.task_state = str(initial["state"])
    if result is not None:
        result.task_id = progress.task_id
        result.task_state = progress.task_state
        result.task_url = task_link(initial)
        result.task_base_ref = request["pull_request"]["head_sha"]
        result.task_base_sha = request["pull_request"]["head_sha"]
    final = monitor_task(runner, snapshot, initial, progress, sleep)
    if result is not None:
        result.task_state = str(final["state"])
        result.task_url = task_link(final) or result.task_url
    require_target_fresh(runner, snapshot, request)
    require_local_unchanged(runner, snapshot)
    code_refs, artifact, validations = prove_generated(
        runner,
        snapshot,
        request,
        final,
        result,
    )
    if options.prior_result is not None:
        validate_prior_generated(
            options.prior_result, code_refs, artifact, validations
        )
    require_target_fresh(runner, snapshot, request)
    require_local_unchanged(runner, snapshot)
    if result is not None:
        result.code_refs = list(code_refs)
        result.artifact = artifact
        result.validations = list(validations)
        result.application_status = "quarantined_refs"
        result.status = "success"
    return 0


def atomic_write_json(path: Path, data: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical_json(data).decode("utf-8") + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConflictError(
            f"could not write result file: {error}",
            "malformed_result",
        ) from None


def result_path_from_args(args: Sequence[str]) -> Path | None:
    for index, token in enumerate(args):
        if token == "--result-file" and index + 1 < len(args):
            path = Path(args[index + 1])
            return path if path.is_absolute() else None
    return None


def safe_error_message(value: str) -> str:
    return (
        "operation failed; sensitive detail omitted"
        if contains_credentials(value)
        else value
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    cwd: Path | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    result_path = result_path_from_args(args)
    result = Result()
    progress = Progress()
    try:
        options = parse_args(args)
        result_path = options.result_file
        exit_code = execute(
            options,
            cwd=Path.cwd() if cwd is None else cwd,
            runner=runner,
            sleep=sleep,
            progress=progress,
            result=result,
        )
    except KeyboardInterrupt:
        result.status = "interrupted"
        result.task_id = progress.task_id
        result.task_state = progress.task_state
        result.error = {
            "code": "interrupted",
            "message": (
                f"monitoring interrupted; task {progress.task_id} remains remote"
                if progress.task_id
                else "interrupted before task creation"
            ),
        }
        exit_code = 130
    except ConflictError as error:
        result.status = "error"
        result.task_id = result.task_id or progress.task_id
        result.task_state = result.task_state or progress.task_state
        result.error = {
            "code": error.code,
            "message": safe_error_message(str(error)),
        }
        print(f"error: {safe_error_message(str(error))}", file=stderr)
        exit_code = 2
    except Exception as error:
        result.status = "error"
        result.task_id = result.task_id or progress.task_id
        result.task_state = result.task_state or progress.task_state
        result.error = {
            "code": "unexpected_helper_error",
            "message": safe_error_message(str(error)),
        }
        print(f"error: {safe_error_message(str(error))}", file=stderr)
        exit_code = 2
    if result_path is not None:
        try:
            atomic_write_json(result_path, result.as_dict())
        except ConflictError as error:
            print(f"error: {safe_error_message(str(error))}", file=stderr)
            return 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
