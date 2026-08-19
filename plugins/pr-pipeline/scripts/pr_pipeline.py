#!/usr/bin/env python3
"""Deterministic mechanics for the PR Pipeline custom agent."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable


STATE_VERSION = 1
DEFAULT_MAX_ITERATIONS = 2
NO_PROGRESS_LIMIT = 2
MERGEABLE_RETRY_DELAYS = (2, 4, 8)
IS_WINDOWS = os.name == "nt"
PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
    r"/?(?:#\S*)?$"
)
SHORT_TARGET_PATTERN = re.compile(
    r"^(?P<owner>[^/\s]+)/(?P<repo>[^#/\s]+)#(?P<number>\d+)$"
)
BARE_NUMBER_PATTERN = re.compile(r"^#?(?P<number>\d+)$")

STAGE_CONFLICT = "conflict-fix-loop"
STAGE_SELF_REVIEW = "self-review-loop"
STAGE_COPILOT_REVIEW = "copilot-review-loop"
STAGE_CI = "ci-fix-loop"
STAGE_DESCRIPTION = "pr-description"

CLAUDE_FAMILY = "claude"
DEFAULT_STAGE_MODEL = "claude-sonnet-4.6"
DEFAULT_EFFORT = "high"

# Execution order. This is the pipeline's own order and is deliberately not the
# bottleneck chain any dashboard shows. The conflict stage leads because a
# conflicted pull request cannot produce meaningful checks and may not present a
# coherent diff. The CI stage trails both review stages because those stages push
# commits and checks are slow, so fixing checks earlier would fix a head that no
# longer exists.
STAGES: tuple[dict[str, Any], ...] = (
    {
        "stage": STAGE_CONFLICT,
        "plugin": STAGE_CONFLICT,
        "agent": f"{STAGE_CONFLICT}:{STAGE_CONFLICT}",
        "module": "conflict_fix_loop",
        "evidence": "github",
        "requires_family": None,
        "summary": "resolve merge conflicts with the base branch",
    },
    {
        "stage": STAGE_SELF_REVIEW,
        "plugin": STAGE_SELF_REVIEW,
        "agent": f"{STAGE_SELF_REVIEW}:{STAGE_SELF_REVIEW}",
        "module": "self_review_loop",
        "evidence": "helper",
        "requires_family": CLAUDE_FAMILY,
        "summary": "review the diff and commit the verified fixes",
    },
    {
        "stage": STAGE_COPILOT_REVIEW,
        "plugin": STAGE_COPILOT_REVIEW,
        "agent": f"{STAGE_COPILOT_REVIEW}:{STAGE_COPILOT_REVIEW}",
        "module": "copilot_review_loop",
        "evidence": "helper",
        "requires_family": None,
        "summary": "address the Copilot review comments",
    },
    {
        "stage": STAGE_CI,
        "plugin": STAGE_CI,
        "agent": f"{STAGE_CI}:{STAGE_CI}",
        "module": "ci_fix_loop",
        "evidence": "github",
        "requires_family": None,
        "summary": "fix the failing checks this pull request caused",
    },
    {
        "stage": STAGE_DESCRIPTION,
        "plugin": STAGE_DESCRIPTION,
        "agent": f"{STAGE_DESCRIPTION}:{STAGE_DESCRIPTION}",
        "module": "pr_description",
        "evidence": "helper",
        "requires_family": None,
        "summary": "validate or replace the title and description",
    },
)
STAGE_NAMES = tuple(entry["stage"] for entry in STAGES)
STAGE_BY_NAME = {entry["stage"]: entry for entry in STAGES}
STAGE_INDEX = {entry["stage"]: index for index, entry in enumerate(STAGES)}

STAGE_OUTCOMES = ("cleared", "skipped", "no_progress", "escalated")
CLEARING_OUTCOMES = ("cleared", "skipped")

ESCALATION_ACTIONS = {
    "max_iterations_reached": (
        "Read the kept stage transcripts, decide what still needs a human, and "
        "start the remaining stage yourself."
    ),
    "stage_escalated": (
        "Read the kept stage session, which holds the reason the stage stopped."
    ),
    "no_progress": (
        "Read the kept stage session. The stage ran twice without changing "
        "anything, so it needs a decision the pipeline cannot make."
    ),
    "pr_not_open": "Reopen the pull request or start the pipeline on an open one.",
    "helper_missing": (
        "Install the missing plugin from the trask-plugins marketplace, then start "
        "the pipeline again."
    ),
    "model_gate": (
        "Start the pipeline again where it can pin a model for every stage."
    ),
}

CHECK_SUCCESS_STATES = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
CHECK_FAILURE_STATES = frozenset(
    {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "ACTION_REQUIRED"}
)
CHECK_PENDING_STATES = frozenset(
    {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED", "STALE"}
)


class WorkflowError(RuntimeError):
    pass


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "no output"
        raise WorkflowError(
            f"{' '.join(command)} failed ({process.returncode}): {detail}"
        )
    return process


def git(repo_root: Path, *arguments: str) -> str:
    return run(["git", "-C", str(repo_root), *arguments]).stdout.strip()


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)


def gh_json(arguments: list[str]) -> Any:
    output = run(["gh", *arguments]).stdout
    try:
        return json.loads(output) if output.strip() else None
    except json.JSONDecodeError as error:
        raise WorkflowError(f"gh returned invalid JSON: {error}") from error


def graphql(query: str, variables: dict[str, str | int | None]) -> Any:
    arguments = ["api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        if value is None:
            arguments.extend(["-F", f"{name}=null"])
        else:
            flag = "-F" if isinstance(value, int) else "-f"
            arguments.extend([flag, f"{name}={value}"])
    payload = gh_json(arguments)
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if errors:
        raise WorkflowError(f"GraphQL failed: {json.dumps(errors, sort_keys=True)}")
    return payload


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def require_tools() -> None:
    missing = [name for name in ("git", "gh") if shutil.which(name) is None]
    if missing:
        raise WorkflowError(f"required tools not found: {', '.join(missing)}")


def normalize_cli_path(value: str, *, windows: bool) -> str:
    if windows:
        match = re.fullmatch(r"/([A-Za-z])(?:/(.*))?", value)
        if match:
            drive, remainder = match.groups()
            value = f"{drive.upper()}:/{remainder or ''}"
    return value


def cli_path(value: str) -> Path:
    return Path(normalize_cli_path(value, windows=IS_WINDOWS)).resolve()


def copilot_home() -> Path:
    value = os.environ.get("COPILOT_HOME", "").strip()
    if value:
        return Path(normalize_cli_path(value, windows=IS_WINDOWS))
    return Path.home() / ".copilot"


def parse_target(target: str, repo_name: str | None = None) -> dict[str, Any]:
    match = PR_URL_PATTERN.fullmatch(target) or SHORT_TARGET_PATTERN.fullmatch(target)
    if match:
        values = match.groupdict()
        return build_target(values["owner"], values["repo"], int(values["number"]))
    bare = BARE_NUMBER_PATTERN.fullmatch(target)
    if bare and repo_name:
        owner, _, repo = repo_name.partition("/")
        if owner and repo:
            return build_target(owner, repo, int(bare.group("number")))
    if bare:
        raise WorkflowError("a bare PR number requires repository context")
    raise WorkflowError(
        "target must be a GitHub PR URL, owner/repo#number, or bare PR number"
    )


def build_target(owner: str, repo: str, number: int) -> dict[str, Any]:
    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        "repo_name": f"{owner}/{repo}",
        "pr_url": f"https://github.com/{owner}/{repo}/pull/{number}",
    }


def default_state_path(target: dict[str, Any]) -> Path:
    name = f"{target['owner']}--{target['repo']}--{target['number']}.json"
    return Path.home() / ".copilot" / "run" / "pr-pipeline" / name


def stage_state_path(plugin: str, target: dict[str, Any]) -> Path:
    name = f"{target['owner']}--{target['repo']}--{target['number']}.json"
    return Path.home() / ".copilot" / "run" / plugin / name


def stage_script_path(entry: dict[str, Any]) -> Path:
    return (
        copilot_home()
        / "installed-plugins"
        / "trask-plugins"
        / entry["plugin"]
        / "scripts"
        / f"{entry['module']}.py"
    )


def status_path_for(state_path: Path) -> Path:
    return state_path.parent / f"{state_path.name}.status.json"


def write_result_file(path: Path, payload: dict[str, Any], label: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except OSError as error:
        raise WorkflowError(
            f"could not write the {label} result file: {error}"
        ) from error


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise WorkflowError(f"state file does not exist: {path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise WorkflowError(f"state file holds invalid JSON: {path}: {error}") from error
    if not isinstance(state, dict):
        raise WorkflowError(f"state file does not hold an object: {path}")
    if state.get("version") != STATE_VERSION:
        raise WorkflowError(f"unsupported state version in {path}")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = utc_now()
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def resolve_repo_root(value: str | None) -> Path:
    cwd = cli_path(value) if value else Path.cwd()
    output = run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"]).stdout.strip()
    return Path(output).resolve()


def github_repo_from_remote(url: str) -> str | None:
    patterns = (
        re.compile(
            r"^(?:https?|git|ssh)://(?:[^@/\s]+@)?github\.com(?::\d+)?/"
            r"(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?/?$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:[^@/\s]+@)?github\.com:(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?$",
            re.IGNORECASE,
        ),
    )
    for pattern in patterns:
        match = pattern.match(url)
        if match:
            return match.group("repo")
    return None


def configured_upstream(repo_root: Path, branch: str) -> dict[str, str] | None:
    remote = run(
        ["git", "-C", str(repo_root), "config", "--get", f"branch.{branch}.remote"],
        check=False,
    )
    merge = run(
        ["git", "-C", str(repo_root), "config", "--get", f"branch.{branch}.merge"],
        check=False,
    )
    if remote.returncode != 0 and merge.returncode != 0:
        return None
    if remote.returncode != 0 or merge.returncode != 0:
        raise WorkflowError(
            f"current branch {branch!r} has incomplete upstream configuration"
        )

    remote_name = remote.stdout.strip()
    merge_ref = merge.stdout.strip()
    if not remote_name or remote_name == ".":
        raise WorkflowError(
            f"current branch {branch!r} does not track a GitHub remote branch"
        )
    prefix = "refs/heads/"
    if not merge_ref.startswith(prefix) or merge_ref == prefix:
        raise WorkflowError(
            f"current branch {branch!r} has unsupported upstream merge ref {merge_ref!r}"
        )

    remote_url = run(
        ["git", "-C", str(repo_root), "remote", "get-url", remote_name]
    ).stdout.strip()
    remote_repo = github_repo_from_remote(remote_url)
    if remote_repo is None:
        raise WorkflowError(
            f"upstream remote {remote_name!r} is not a supported GitHub URL: {remote_url}"
        )
    return {
        "remote": remote_name,
        "repo": remote_repo,
        "branch": merge_ref[len(prefix) :],
    }


def pr_target_from_payload(
    payload: Any, expected_upstream: dict[str, str] | None = None
) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("url"), str):
        raise WorkflowError("gh pr view did not return a pull request URL")
    if payload.get("state") != "OPEN":
        return None
    if expected_upstream is not None:
        owner = payload.get("headRepositoryOwner")
        repository = payload.get("headRepository")
        head_repo = (
            f"{owner.get('login')}/{repository.get('name')}"
            if isinstance(owner, dict)
            and isinstance(owner.get("login"), str)
            and isinstance(repository, dict)
            and isinstance(repository.get("name"), str)
            else None
        )
        if (
            head_repo is None
            or head_repo.lower() != expected_upstream["repo"].lower()
            or payload.get("headRefName") != expected_upstream["branch"]
        ):
            return None
    return parse_target(payload["url"])


def simple_current_pr_target(
    repo_root: Path, expected_upstream: dict[str, str] | None
) -> dict[str, Any] | None:
    fields = "url,state,headRefName,headRepositoryOwner,headRepository"
    process = run(["gh", "pr", "view", "--json", fields], cwd=repo_root, check=False)
    if process.returncode != 0:
        return None
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise WorkflowError(f"gh pr view returned invalid JSON: {error}") from error
    return pr_target_from_payload(payload, expected_upstream)


def exact_upstream_pr_targets(upstream: dict[str, str]) -> list[dict[str, Any]]:
    query = """
query($owner:String!,$repo:String!,$refName:String!,$after:String){
  repository(owner:$owner,name:$repo){
    ref(qualifiedName:$refName){
      target{
        ... on Commit{
          associatedPullRequests(first:100,after:$after){
            pageInfo{hasNextPage endCursor}
            nodes{
              url state headRefName headRepository{nameWithOwner}
            }
          }
        }
      }
    }
  }
}
"""
    owner, repo = upstream["repo"].split("/", 1)
    after: str | None = None
    targets: dict[str, dict[str, Any]] = {}
    while True:
        payload = graphql(
            query,
            {
                "owner": owner,
                "repo": repo,
                "refName": f"refs/heads/{upstream['branch']}",
                "after": after,
            },
        )
        repository = payload["data"].get("repository") or {}
        ref = repository.get("ref") or {}
        commit = ref.get("target") or {}
        connection = commit.get("associatedPullRequests")
        if connection is None:
            return []
        for node in connection["nodes"]:
            head_repository = node.get("headRepository") or {}
            if (
                node.get("state") == "OPEN"
                and node.get("headRefName") == upstream["branch"]
                and head_repository.get("nameWithOwner", "").lower()
                == upstream["repo"].lower()
            ):
                target = parse_target(node["url"])
                targets[target["pr_url"]] = target
        if not connection["pageInfo"]["hasNextPage"]:
            return list(targets.values())
        after = connection["pageInfo"]["endCursor"]


def current_pr_target(repo_root: Path) -> dict[str, Any]:
    branch = git(repo_root, "branch", "--show-current")
    if not branch:
        raise WorkflowError("cannot resolve the current pull request from detached HEAD")
    upstream = configured_upstream(repo_root, branch)

    if upstream is None or branch == upstream["branch"]:
        target = simple_current_pr_target(repo_root, upstream)
        if upstream is None and target is not None:
            return target
    if upstream is None:
        raise WorkflowError(
            f"no pull request found for current branch {branch!r}, "
            "which has no configured upstream"
        )

    targets = exact_upstream_pr_targets(upstream)
    if not targets:
        raise WorkflowError(
            "no open pull request found for upstream "
            f"{upstream['repo']}:{upstream['branch']}"
        )
    if len(targets) > 1:
        urls = ", ".join(sorted(target["pr_url"] for target in targets))
        raise WorkflowError(
            "multiple open pull requests found for upstream "
            f"{upstream['repo']}:{upstream['branch']}: {urls}"
        )
    return targets[0]


def repo_name_for(repo_root: Path) -> str | None:
    process = run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"], check=False
    )
    if process.returncode != 0:
        return None
    return github_repo_from_remote(process.stdout.strip())


def resolve_target(value: str | None, repo_root: Path) -> dict[str, Any]:
    if not value:
        return current_pr_target(repo_root)
    return parse_target(value, repo_name_for(repo_root))


def observe_pull_request(target: dict[str, Any]) -> dict[str, Any]:
    """Read every live GitHub fact the stage decisions depend on."""

    fields = (
        "number,title,url,state,isDraft,mergeable,mergeStateStatus,headRefName,"
        "headRefOid,headRepositoryOwner,headRepository,baseRefName,baseRefOid,"
        "statusCheckRollup"
    )
    payload: dict[str, Any] = {}
    for attempt, delay in enumerate((*MERGEABLE_RETRY_DELAYS, None)):
        raw = gh_json(
            [
                "pr",
                "view",
                target["pr_url"],
                "--repo",
                target["repo_name"],
                "--json",
                fields,
            ]
        )
        if not isinstance(raw, dict):
            raise WorkflowError("gh pr view did not return PR metadata")
        payload = raw
        if payload.get("mergeable") != "UNKNOWN" or delay is None:
            break
        # GitHub computes mergeability in the background and reports UNKNOWN until
        # it finishes. Asking again is the only way to turn that into a fact.
        time.sleep(delay)

    head_sha = payload.get("headRefOid")
    if not isinstance(head_sha, str) or not head_sha:
        raise WorkflowError("resolved PR metadata has no head commit")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise WorkflowError("resolved PR metadata has no title")
    head_owner = payload.get("headRepositoryOwner")
    head_repository = payload.get("headRepository")
    if (
        not isinstance(head_owner, dict)
        or not isinstance(head_owner.get("login"), str)
        or not isinstance(head_repository, dict)
        or not isinstance(head_repository.get("name"), str)
    ):
        raise WorkflowError(
            "pull request head repository is unavailable; it may have been deleted"
        )
    return {
        "pr": {
            "number": target["number"],
            "title": title.strip(),
            "pr_url": target["pr_url"],
            "repo_name": target["repo_name"],
            "owner": target["owner"],
            "repo": target["repo"],
            "head_owner": head_owner["login"],
            "head_repo": head_repository["name"],
            "head_branch": payload.get("headRefName"),
            "base_branch": payload.get("baseRefName"),
            "is_draft": bool(payload.get("isDraft")),
        },
        "state": payload.get("state"),
        "head_sha": head_sha,
        "base_sha": payload.get("baseRefOid"),
        "mergeable": payload.get("mergeable"),
        "merge_state_status": payload.get("mergeStateStatus"),
        "checks": summarize_checks(payload.get("statusCheckRollup")),
    }


def check_conclusion(node: Any) -> str:
    """Reduce one status check rollup node to a single upper-case state."""

    if not isinstance(node, dict):
        return "UNKNOWN"
    status = str(node.get("status") or "").upper()
    conclusion = str(node.get("conclusion") or "").upper()
    state = str(node.get("state") or "").upper()
    if status and status != "COMPLETED":
        return status
    if conclusion:
        return conclusion
    if state:
        return state
    if status:
        return status
    return "UNKNOWN"


def summarize_checks(rollup: Any) -> dict[str, Any]:
    """Turn the rollup into counts plus one overall state.

    An empty rollup is reported as ``none`` rather than as success. A repository
    with no applicable checks must never look like a passing pipeline.
    """

    nodes = rollup if isinstance(rollup, list) else []
    counts: dict[str, int] = {}
    failing: list[dict[str, str]] = []
    pending: list[dict[str, str]] = []
    for node in nodes:
        conclusion = check_conclusion(node)
        counts[conclusion] = counts.get(conclusion, 0) + 1
        name = ""
        if isinstance(node, dict):
            name = str(node.get("name") or node.get("context") or "")
        if conclusion in CHECK_FAILURE_STATES:
            failing.append({"name": name, "state": conclusion})
        elif conclusion not in CHECK_SUCCESS_STATES:
            pending.append({"name": name, "state": conclusion})

    if not nodes:
        overall = "none"
    elif failing:
        overall = "failing"
    elif pending:
        overall = "pending"
    else:
        overall = "success"
    return {
        "state": overall,
        "total": len(nodes),
        "counts": counts,
        "failing": failing,
        "pending": pending,
        "action_required": [
            entry for entry in failing if entry["state"] == "ACTION_REQUIRED"
        ],
    }


def read_stage_marker(entry: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """Read one stage's clean-at-head record from its own helper.

    The pipeline never reads a stage's prose report. A stage whose result is a
    judgment leaves the only durable record of that judgment in its own state
    file, so the helper that owns the file is the only thing that may interpret
    it.
    """

    if entry["evidence"] != "helper":
        return {"source": "github", "available": True, "clean_at_head_sha": None}

    state_path = stage_state_path(entry["plugin"], target)
    script = stage_script_path(entry)
    if not script.is_file():
        return {
            "source": "helper",
            "available": False,
            "reason": "helper_missing",
            "script": str(script),
            "clean_at_head_sha": None,
        }
    if not state_path.is_file():
        return {
            "source": "helper",
            "available": True,
            "reason": "no_state",
            "state": str(state_path),
            "clean_at_head_sha": None,
        }
    process = run(
        [sys.executable, str(script), "status", "--state", str(state_path)],
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "no output"
        return {
            "source": "helper",
            "available": False,
            "reason": "status_failed",
            "state": str(state_path),
            "detail": detail,
            "clean_at_head_sha": None,
        }
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        return {
            "source": "helper",
            "available": False,
            "reason": "invalid_status_json",
            "state": str(state_path),
            "detail": str(error),
            "clean_at_head_sha": None,
        }
    return {
        "source": "helper",
        "available": True,
        "state": str(state_path),
        "clean_at_head_sha": extract_clean_at_head_sha(entry["stage"], payload),
        "status_result": payload.get("result") if isinstance(payload, dict) else None,
    }


def extract_clean_at_head_sha(stage: str, payload: Any) -> str | None:
    """Pull the clean-at-head SHA out of one stage helper's status envelope.

    Each stage names the field differently because each one was built on its own.
    The pipeline keeps that translation in one place so a stage's own wording
    never leaks into a decision.
    """

    if not isinstance(payload, dict) or payload.get("result") != "ready":
        return None
    if stage == STAGE_SELF_REVIEW:
        review = payload.get("review")
        if not isinstance(review, dict):
            return None
        if review.get("outcome") != "clean":
            return None
        return sha_or_none(review.get("clean_at_head_sha"))
    if stage == STAGE_COPILOT_REVIEW:
        direct = sha_or_none(payload.get("clean_at_head_sha"))
        if direct:
            return direct
        for key in ("queue", "monitoring", "review"):
            section = payload.get(key)
            if isinstance(section, dict):
                nested = sha_or_none(section.get("clean_at_head_sha"))
                if nested:
                    return nested
        return None
    if stage == STAGE_DESCRIPTION:
        return sha_or_none(payload.get("validated_head_sha"))
    return None


def sha_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def stage_green(
    entry: dict[str, Any],
    *,
    head_sha: str,
    cleared: dict[str, Any],
    marker: dict[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any]:
    """Decide whether one stage is green at the current head.

    A stage whose truth lives on GitHub can be green without ever running,
    because GitHub already states the fact the stage exists to establish. GitHub
    is also the only thing that may retract it, so a recorded clearance never
    speaks for such a stage: checks that pass and then fail again at the same
    head must show through.

    A stage whose truth is a judgment can only be green when its own helper
    recorded that judgment at this exact head. The pipeline's own record stands
    in for the helper there, so a stage stays green after it cleans up its state.
    """

    recorded = sha_or_none((cleared or {}).get(entry["stage"]))

    if entry["evidence"] == "helper":
        if recorded == head_sha:
            return {
                "green": True,
                "evidence": "recorded",
                "clean_at_head_sha": recorded,
            }
        if not marker.get("available"):
            return {
                "green": False,
                "evidence": "helper_unavailable",
                "reason": marker.get("reason"),
                "detail": marker.get("detail"),
            }
        clean = sha_or_none(marker.get("clean_at_head_sha"))
        if clean == head_sha:
            return {"green": True, "evidence": "helper", "clean_at_head_sha": clean}
        return {"green": False, "evidence": "helper", "clean_at_head_sha": clean}

    if entry["stage"] == STAGE_CONFLICT:
        mergeable = observation.get("mergeable")
        return {
            "green": mergeable == "MERGEABLE",
            "evidence": "github",
            "mergeable": mergeable,
            "recorded_at_head_sha": recorded,
        }

    if entry["stage"] == STAGE_CI:
        checks = observation.get("checks") or {}
        return {
            "green": checks.get("state") == "success",
            "evidence": "github",
            "checks": checks.get("state"),
            "recorded_at_head_sha": recorded,
        }

    return {"green": False, "evidence": "unknown"}


def projected_iteration(state: dict[str, Any], stage: str) -> dict[str, Any]:
    """Work out which pipeline iteration running ``stage`` next would belong to.

    An iteration is one pass down the stage order. Choosing a stage that sits
    earlier than the furthest stage this iteration already started means the head
    moved under a stage that had already run, so the pipeline is going round
    again.
    """

    iteration = int(state.get("iteration") or 1)
    high_water = state.get("stage_high_water")
    index = STAGE_INDEX[stage]
    if not isinstance(high_water, int):
        return {"iteration": iteration, "loop_back": False, "high_water": index}
    if index >= high_water:
        return {
            "iteration": iteration,
            "loop_back": False,
            "high_water": max(index, high_water),
        }
    return {"iteration": iteration + 1, "loop_back": True, "high_water": index}


def no_progress_streak(state: dict[str, Any], stage: str) -> int:
    streaks = state.get("no_progress") or {}
    entry = streaks.get(stage)
    if isinstance(entry, dict):
        return int(entry.get("count") or 0)
    return 0


def decide_next(state: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    """Choose what the pipeline does next. This is the whole control flow.

    The result is one of ``escalate``, ``complete``, or ``run_stage``. Nothing
    here reads a stage's prose, and nothing here looks at the base branch: base
    movement deliberately triggers no re-review and no fresh check wait.
    """

    head_sha = observation["head_sha"]
    max_iterations = int(state.get("max_iterations") or DEFAULT_MAX_ITERATIONS)

    escalation = state.get("escalation")
    if isinstance(escalation, dict) and escalation:
        return {
            "result": "escalate",
            "stage": escalation.get("stage"),
            "reason": escalation.get("reason"),
            "detail": escalation.get("detail"),
            "next_action": escalation.get("next_action"),
            "head_sha": head_sha,
            "recorded": True,
        }

    if observation.get("state") not in (None, "OPEN"):
        return {
            "result": "escalate",
            "stage": None,
            "reason": "pr_not_open",
            "detail": (
                f"the pull request is {observation.get('state')}, so the pipeline "
                "has nothing to drive"
            ),
            "next_action": ESCALATION_ACTIONS["pr_not_open"],
            "head_sha": head_sha,
            "recorded": False,
        }

    cleared = state.get("cleared") or {}
    markers = observation.get("stage_markers") or {}
    stage_states: dict[str, Any] = {}
    next_entry: dict[str, Any] | None = None
    for entry in STAGES:
        verdict = stage_green(
            entry,
            head_sha=head_sha,
            cleared=cleared,
            marker=markers.get(entry["stage"]) or {},
            observation=observation,
        )
        stage_states[entry["stage"]] = verdict
        if next_entry is None and not verdict["green"]:
            next_entry = entry

    if next_entry is None:
        return {
            "result": "complete",
            "head_sha": head_sha,
            "iteration": int(state.get("iteration") or 1),
            "max_iterations": max_iterations,
            "stage_states": stage_states,
        }

    stage = next_entry["stage"]
    verdict = stage_states[stage]
    if verdict.get("evidence") == "helper_unavailable":
        reason = verdict.get("reason") or "helper_missing"
        detail = (
            f"the {stage} helper could not report its state: "
            f"{verdict.get('detail') or reason}"
        )
        return {
            "result": "escalate",
            "stage": stage,
            "reason": "helper_missing",
            "detail": detail,
            "next_action": ESCALATION_ACTIONS["helper_missing"],
            "head_sha": head_sha,
            "recorded": False,
        }

    streak = no_progress_streak(state, stage)
    if streak >= NO_PROGRESS_LIMIT:
        return {
            "result": "escalate",
            "stage": stage,
            "reason": "no_progress",
            "detail": (
                f"{stage} ran {streak} times in a row without changing anything"
            ),
            "next_action": ESCALATION_ACTIONS["no_progress"],
            "head_sha": head_sha,
            "recorded": False,
        }

    projection = projected_iteration(state, stage)
    if projection["iteration"] > max_iterations:
        return {
            "result": "escalate",
            "stage": stage,
            "reason": "max_iterations_reached",
            "detail": (
                f"running {stage} again would start iteration "
                f"{projection['iteration']} of a maximum of {max_iterations}"
            ),
            "next_action": ESCALATION_ACTIONS["max_iterations_reached"],
            "head_sha": head_sha,
            "recorded": False,
        }

    return {
        "result": "run_stage",
        "stage": stage,
        "stage_index": STAGE_INDEX[stage],
        "summary": next_entry["summary"],
        "head_sha": head_sha,
        "iteration": projection["iteration"],
        "loop_back": projection["loop_back"],
        "max_iterations": max_iterations,
        "stage_states": stage_states,
    }


def stage_models(state: dict[str, Any]) -> dict[str, str]:
    configured = state.get("stage_models")
    models = {entry["stage"]: DEFAULT_STAGE_MODEL for entry in STAGES}
    if isinstance(configured, dict):
        for stage, model in configured.items():
            if stage in models and isinstance(model, str) and model.strip():
                models[stage] = model.strip()
    return models


def model_family(model: str) -> str:
    name = (model or "").strip().lower()
    if name.startswith("claude"):
        return CLAUDE_FAMILY
    if name.startswith("gpt") or name.startswith("o1") or name.startswith("o3"):
        return "gpt"
    if name.startswith("gemini"):
        return "gemini"
    if name.startswith("grok"):
        return "grok"
    return "other"


def gate_stage_models(models: dict[str, str], *, can_pin: bool) -> dict[str, Any]:
    """Check every stage against the model family it requires.

    ``self-review-loop`` runs a fixed GPT evaluator and refuses to grade its own
    findings, so it has to run on a Claude model. No frontmatter key sets a
    model, so the launcher pins one. When the launcher cannot pin a model the
    stage inherits the pipeline's, and that gate can fail.
    """

    stages = []
    blocked = []
    for entry in STAGES:
        model = models[entry["stage"]]
        required = entry["requires_family"]
        satisfied = required is None or model_family(model) == required
        stages.append(
            {
                "stage": entry["stage"],
                "agent": entry["agent"],
                "model": model,
                "requires_family": required,
                "satisfied": satisfied,
                "pinned": can_pin,
            }
        )
        if not satisfied:
            blocked.append(entry["stage"])
    return {
        "result": "blocked" if blocked else "ready",
        "stages": stages,
        "blocked": blocked,
        "can_pin": can_pin,
    }


def launch_plan(
    state: dict[str, Any], stage: str, *, effort: str = DEFAULT_EFFORT
) -> dict[str, Any]:
    """Build the exact launch instructions for one stage.

    The plugin-qualified agent reference is built here rather than typed by the
    model. A bare basename silently resolves to the default agent and reports no
    error, so the reference is never left to a judgment call.
    """

    entry = STAGE_BY_NAME[stage]
    model = stage_models(state)[stage]
    pr = state["pr"]
    target = f"{pr['repo_name']}#{pr['number']}"
    return {
        "stage": stage,
        "plugin": entry["plugin"],
        "agent": entry["agent"],
        "model": model,
        "effort": effort,
        "target": target,
        "session_name": f"PR Pipeline {stage}: {pr['number']} - {pr['title']}",
        "command": [
            "copilot",
            "-p",
            target,
            "--agent",
            entry["agent"],
            "--model",
            model,
            "--effort",
            effort,
        ],
    }


def new_state(
    target: dict[str, Any],
    observation: dict[str, Any],
    repo_root: Path,
    max_iterations: int,
) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "created_at": utc_now(),
        "repo_root": str(repo_root),
        "pr": observation["pr"],
        "max_iterations": max_iterations,
        "iteration": 1,
        "stage_high_water": None,
        "stage_models": {entry["stage"]: DEFAULT_STAGE_MODEL for entry in STAGES},
        "cleared": {},
        "no_progress": {},
        "running": None,
        "history": [],
        "escalation": None,
        "completed": None,
    }


def collect_observation(
    target: dict[str, Any], *, with_markers: bool = True
) -> dict[str, Any]:
    observation = observe_pull_request(target)
    markers: dict[str, Any] = {}
    if with_markers:
        for entry in STAGES:
            markers[entry["stage"]] = read_stage_marker(entry, target)
    observation["stage_markers"] = markers
    return observation


def sync_cleared(state: dict[str, Any], decision: dict[str, Any]) -> None:
    """Record every stage the decision found green at the current head.

    Live GitHub evidence clears a stage without running it. A merge-clean pull
    request needs no conflict run, and a green check rollup needs no check run.
    """

    head_sha = decision.get("head_sha")
    stage_states = decision.get("stage_states") or {}
    cleared = state.setdefault("cleared", {})
    for stage, verdict in stage_states.items():
        if verdict.get("green") and head_sha:
            cleared[stage] = head_sha


def record_escalation(
    state: dict[str, Any], decision: dict[str, Any]
) -> dict[str, Any]:
    escalation = {
        "stage": decision.get("stage"),
        "reason": decision.get("reason"),
        "detail": decision.get("detail"),
        "next_action": decision.get("next_action"),
        "head_sha": decision.get("head_sha"),
        "at": utc_now(),
    }
    state["escalation"] = escalation
    state["running"] = None
    return escalation


def command_preflight(args: argparse.Namespace) -> None:
    require_tools()
    repo_root = resolve_repo_root(args.repo_root)
    target = resolve_target(args.target, repo_root)
    path = cli_path(args.state) if args.state else default_state_path(target)
    max_iterations = max(1, int(args.max_iterations))

    observation = collect_observation(target)
    if observation.get("state") != "OPEN":
        raise WorkflowError(
            f"pull request {target['pr_url']} is {observation.get('state')}; "
            "the pipeline only drives an open pull request"
        )

    if path.is_file():
        state = load_state(path)
        if state["pr"]["pr_url"] != observation["pr"]["pr_url"]:
            raise WorkflowError(
                f"state file {path} belongs to {state['pr']['pr_url']}"
            )
        state["pr"] = observation["pr"]
        state["repo_root"] = str(repo_root)
        state["max_iterations"] = max_iterations
        resumed = True
    else:
        state = new_state(target, observation, repo_root, max_iterations)
        resumed = False

    for assignment in args.stage_model or []:
        stage, separator, model = assignment.partition("=")
        if not separator or stage not in STAGE_BY_NAME or not model.strip():
            raise WorkflowError(
                f"--stage-model expects <stage>=<model> for a known stage: {assignment}"
            )
        state.setdefault("stage_models", {})[stage] = model.strip()

    save_state(path, state)
    gate = gate_stage_models(stage_models(state), can_pin=not args.no_pin)
    emit(
        {
            "result": "blocked" if gate["result"] == "blocked" else "ready",
            "resumed": resumed,
            "state": str(path),
            "repo_root": str(repo_root),
            "pr": state["pr"],
            "head_sha": observation["head_sha"],
            "is_draft": state["pr"]["is_draft"],
            "iteration": state["iteration"],
            "max_iterations": state["max_iterations"],
            "cleared": state.get("cleared") or {},
            "model_gate": gate,
            "stages": list(STAGE_NAMES),
        }
    )


def command_next(args: argparse.Namespace) -> None:
    require_tools()
    path = cli_path(args.state)
    state = load_state(path)
    target = build_target(
        state["pr"]["owner"], state["pr"]["repo"], state["pr"]["number"]
    )
    observation = collect_observation(target)
    decision = decide_next(state, observation)

    if decision["result"] == "run_stage":
        sync_cleared(state, decision)
        save_state(path, state)
        plan = launch_plan(state, decision["stage"], effort=args.effort)
        emit(
            {
                **{key: value for key, value in decision.items() if key != "stage_states"},
                "state": str(path),
                "plan": plan,
                "stage_states": decision["stage_states"],
                "checks": observation["checks"],
                "mergeable": observation["mergeable"],
                "cleared": state.get("cleared") or {},
            }
        )
        return

    if decision["result"] == "complete":
        sync_cleared(state, decision)
        state["completed"] = {"at": utc_now(), "head_sha": decision["head_sha"]}
        state["running"] = None
        save_state(path, state)
        emit(
            {
                **decision,
                "state": str(path),
                "checks": observation["checks"],
                "mergeable": observation["mergeable"],
                "cleared": state.get("cleared") or {},
                "reminder": (
                    "The pipeline never marks a pull request ready for review and "
                    "never touches approval. Leaving the draft is the user's call."
                ),
            }
        )
        return

    if not decision.get("recorded"):
        record_escalation(state, decision)
        save_state(path, state)
    emit(
        {
            **decision,
            "state": str(path),
            "checks": observation["checks"],
            "mergeable": observation["mergeable"],
        }
    )


def command_start(args: argparse.Namespace) -> None:
    """Record that a stage is starting, and charge it to an iteration."""

    path = cli_path(args.state)
    state = load_state(path)
    stage = args.stage
    if stage not in STAGE_BY_NAME:
        raise WorkflowError(f"unknown stage: {stage}")
    if state.get("escalation"):
        raise WorkflowError("the pipeline already escalated; it cannot start a stage")
    running = state.get("running")
    if isinstance(running, dict) and running:
        raise WorkflowError(
            f"stage {running.get('stage')} is already recorded as running; "
            "finish it before starting another"
        )

    projection = projected_iteration(state, stage)
    max_iterations = int(state.get("max_iterations") or DEFAULT_MAX_ITERATIONS)
    if projection["iteration"] > max_iterations:
        decision = {
            "stage": stage,
            "reason": "max_iterations_reached",
            "detail": (
                f"running {stage} again would start iteration "
                f"{projection['iteration']} of a maximum of {max_iterations}"
            ),
            "next_action": ESCALATION_ACTIONS["max_iterations_reached"],
            "head_sha": args.head,
        }
        escalation = record_escalation(state, decision)
        save_state(path, state)
        emit({"result": "escalated", "state": str(path), "escalation": escalation})
        return

    state["iteration"] = projection["iteration"]
    state["stage_high_water"] = projection["high_water"]
    state["running"] = {
        "stage": stage,
        "head_sha": args.head,
        "iteration": projection["iteration"],
        "launch": args.launch,
        "session_id": args.session,
        "process_id": args.process,
        "model": stage_models(state)[stage],
        "started_at": utc_now(),
    }
    save_state(path, state)
    emit(
        {
            "result": "started",
            "state": str(path),
            "stage": stage,
            "iteration": state["iteration"],
            "max_iterations": max_iterations,
            "loop_back": projection["loop_back"],
            "running": state["running"],
        }
    )


def command_finish(args: argparse.Namespace) -> None:
    """Record how a stage ended, and keep the durable history entry."""

    path = cli_path(args.state)
    state = load_state(path)
    stage = args.stage
    if stage not in STAGE_BY_NAME:
        raise WorkflowError(f"unknown stage: {stage}")
    running = state.get("running")
    if not isinstance(running, dict) or running.get("stage") != stage:
        raise WorkflowError(
            f"stage {stage} is not recorded as running; start it before finishing it"
        )

    head_sha = args.head or running.get("head_sha")
    entry = {
        "stage": stage,
        "outcome": args.outcome,
        "iteration": running.get("iteration"),
        "started_head_sha": running.get("head_sha"),
        "head_sha": head_sha,
        "started_at": running.get("started_at"),
        "ended_at": utc_now(),
        "session_id": args.session or running.get("session_id"),
        "process_id": args.process or running.get("process_id"),
        "launch": running.get("launch"),
        "model": running.get("model"),
        "detail": args.detail,
    }
    state.setdefault("history", []).append(entry)
    state["running"] = None

    streaks = state.setdefault("no_progress", {})
    if args.outcome == "no_progress":
        previous = streaks.get(stage)
        count = int(previous.get("count") or 0) + 1 if isinstance(previous, dict) else 1
        streaks[stage] = {"count": count, "head_sha": head_sha, "at": utc_now()}
    else:
        streaks.pop(stage, None)

    escalation = None
    if args.outcome in CLEARING_OUTCOMES and head_sha:
        state.setdefault("cleared", {})[stage] = head_sha
    elif args.outcome == "escalated":
        escalation = record_escalation(
            state,
            {
                "stage": stage,
                "reason": "stage_escalated",
                "detail": args.detail
                or f"{stage} stopped without clearing and asked for a person",
                "next_action": ESCALATION_ACTIONS["stage_escalated"],
                "head_sha": head_sha,
            },
        )
    elif args.outcome == "no_progress":
        count = int((streaks.get(stage) or {}).get("count") or 0)
        if count >= NO_PROGRESS_LIMIT:
            escalation = record_escalation(
                state,
                {
                    "stage": stage,
                    "reason": "no_progress",
                    "detail": (
                        f"{stage} ran {count} times in a row without changing anything"
                    ),
                    "next_action": ESCALATION_ACTIONS["no_progress"],
                    "head_sha": head_sha,
                },
            )

    save_state(path, state)
    emit(
        {
            "result": "escalated" if escalation else "recorded",
            "state": str(path),
            "stage": stage,
            "outcome": args.outcome,
            "entry": entry,
            "cleared": state.get("cleared") or {},
            "no_progress": state.get("no_progress") or {},
            "escalation": escalation,
            "keep_session": args.outcome != "cleared",
        }
    )


def command_escalate(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    if args.stage is not None and args.stage not in STAGE_BY_NAME:
        raise WorkflowError(f"unknown stage: {args.stage}")
    escalation = record_escalation(
        state,
        {
            "stage": args.stage,
            "reason": args.reason,
            "detail": args.detail,
            "next_action": args.next_action
            or ESCALATION_ACTIONS.get(args.reason)
            or "Read the kept stage session and decide what to do next.",
            "head_sha": args.head,
        },
    )
    save_state(path, state)
    emit({"result": "escalated", "state": str(path), "escalation": escalation})


def command_models(args: argparse.Namespace) -> None:
    if args.state:
        state = load_state(cli_path(args.state))
        models = stage_models(state)
    else:
        models = {entry["stage"]: DEFAULT_STAGE_MODEL for entry in STAGES}
    gate = gate_stage_models(models, can_pin=not args.no_pin)
    payload = {**gate, "pipeline_model": args.pipeline_model}
    if args.pipeline_model:
        payload["pipeline_model_family"] = model_family(args.pipeline_model)
    if gate["result"] == "blocked":
        payload["next_action"] = ESCALATION_ACTIONS["model_gate"]
    emit(payload)


def command_plan(args: argparse.Namespace) -> None:
    state = load_state(cli_path(args.state))
    if args.stage not in STAGE_BY_NAME:
        raise WorkflowError(f"unknown stage: {args.stage}")
    emit({"result": "ready", **launch_plan(state, args.stage, effort=args.effort)})


def summarize_history(history: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in history:
        outcome = str(entry.get("outcome") or "unknown")
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def command_status(args: argparse.Namespace) -> None:
    if args.current:
        require_tools()
        repo_root = resolve_repo_root(args.repo_root)
        target = current_pr_target(repo_root)
        path = default_state_path(target)
        if not path.is_file():
            emit(
                {
                    "result": "no_state",
                    "state": str(path),
                    "pr": {"number": target["number"], "url": target["pr_url"]},
                }
            )
            return
    else:
        path = cli_path(args.state)
    state = load_state(path)
    history = state.get("history") or []
    payload = {
        "result": "ready",
        "state": str(path),
        "pr": state["pr"],
        "iteration": state.get("iteration"),
        "max_iterations": state.get("max_iterations"),
        "stage_high_water": state.get("stage_high_water"),
        "cleared": state.get("cleared") or {},
        "no_progress": state.get("no_progress") or {},
        "running": state.get("running"),
        "escalation": state.get("escalation"),
        "completed": state.get("completed"),
        "stage_models": stage_models(state),
        "history": history,
    }
    status_path = status_path_for(path)
    write_result_file(status_path, payload, "status")
    emit(
        {
            "result": "ready",
            "state": str(path),
            "status_path": str(status_path),
            "pr": {
                "number": state["pr"]["number"],
                "title": state["pr"]["title"],
                "pr_url": state["pr"]["pr_url"],
                "repo_name": state["pr"]["repo_name"],
                "head_branch": state["pr"]["head_branch"],
                "base_branch": state["pr"]["base_branch"],
            },
            "iteration": state.get("iteration"),
            "max_iterations": state.get("max_iterations"),
            "cleared": state.get("cleared") or {},
            "running": state.get("running"),
            "escalation": state.get("escalation"),
            "completed": state.get("completed"),
            "counts": {
                "history": len(history),
                "outcomes": summarize_history(history),
            },
        }
    )


def command_cleanup(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    running = state.get("running")
    if isinstance(running, dict) and running and not args.force:
        raise WorkflowError(
            f"stage {running.get('stage')} is still recorded as running; "
            "finish it or pass --force"
        )
    path.unlink()
    status_path_for(path).unlink(missing_ok=True)
    emit({"result": "cleaned_up", "state": str(path)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser(
        "preflight",
        help="resolve the pull request and open or resume the pipeline state",
    )
    preflight.add_argument(
        "target",
        nargs="?",
        help="PR URL, owner/repo#number, or bare number; omit to use the current PR",
    )
    preflight.add_argument("--repo-root")
    preflight.add_argument("--state")
    preflight.add_argument(
        "--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS
    )
    preflight.add_argument(
        "--stage-model",
        action="append",
        help="pin one stage's model as <stage>=<model>; repeatable",
    )
    preflight.add_argument(
        "--no-pin",
        action="store_true",
        help="the launcher cannot pin a model, so stages inherit the pipeline's",
    )
    preflight.set_defaults(function=command_preflight)

    next_command = subparsers.add_parser(
        "next", help="decide the next stage from live GitHub state and stage helpers"
    )
    next_command.add_argument("--state", required=True)
    next_command.add_argument("--effort", default=DEFAULT_EFFORT)
    next_command.set_defaults(function=command_next)

    start = subparsers.add_parser("start", help="record that a stage is starting")
    start.add_argument("--state", required=True)
    start.add_argument("--stage", required=True, choices=list(STAGE_NAMES))
    start.add_argument("--head", required=True)
    start.add_argument("--launch", choices=["session", "subprocess"], required=True)
    start.add_argument("--session")
    start.add_argument("--process")
    start.set_defaults(function=command_start)

    finish = subparsers.add_parser("finish", help="record how a stage ended")
    finish.add_argument("--state", required=True)
    finish.add_argument("--stage", required=True, choices=list(STAGE_NAMES))
    finish.add_argument("--outcome", required=True, choices=list(STAGE_OUTCOMES))
    finish.add_argument("--head")
    finish.add_argument("--session")
    finish.add_argument("--process")
    finish.add_argument("--detail")
    finish.set_defaults(function=command_finish)

    escalate = subparsers.add_parser("escalate", help="stop the pipeline and say why")
    escalate.add_argument("--state", required=True)
    escalate.add_argument("--stage")
    escalate.add_argument("--reason", required=True)
    escalate.add_argument("--detail", required=True)
    escalate.add_argument("--next-action", dest="next_action")
    escalate.add_argument("--head")
    escalate.set_defaults(function=command_escalate)

    models = subparsers.add_parser(
        "models", help="report the pinned per-stage models and check their gates"
    )
    models.add_argument("--state")
    models.add_argument("--pipeline-model")
    models.add_argument("--no-pin", action="store_true")
    models.set_defaults(function=command_models)

    plan = subparsers.add_parser(
        "plan", help="print the exact launch instructions for one stage"
    )
    plan.add_argument("--state", required=True)
    plan.add_argument("--stage", required=True, choices=list(STAGE_NAMES))
    plan.add_argument("--effort", default=DEFAULT_EFFORT)
    plan.set_defaults(function=command_plan)

    status = subparsers.add_parser("status", help="print the pipeline state")
    status_source = status.add_mutually_exclusive_group(required=True)
    status_source.add_argument("--state")
    status_source.add_argument("--current", action="store_true")
    status.add_argument("--repo-root")
    status.set_defaults(function=command_status)

    cleanup = subparsers.add_parser("cleanup", help="delete the pipeline state")
    cleanup.add_argument("--state", required=True)
    cleanup.add_argument("--force", action="store_true")
    cleanup.set_defaults(function=command_cleanup)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.function(args)
        return 0
    except (WorkflowError, json.JSONDecodeError, OSError) as error:
        emit({"result": "error", "error": str(error)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
