#!/usr/bin/env python3
"""Deterministic mechanics for the CI Fix Loop custom agent."""

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
DEFAULT_MAX_ITERATIONS = 5
DEFAULT_POLL_INTERVAL = 60
DEFAULT_POLL_TIMEOUT = 5400
DEFAULT_NOT_STARTED_GRACE = 900
MAX_RERUNS_PER_CHECK = 1
PR_HEAD_LAG_RETRY_DELAY = 1
REMOTE_REF_LAG_RETRY_DELAYS = (1, 2, 4)
IS_WINDOWS = os.name == "nt"
PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
    r"/?(?:#\S*)?$"
)
SHORT_TARGET_PATTERN = re.compile(
    r"^(?P<owner>[^/\s]+)/(?P<repo>[^#/\s]+)#(?P<number>\d+)$"
)
NON_FAST_FORWARD_PATTERN = re.compile(r"fast[- ]forward|divergent", re.IGNORECASE)
RUN_URL_PATTERN = re.compile(r"/actions/runs/(?P<run>\d+)")
JOB_URL_PATTERN = re.compile(r"/(?:job|jobs)/(?P<job>\d+)")
LEGACY_JOB_URL_PATTERN = re.compile(r"/runs/(?P<job>\d+)(?:$|[/?#])")

# One classified vocabulary for every check, whatever GitHub calls it. Anything
# this loop does not recognize becomes "unknown", which escalates rather than
# passing silently.
CHECK_RUN_CONCLUSION_CLASSES = {
    "SUCCESS": "passed",
    "NEUTRAL": "neutral",
    "SKIPPED": "neutral",
    "FAILURE": "failed",
    "TIMED_OUT": "failed",
    "STARTUP_FAILURE": "failed",
    "CANCELLED": "failed",
    "ACTION_REQUIRED": "approval_blocked",
    "STALE": "stale",
}
CHECK_RUN_STATUS_CLASSES = {
    "QUEUED": "not_started",
    "REQUESTED": "not_started",
    "PENDING": "not_started",
    "WAITING": "approval_blocked",
    "IN_PROGRESS": "running",
}
STATUS_CONTEXT_CLASSES = {
    "SUCCESS": "passed",
    "PENDING": "running",
    "EXPECTED": "not_started",
    "FAILURE": "failed",
    "ERROR": "failed",
}
CHECK_CLASSES = (
    "passed",
    "neutral",
    "failed",
    "running",
    "not_started",
    "approval_blocked",
    "stale",
    "unknown",
)
FAILED_BASELINE_CONCLUSIONS = {
    "FAILURE",
    "TIMED_OUT",
    "STARTUP_FAILURE",
    "CANCELLED",
    "ACTION_REQUIRED",
    "ERROR",
}
PASSED_BASELINE_CONCLUSIONS = {"SUCCESS"}
APPROVAL_RUN_STATES = {"ACTION_REQUIRED", "WAITING"}
VERDICTS = ("pr_caused", "pre_existing", "flake")
WORKING_ACTIONS = ("attribute", "rerun", "fix")
ESCALATION_REASONS = (
    "approval_required",
    "checks_never_started",
    "stale_checks",
    "unknown_check_state",
    "timeout",
    "pre_existing_failures",
    "flake_failed_twice",
    "no_rerun_support",
    "max_iterations_reached",
    "unfixable_failure",
    "head_changed",
)
ESCALATION_ACTIONS = {
    "approval_required": (
        "Approve the workflow runs on the pull request yourself, then start this "
        "loop again."
    ),
    "checks_never_started": (
        "Check the repository's workflow triggers and any required approval, then "
        "start this loop again once the checks run."
    ),
    "stale_checks": (
        "Re-run the stale checks from the pull request, then start this loop again."
    ),
    "unknown_check_state": (
        "Read the named checks on the pull request yourself; this loop does not "
        "recognize the state they report."
    ),
    "timeout": (
        "Wait for the running checks to finish, then start this loop again."
    ),
    "pre_existing_failures": (
        "Fix the named checks on the base branch instead. They already fail there, "
        "so this loop must not edit the pull request to hide them."
    ),
    "flake_failed_twice": (
        "Read the named check yourself. It failed again after one automatic re-run, "
        "so it is not a flake."
    ),
    "no_rerun_support": (
        "Re-run the named check yourself from the pull request, then start this "
        "loop again."
    ),
    "max_iterations_reached": (
        "Read this loop's commits and the remaining failures, then finish them "
        "yourself."
    ),
    "unfixable_failure": (
        "Read the named check and this loop's notes, then decide what the fix "
        "should be."
    ),
    "head_changed": (
        "Someone pushed to the head branch while this loop ran. Start it again on "
        "the new head."
    ),
}


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


def parse_timestamp(value: Any) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise WorkflowError(f"invalid timestamp {value!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


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


def parse_target(target: str) -> dict[str, Any]:
    match = PR_URL_PATTERN.fullmatch(target) or SHORT_TARGET_PATTERN.fullmatch(target)
    if not match:
        raise WorkflowError("target must be a GitHub PR URL or owner/repo#number")
    values = match.groupdict()
    owner = values["owner"]
    repo = values["repo"]
    number = int(values["number"])
    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        "repo_name": f"{owner}/{repo}",
        "pr_url": f"https://github.com/{owner}/{repo}/pull/{number}",
    }


def default_state_path(target: dict[str, Any]) -> Path:
    name = f"{target['owner']}--{target['repo']}--{target['number']}.json"
    return Path.home() / ".copilot" / "run" / "ci-fix-loop" / name


def diff_path_for(state_path: Path) -> Path:
    return state_path.parent / f"{state_path.name}.diff"


def preflight_path_for(state_path: Path) -> Path:
    return state_path.parent / f"{state_path.name}.preflight.json"


def checks_path_for(state_path: Path) -> Path:
    return state_path.parent / f"{state_path.name}.checks.json"


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


def count_by_status(items: list[dict[str, Any]] | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items or []:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise WorkflowError(f"state file does not exist: {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
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


def load_text_input(path_value: str, label: str) -> str:
    try:
        text = (
            sys.stdin.read()
            if path_value == "-"
            else cli_path(path_value).read_text(encoding="utf-8")
        )
    except OSError as error:
        raise WorkflowError(f"could not read {label}: {error}") from error
    if not text.strip():
        raise WorkflowError(f"{label} must not be empty")
    return text.strip()


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


def resolve_target(value: str | None, repo_root: Path) -> dict[str, Any]:
    return parse_target(value) if value else current_pr_target(repo_root)


def metadata_for(target: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "number,title,url,state,isDraft,headRefName,headRefOid,headRepositoryOwner,"
        "headRepository,baseRefName,baseRefOid,commits"
    )
    metadata = gh_json(
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
    if not isinstance(metadata, dict):
        raise WorkflowError("gh pr view did not return PR metadata")
    metadata_url = metadata.get("url")
    if not isinstance(metadata_url, str):
        raise WorkflowError("resolved PR metadata has no URL")
    resolved = parse_target(metadata_url)
    if (
        metadata.get("number") != target["number"]
        or resolved["repo_name"].casefold() != target["repo_name"].casefold()
    ):
        raise WorkflowError("resolved PR metadata does not match the requested target")
    if metadata.get("state") != "OPEN":
        raise WorkflowError(
            f"pull request {resolved['pr_url']} is not open; this loop only fixes "
            "checks on an open pull request"
        )
    head_owner = metadata.get("headRepositoryOwner")
    head_repository = metadata.get("headRepository")
    if (
        not isinstance(head_owner, dict)
        or not isinstance(head_owner.get("login"), str)
        or not isinstance(head_repository, dict)
        or not isinstance(head_repository.get("name"), str)
    ):
        raise WorkflowError(
            "pull request head repository is unavailable; it may have been deleted"
        )
    head_sha = metadata.get("headRefOid")
    if not isinstance(head_sha, str) or not head_sha:
        raise WorkflowError("resolved PR metadata has no head commit")
    title = metadata.get("title")
    if not isinstance(title, str) or not title.strip():
        raise WorkflowError("resolved PR metadata has no title")
    raw_commits = metadata.get("commits")
    if not isinstance(raw_commits, list):
        raise WorkflowError("resolved PR metadata has no commit list")
    commits = []
    for index, commit in enumerate(raw_commits):
        if not isinstance(commit, dict):
            raise WorkflowError(f"resolved PR commit {index} is not an object")
        sha = commit.get("oid")
        headline = commit.get("messageHeadline")
        if not isinstance(sha, str) or not sha:
            raise WorkflowError(f"resolved PR commit {index} has no OID")
        if not isinstance(headline, str):
            raise WorkflowError(f"resolved PR commit {index} has no message headline")
        commits.append({"sha": sha, "message": headline.strip()})
    upstream_repo_name = f"{resolved['owner']}/{resolved['repo']}"
    head_repo_name = f"{head_owner['login']}/{head_repository['name']}"
    return {
        "number": target["number"],
        "title": title.strip(),
        "pr_url": resolved["pr_url"],
        "repo_name": upstream_repo_name,
        "upstream_owner": resolved["owner"],
        "upstream_repo": resolved["repo"],
        "head_owner": head_owner["login"],
        "head_repo": head_repository["name"],
        "head_branch": metadata["headRefName"],
        "head_sha": head_sha,
        "base_branch": metadata["baseRefName"],
        "base_sha": metadata["baseRefOid"],
        "is_fork": head_repo_name.lower() != upstream_repo_name.lower(),
        "is_draft": bool(metadata.get("isDraft")),
        "commits": commits,
    }


def changed_files_for(pr: dict[str, Any]) -> list[str]:
    payload = gh_json(
        ["pr", "view", pr["pr_url"], "--repo", pr["repo_name"], "--json", "files"]
    )
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        raise WorkflowError("gh pr view did not return the changed file list")
    paths = []
    for index, entry in enumerate(files):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise WorkflowError(f"changed file {index} has no path")
        paths.append(entry["path"])
    return sorted(set(paths))


def commit_provenance(
    repo_root: Path, commits: list[dict[str, str]]
) -> list[dict[str, Any]]:
    provenance = []
    for commit in commits:
        files = sorted(
            {
                line
                for line in git(
                    repo_root,
                    "diff-tree",
                    "--root",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "-m",
                    commit["sha"],
                ).splitlines()
                if line
            }
        )
        provenance.append({**commit, "files": files})
    return provenance


def require_checkout_head(local_head: str, pr_head: str) -> None:
    if local_head == pr_head:
        return
    raise WorkflowError(
        f"HEAD mismatch: local {local_head}, PR head {pr_head}; this loop fixes the "
        "checks GitHub ran on the PR head, so publish or reconcile local work first"
    )


def reconcile_equivalent_local_head(
    repo_root: Path,
    metadata: dict[str, Any],
    checkout_error: WorkflowError,
) -> None:
    local_head = git(repo_root, "rev-parse", "HEAD")
    pr_head = metadata["head_sha"]
    if local_head == pr_head:
        raise checkout_error

    try:
        unique_merges = git(
            repo_root, "rev-list", "--merges", f"{pr_head}..{local_head}"
        )
        cherry = git(repo_root, "cherry", pr_head, local_head)
    except WorkflowError:
        raise checkout_error

    unique_commits = [
        line[2:].strip()
        for line in cherry.splitlines()
        if line.startswith("+ ") and line[2:].strip()
    ]
    if unique_merges or unique_commits:
        unique = [line for line in unique_merges.splitlines() if line] + unique_commits
        raise WorkflowError(
            "head_moved: the PR branch was force-pushed and the clean local branch "
            f"still has unique work ({', '.join(unique)}); local {local_head}, "
            f"PR head {pr_head}"
        ) from checkout_error

    git(repo_root, "reset", "--hard", pr_head)


def checkout_pr(
    repo_root: Path, target: dict[str, Any], metadata: dict[str, Any]
) -> bool:
    current_branch = git(repo_root, "branch", "--show-current")
    on_pr_branch = current_branch == metadata["head_branch"]
    command = ["gh", "pr", "checkout", target["pr_url"]]
    if not on_pr_branch:
        command.append("--detach")
    try:
        run(command, cwd=repo_root)
    except WorkflowError as checkout_error:
        if not on_pr_branch or not NON_FAST_FORWARD_PATTERN.search(str(checkout_error)):
            raise
        reconcile_equivalent_local_head(repo_root, metadata, checkout_error)
    return on_pr_branch


def fetch_authoritative_diff(pr: dict[str, Any]) -> str:
    return run(["gh", "pr", "diff", pr["pr_url"], "--repo", pr["repo_name"]]).stdout


def entry_typename(node: dict[str, Any]) -> str:
    typename = node.get("__typename")
    if isinstance(typename, str) and typename:
        return typename
    if "context" in node:
        return "StatusContext"
    if "name" in node:
        return "CheckRun"
    raise WorkflowError(
        "status check entry has no recognizable shape: "
        f"{json.dumps(node, sort_keys=True)}"
    )


def classify_check_run(status: str, conclusion: str) -> str:
    if status == "COMPLETED":
        return CHECK_RUN_CONCLUSION_CLASSES.get(conclusion, "unknown")
    return CHECK_RUN_STATUS_CLASSES.get(status, "unknown")


def classify_status_context(state: str) -> str:
    return STATUS_CONTEXT_CLASSES.get(state, "unknown")


def normalize_rollup(nodes: Any) -> list[dict[str, Any]]:
    """Turn GitHub's status check rollup into one flat, classified list.

    Each check gets a key that stays the same across polls, so the loop can follow
    one check over time. Two checks that would share a key get a numeric suffix
    instead of overwriting each other.
    """
    if nodes is None:
        return []
    if not isinstance(nodes, list):
        raise WorkflowError("status check rollup is not a list")
    checks: list[dict[str, Any]] = []
    used: dict[str, int] = {}
    for node in nodes:
        if not isinstance(node, dict):
            raise WorkflowError("status check rollup entry is not an object")
        typename = entry_typename(node)
        if typename == "CheckRun":
            name = str(node.get("name") or "").strip()
            if not name:
                raise WorkflowError("check run entry has no name")
            workflow = str(node.get("workflowName") or "").strip()
            status = str(node.get("status") or "").upper()
            conclusion = str(node.get("conclusion") or "").upper()
            check = {
                "kind": "check_run",
                "name": name,
                "workflow": workflow or None,
                "status": status or None,
                "conclusion": conclusion or None,
                "state": None,
                "class": classify_check_run(status, conclusion),
                "url": node.get("detailsUrl") or None,
                "started_at": node.get("startedAt") or None,
                "completed_at": node.get("completedAt") or None,
                "description": None,
            }
            base_key = f"check:{workflow}/{name}" if workflow else f"check:{name}"
        elif typename == "StatusContext":
            context = str(node.get("context") or "").strip()
            if not context:
                raise WorkflowError("status context entry has no context")
            state = str(node.get("state") or "").upper()
            check = {
                "kind": "status",
                "name": context,
                "workflow": None,
                "status": None,
                "conclusion": None,
                "state": state or None,
                "class": classify_status_context(state),
                "url": node.get("targetUrl") or None,
                "started_at": node.get("createdAt") or None,
                "completed_at": None,
                "description": node.get("description") or None,
            }
            base_key = f"status:{context}"
        else:
            raise WorkflowError(f"unsupported status check entry type: {typename}")
        used[base_key] = used.get(base_key, 0) + 1
        occurrence = used[base_key]
        check["key"] = base_key if occurrence == 1 else f"{base_key}#{occurrence}"
        checks.append(check)
    return checks


def group_by_class(checks: list[dict[str, Any]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {name: [] for name in CHECK_CLASSES}
    for check in checks:
        grouped.setdefault(check["class"], []).append(check["key"])
    return grouped


def class_counts(checks: list[dict[str, Any]]) -> dict[str, int]:
    return {name: len(keys) for name, keys in group_by_class(checks).items()}


def describe_checks(checks: list[dict[str, Any]], keys: Iterable[str]) -> str:
    by_key = {check["key"]: check for check in checks}
    return ", ".join(by_key[key]["name"] for key in keys if key in by_key)


def update_check_tracking(
    tracking: Any, checks: list[dict[str, Any]], now: dt.datetime
) -> dict[str, Any]:
    """Record when each check was first seen and when it last entered not-started.

    A check that starts running, or that a re-run puts back in the queue, gets a
    fresh not-started clock. An ordinary queue wait must never look like a check
    that never starts.
    """
    stamp = now.isoformat().replace("+00:00", "Z")
    tracking = tracking if isinstance(tracking, dict) else {}
    updated: dict[str, Any] = {}
    for check in checks:
        key = check["key"]
        previous = tracking.get(key)
        previous = previous if isinstance(previous, dict) else {}
        entry = {
            "first_seen_at": previous.get("first_seen_at") or stamp,
            "last_class": check["class"],
            "last_seen_at": stamp,
        }
        if check["class"] == "not_started":
            entry["not_started_since"] = (
                previous.get("not_started_since")
                if previous.get("last_class") == "not_started"
                and previous.get("not_started_since")
                else stamp
            )
        updated[key] = entry
    return updated


def not_started_seconds(tracking: Any, key: str, now: dt.datetime) -> float:
    entry = tracking.get(key) if isinstance(tracking, dict) else None
    since = entry.get("not_started_since") if isinstance(entry, dict) else None
    if not since:
        return 0.0
    return max(0.0, (now - parse_timestamp(since)).total_seconds())


def decide(
    checks: list[dict[str, Any]],
    *,
    now: dt.datetime,
    tracking: Any = None,
    not_started_grace: int = DEFAULT_NOT_STARTED_GRACE,
    deadline_expired: bool = False,
    approval_runs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Decide what the loop does next from one classified snapshot.

    The order matters. Every state that cannot resolve on its own escalates before
    the loop is allowed to decide to wait, and an empty rollup gets its own outcome
    rather than counting as success.
    """
    approval_runs = approval_runs or []
    grouped = group_by_class(checks)

    approval_blocked = grouped["approval_blocked"]
    if approval_blocked:
        return {
            "decision": "escalate",
            "reason": "approval_required",
            "checks": sorted(approval_blocked),
            "detail": (
                "these checks wait for a maintainer to approve the run: "
                f"{describe_checks(checks, sorted(approval_blocked))}"
            ),
        }
    if not checks and approval_runs:
        names = ", ".join(
            str(entry.get("name") or entry.get("id")) for entry in approval_runs
        )
        return {
            "decision": "escalate",
            "reason": "approval_required",
            "checks": [],
            "detail": (
                "the pull request reports no checks because these workflow runs wait "
                f"for a maintainer to approve them: {names}"
            ),
        }

    unknown = grouped["unknown"]
    if unknown:
        return {
            "decision": "escalate",
            "reason": "unknown_check_state",
            "checks": sorted(unknown),
            "detail": (
                "these checks report a state this loop does not understand: "
                f"{describe_checks(checks, sorted(unknown))}"
            ),
        }

    stale = grouped["stale"]
    if stale:
        return {
            "decision": "escalate",
            "reason": "stale_checks",
            "checks": sorted(stale),
            "detail": (
                "these checks report a stale result that will not refresh at this "
                f"head: {describe_checks(checks, sorted(stale))}"
            ),
        }

    not_started = grouped["not_started"]
    overdue = sorted(
        key
        for key in not_started
        if not_started_seconds(tracking, key, now) >= not_started_grace
    )
    if overdue:
        return {
            "decision": "escalate",
            "reason": "checks_never_started",
            "checks": overdue,
            "detail": (
                f"these checks have not started after {not_started_grace} seconds: "
                f"{describe_checks(checks, overdue)}"
            ),
        }

    pending = sorted(grouped["running"] + not_started)
    if pending:
        if deadline_expired:
            return {
                "decision": "escalate",
                "reason": "timeout",
                "checks": pending,
                "detail": (
                    "these checks had not finished when the wait ran out of time: "
                    f"{describe_checks(checks, pending)}"
                ),
            }
        return {
            "decision": "waiting",
            "reason": "checks_running",
            "checks": pending,
            "detail": f"waiting for {len(pending)} check(s) to finish",
        }

    if not checks:
        return {
            "decision": "no_checks",
            "reason": "no_applicable_checks",
            "checks": [],
            "detail": (
                "the pull request head reports no status checks at all, so this "
                "repository runs no checks on it"
            ),
        }

    failed = sorted(grouped["failed"])
    if failed:
        return {
            "decision": "failures",
            "reason": "checks_failed",
            "checks": failed,
            "detail": f"these checks failed: {describe_checks(checks, failed)}",
        }

    return {
        "decision": "green",
        "reason": "all_checks_passed",
        "checks": [],
        "detail": f"all {len(checks)} check(s) finished without a failure",
    }


def approval_blocked_runs(payload: Any) -> list[dict[str, Any]]:
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        return []
    blocked = []
    for entry in runs:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").upper()
        conclusion = str(entry.get("conclusion") or "").upper()
        if status in APPROVAL_RUN_STATES or conclusion == "ACTION_REQUIRED":
            blocked.append(
                {
                    "id": entry.get("id"),
                    "name": entry.get("name"),
                    "status": entry.get("status"),
                    "conclusion": entry.get("conclusion"),
                    "url": entry.get("html_url"),
                }
            )
    return blocked


def fetch_workflow_runs(pr: dict[str, Any], head_sha: str) -> Any:
    return gh_json(
        [
            "api",
            f"repos/{pr['upstream_owner']}/{pr['upstream_repo']}/actions/runs"
            f"?head_sha={head_sha}&per_page=100",
        ]
    )


def fetch_rollup(pr: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    payload = gh_json(
        [
            "pr",
            "view",
            pr["pr_url"],
            "--repo",
            pr["repo_name"],
            "--json",
            "headRefOid,statusCheckRollup",
        ]
    )
    if not isinstance(payload, dict):
        raise WorkflowError("gh pr view did not return the status check rollup")
    head_sha = payload.get("headRefOid")
    if not isinstance(head_sha, str) or not head_sha:
        raise WorkflowError("status check rollup response has no head commit")
    return head_sha, normalize_rollup(payload.get("statusCheckRollup"))


def baseline_conclusions(pr: dict[str, Any], base_sha: str) -> dict[str, str]:
    """Read how the same checks concluded on the base branch commit.

    This is the evidence that stops the loop from editing the pull request to
    paper over a breakage the base branch already has.
    """
    owner = pr["upstream_owner"]
    repo = pr["upstream_repo"]
    results: dict[str, str] = {}
    check_runs = gh_json(
        ["api", f"repos/{owner}/{repo}/commits/{base_sha}/check-runs?per_page=100"]
    )
    entries = check_runs.get("check_runs") if isinstance(check_runs, dict) else None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        status = str(entry.get("status") or "").upper()
        conclusion = str(entry.get("conclusion") or "").upper()
        results[name] = conclusion if status == "COMPLETED" and conclusion else status
    statuses = gh_json(
        ["api", f"repos/{owner}/{repo}/commits/{base_sha}/status?per_page=100"]
    )
    contexts = statuses.get("statuses") if isinstance(statuses, dict) else None
    for entry in contexts or []:
        if not isinstance(entry, dict):
            continue
        context = entry.get("context")
        if not isinstance(context, str) or not context:
            continue
        results.setdefault(context, str(entry.get("state") or "").upper())
    return results


def baseline_verdict(conclusion: Any) -> str:
    """Turn one base-branch conclusion into the verdict the evidence supports."""
    if not isinstance(conclusion, str) or not conclusion:
        return "unknown"
    value = conclusion.upper()
    if value in FAILED_BASELINE_CONCLUSIONS:
        return "pre_existing"
    if value in PASSED_BASELINE_CONCLUSIONS:
        return "pr_caused"
    return "unknown"


def allowed_verdicts(baseline: str) -> tuple[str, ...]:
    """Say which verdicts the base-branch evidence still leaves open.

    A check that already fails on the base commit is pre-existing, whatever the
    diff looks like. A check that passes there was not broken before this pull
    request, so the only open question is whether the pull request broke it or the
    check is flaky.
    """
    if baseline == "pre_existing":
        return ("pre_existing",)
    if baseline == "pr_caused":
        return ("pr_caused", "flake")
    return VERDICTS


def attribute_failures(
    checks: list[dict[str, Any]],
    baseline: dict[str, str],
    previous: Any = None,
) -> dict[str, dict[str, Any]]:
    """Attribute every failing check, keeping model verdicts the evidence allows."""
    previous = previous if isinstance(previous, dict) else {}
    attributions: dict[str, dict[str, Any]] = {}
    for check in checks:
        if check["class"] != "failed":
            continue
        conclusion = baseline.get(check["name"])
        from_baseline = baseline_verdict(conclusion)
        entry = {
            "key": check["key"],
            "name": check["name"],
            "verdict": from_baseline,
            "source": "baseline" if from_baseline != "unknown" else "unattributed",
            "baseline_conclusion": conclusion,
            "baseline_verdict": from_baseline,
            "rationale": None,
        }
        earlier = previous.get(check["key"])
        if (
            isinstance(earlier, dict)
            and earlier.get("source") == "model"
            and earlier.get("verdict") in allowed_verdicts(from_baseline)
        ):
            entry.update(
                {
                    "verdict": earlier["verdict"],
                    "source": "model",
                    "rationale": earlier.get("rationale"),
                }
            )
        attributions[check["key"]] = entry
    return attributions


def rerun_count(state: dict[str, Any], key: str) -> int:
    reruns = state.get("reruns")
    entry = reruns.get(key) if isinstance(reruns, dict) else None
    count = entry.get("count") if isinstance(entry, dict) else None
    return int(count) if isinstance(count, int) else 0


def handled_checks(state: dict[str, Any]) -> set[str]:
    run_state = state.get("run") or {}
    handled: set[str] = set()
    for batch in run_state.get("batches") or []:
        if batch.get("status") == "recorded":
            handled.update(batch.get("check_keys") or [])
    return handled


def next_action(state: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """Say what the loop must do next, given one decision and the recorded state.

    Every branch here is a decision the agent must not make for itself. A failure
    with no verdict has to be attributed, a flake gets exactly one re-run, and a
    failure the base branch already has escalates rather than being fixed.
    """
    if decision["decision"] != "failures":
        return {
            "action": decision["decision"],
            "reason": decision["reason"],
            "checks": decision["checks"],
            "detail": decision["detail"],
        }

    run_state = state.get("run") or {}
    attributions = run_state.get("attributions") or {}
    failing = list(decision["checks"])
    already_handled = handled_checks(state)

    unattributed = sorted(
        key
        for key in failing
        if str((attributions.get(key) or {}).get("verdict") or "unknown") == "unknown"
    )
    if unattributed:
        return {
            "action": "attribute",
            "reason": "unattributed_failures",
            "checks": unattributed,
            "detail": (
                "the base branch evidence does not settle these failures, so each one "
                "needs a verdict before this loop may touch it"
            ),
        }

    flakes = sorted(
        key
        for key in failing
        if attributions[key]["verdict"] == "flake"
        and rerun_count(state, key) < MAX_RERUNS_PER_CHECK
    )
    if flakes:
        return {
            "action": "rerun",
            "reason": "suspected_flake",
            "checks": flakes,
            "detail": "re-run each suspected flake exactly once",
        }

    exhausted = sorted(
        key
        for key in failing
        if attributions[key]["verdict"] == "flake"
        and rerun_count(state, key) >= MAX_RERUNS_PER_CHECK
    )
    if exhausted:
        return {
            "action": "escalate",
            "reason": "flake_failed_twice",
            "checks": exhausted,
            "detail": (
                "these checks failed again after their one automatic re-run, so they "
                "are not flakes"
            ),
        }

    fixable = sorted(
        key
        for key in failing
        if attributions[key]["verdict"] == "pr_caused" and key not in already_handled
    )
    if fixable:
        return {
            "action": "fix",
            "reason": "pr_caused_failures",
            "checks": fixable,
            "detail": "this pull request plausibly caused these failures",
        }

    pre_existing = sorted(
        key for key in failing if attributions[key]["verdict"] == "pre_existing"
    )
    if pre_existing:
        return {
            "action": "escalate",
            "reason": "pre_existing_failures",
            "checks": pre_existing,
            "detail": (
                "these checks already fail on the base branch, so this loop must not "
                "edit the pull request to hide them"
            ),
        }

    return {
        "action": "escalate",
        "reason": "unfixable_failure",
        "checks": failing,
        "detail": (
            "every failing check is already recorded as handled, yet it still fails at "
            "this head"
        ),
    }


def charge_iteration(state: dict[str, Any], run_state: dict[str, Any]) -> bool:
    """Spend an iteration on the current run, once, when it has real work to do.

    A launch that reads the checks and finds nothing to fix costs nothing. Only a
    run that reaches attribution, a re-run, or a fix spends one, so relaunching the
    loop at a head whose checks already passed can never exhaust the cap.
    """
    if run_state.get("charged"):
        return False
    run_state["charged"] = True
    state["iterations"] = int(state.get("iterations", 0)) + 1
    return True


def parse_run_reference(url: Any) -> dict[str, int] | None:
    """Pull the Actions run and job identifiers out of a check's details URL."""
    if not isinstance(url, str) or not url:
        return None
    reference: dict[str, int] = {}
    run_match = RUN_URL_PATTERN.search(url)
    if run_match:
        reference["run_id"] = int(run_match.group("run"))
        job_match = JOB_URL_PATTERN.search(url[run_match.end() :])
        if job_match:
            reference["job_id"] = int(job_match.group("job"))
        return reference
    legacy = LEGACY_JOB_URL_PATTERN.search(url)
    if legacy and "/actions/" not in url:
        return {"job_id": int(legacy.group("job"))}
    return None


def resolve_run_id(pr: dict[str, Any], reference: dict[str, int]) -> int:
    if "run_id" in reference:
        return reference["run_id"]
    job = gh_json(
        [
            "api",
            f"repos/{pr['upstream_owner']}/{pr['upstream_repo']}/actions/jobs/"
            f"{reference['job_id']}",
        ]
    )
    run_id = job.get("run_id") if isinstance(job, dict) else None
    if not isinstance(run_id, int):
        raise WorkflowError(
            f"could not resolve the workflow run for job {reference['job_id']}"
        )
    return run_id


def rerun_failed_jobs(pr: dict[str, Any], run_id: int) -> None:
    run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{pr['upstream_owner']}/{pr['upstream_repo']}/actions/runs/"
            f"{run_id}/rerun-failed-jobs",
        ]
    )


def active_run(state: dict[str, Any]) -> dict[str, Any]:
    run_state = state.get("run")
    if not run_state:
        raise WorkflowError("state has no iteration; run preflight first")
    if run_state.get("status") == "published":
        raise WorkflowError(
            "this iteration is already published; run preflight to start the next one"
        )
    return run_state


def find_batch(run_state: dict[str, Any], batch_id: str) -> dict[str, Any]:
    for batch in run_state.get("batches") or []:
        if batch["id"] == batch_id:
            return batch
    raise WorkflowError(f"batch is not planned: {batch_id}")


def require_known_checks(run_state: dict[str, Any], keys: Iterable[str]) -> None:
    known = {check["key"] for check in run_state.get("checks") or []}
    missing = sorted(set(keys) - known)
    if missing:
        raise WorkflowError(
            f"these checks are not in this iteration's snapshot: {', '.join(missing)}"
        )


def archive_run(state: dict[str, Any]) -> None:
    """Fold a finished iteration into the carried-forward history.

    Only settled records are archived. An iteration an interrupted run never
    finished stays out, so a later iteration can decide it again.
    """
    run_state = state.get("run")
    if not run_state:
        return
    history = state.setdefault("history", [])
    recorded = {entry["id"] for entry in history}
    for batch in run_state.get("batches") or []:
        identifier = f"{run_state.get('iteration')}:{batch['id']}"
        if identifier in recorded or batch.get("status") not in {"recorded", "skipped"}:
            continue
        history.append(
            {
                "id": identifier,
                "iteration": run_state.get("iteration"),
                "batch": batch["id"],
                "label": batch.get("label"),
                "check_keys": batch.get("check_keys") or [],
                "check_names": batch.get("check_names") or [],
                "outcome": "addressed" if batch.get("commit") else batch["status"],
                "detail": batch.get("rationale") or batch.get("summary"),
                "commit": batch.get("commit"),
                "head_sha": run_state.get("head_sha"),
            }
        )
    for key, entry in (run_state.get("attributions") or {}).items():
        identifier = f"{run_state.get('iteration')}:verdict:{key}"
        if identifier in recorded or entry.get("verdict") == "unknown":
            continue
        history.append(
            {
                "id": identifier,
                "iteration": run_state.get("iteration"),
                "check_key": key,
                "check_names": [entry.get("name")],
                "outcome": f"verdict_{entry['verdict']}",
                "detail": entry.get("rationale") or entry.get("baseline_conclusion"),
                "commit": None,
                "head_sha": run_state.get("head_sha"),
            }
        )


def command_preflight(args: argparse.Namespace) -> None:
    require_tools()
    repo_root = resolve_repo_root(args.repo_root)
    target = resolve_target(args.target, repo_root)
    state_path = cli_path(args.state) if args.state else default_state_path(target)
    state = load_state(state_path) if state_path.is_file() else None

    dirty = git(repo_root, "status", "--porcelain=v1")
    if dirty:
        raise WorkflowError(f"worktree is not clean:\n{dirty}")

    metadata = metadata_for(target)
    checked_out_branch = checkout_pr(repo_root, target, metadata)
    branch = git(repo_root, "branch", "--show-current")
    if checked_out_branch and branch != metadata["head_branch"]:
        raise WorkflowError(
            f"branch mismatch: local {branch!r}, PR head {metadata['head_branch']!r}"
        )
    require_checkout_head(git(repo_root, "rev-parse", "HEAD"), metadata["head_sha"])

    diff_text = fetch_authoritative_diff(metadata)
    changed_files = changed_files_for(metadata)
    refreshed = metadata_for(target)
    if refreshed["head_sha"] != metadata["head_sha"]:
        raise WorkflowError(
            "PR head changed while the authoritative diff was fetched: expected "
            f"{metadata['head_sha']}, got {refreshed['head_sha']}"
        )
    pr_commits = commit_provenance(repo_root, metadata["commits"])

    if state is None:
        state = {
            "version": STATE_VERSION,
            "created_at": utc_now(),
            "iterations": 0,
            "history": [],
            "reruns": {},
            "escalation": None,
        }
    archive_run(state)
    state["iterations"] = int(state.get("iterations", 0))
    previous_run = state.get("run") or {}
    previous_head = previous_run.get("head_sha")
    if previous_head and previous_head != metadata["head_sha"]:
        # A new head invalidates every re-run this loop spent on the old one.
        state["reruns"] = {}
    max_iterations = getattr(args, "max_iterations", DEFAULT_MAX_ITERATIONS)
    iteration = state["iterations"] + 1
    result = (
        "max_iterations_reached" if state["iterations"] >= max_iterations else "ready"
    )
    diff_path = diff_path_for(state_path)
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(diff_text, encoding="utf-8", newline="")
    state.update(
        {
            "repo_root": str(repo_root),
            "pr": metadata,
            "run": {
                "id": f"pr-{metadata['number']}-iteration-{iteration}",
                "status": "active",
                "iteration": iteration,
                "head_sha": metadata["head_sha"],
                "base_sha": metadata["base_sha"],
                "diff_path": str(diff_path),
                "changed_files": changed_files,
                "pr_commits": pr_commits,
                "checks": [],
                "attributions": {},
                "batches": [],
                "tracking": {},
                "decision": None,
                "charged": False,
            },
        }
    )
    # A relaunch reads the checks again from GitHub, which is the only thing that
    # states whether they pass and the only thing that may retract it. Drop the
    # outcome the previous run recorded so nothing reports a stale clearance.
    state["outcome"] = None
    state["clean_at_head_sha"] = None
    if result == "max_iterations_reached":
        state["escalation"] = {
            "reason": "max_iterations_reached",
            "detail": (
                f"this loop already ran {state['iterations']} iteration(s), which is "
                f"its cap of {max_iterations}"
            ),
            "checks": [],
            "next_action": ESCALATION_ACTIONS["max_iterations_reached"],
            "head_sha": metadata["head_sha"],
            "recorded_at": utc_now(),
        }
    else:
        state["escalation"] = None
    save_state(state_path, state)

    preflight_path = preflight_path_for(state_path)
    payload = {
        "result": result,
        "state": str(state_path),
        "repo_root": str(repo_root),
        "pr": metadata,
        "head_sha": metadata["head_sha"],
        "base_sha": metadata["base_sha"],
        "diff_path": str(diff_path),
        "changed_files": changed_files,
        "pr_commits": pr_commits,
        "history": state["history"],
        "escalation": state.get("escalation"),
        "iteration": iteration,
        "max_iterations": max_iterations,
    }
    write_result_file(preflight_path, payload, "preflight")
    emit(
        {
            "result": result,
            "state": str(state_path),
            "preflight_path": str(preflight_path),
            "repo_root": str(repo_root),
            "pr": {
                "number": metadata["number"],
                "title": metadata["title"],
                "pr_url": metadata["pr_url"],
                "repo_name": metadata["repo_name"],
                "head_branch": metadata["head_branch"],
                "base_branch": metadata["base_branch"],
                "is_fork": metadata["is_fork"],
                "is_draft": metadata["is_draft"],
            },
            "head_sha": metadata["head_sha"],
            "base_sha": metadata["base_sha"],
            "diff_path": str(diff_path),
            "diff_bytes": len(diff_text.encode("utf-8")),
            "counts": {
                "changed_files": len(changed_files),
                "history": len(state["history"]),
                "pr_commits": len(pr_commits),
            },
            "iteration": iteration,
            "max_iterations": max_iterations,
        }
    )


def snapshot_checks(
    state: dict[str, Any],
    *,
    now: dt.datetime,
    not_started_grace: int,
    deadline_expired: bool,
) -> dict[str, Any]:
    """Read the live rollup once and turn it into a decision and a next action."""
    pr = state["pr"]
    run_state = active_run(state)
    pinned = run_state["head_sha"]
    live_head, checks = fetch_rollup(pr)
    if live_head != pinned:
        return {
            "head_sha": live_head,
            "checks": checks,
            "decision": {
                "decision": "escalate",
                "reason": "head_changed",
                "checks": [],
                "detail": (
                    f"the PR head moved from {pinned} to {live_head} while this "
                    "iteration was reading its checks"
                ),
            },
            "action": {
                "action": "escalate",
                "reason": "head_changed",
                "checks": [],
                "detail": (
                    f"the PR head moved from {pinned} to {live_head} while this "
                    "iteration was reading its checks"
                ),
            },
            "attributions": run_state.get("attributions") or {},
            "tracking": run_state.get("tracking") or {},
        }

    tracking = update_check_tracking(run_state.get("tracking"), checks, now)
    approval_runs: list[dict[str, Any]] = []
    if not checks:
        approval_runs = approval_blocked_runs(fetch_workflow_runs(pr, pinned))
    decision = decide(
        checks,
        now=now,
        tracking=tracking,
        not_started_grace=not_started_grace,
        deadline_expired=deadline_expired,
        approval_runs=approval_runs,
    )
    attributions = run_state.get("attributions") or {}
    if decision["decision"] == "failures":
        attributions = attribute_failures(
            checks,
            baseline_conclusions(pr, run_state["base_sha"]),
            attributions,
        )
    return {
        "head_sha": live_head,
        "checks": checks,
        "decision": decision,
        "action": next_action(
            {**state, "run": {**run_state, "attributions": attributions}}, decision
        ),
        "attributions": attributions,
        "tracking": tracking,
        "approval_runs": approval_runs,
    }


def command_checks(args: argparse.Namespace) -> None:
    require_tools()
    path = cli_path(args.state)
    state = load_state(path)
    run_state = active_run(state)
    deadline = time.monotonic() + max(0, args.timeout)
    while True:
        now = dt.datetime.now(dt.timezone.utc)
        expired = time.monotonic() >= deadline
        snapshot = snapshot_checks(
            state,
            now=now,
            not_started_grace=args.not_started_grace,
            deadline_expired=expired,
        )
        if snapshot["decision"]["decision"] != "waiting":
            break
        if not args.wait:
            break
        time.sleep(max(1, args.interval))

    decision = snapshot["decision"]
    action = snapshot["action"]
    run_state["checks"] = snapshot["checks"]
    run_state["attributions"] = snapshot["attributions"]
    run_state["tracking"] = snapshot["tracking"]
    run_state["decision"] = {
        **decision,
        "action": action["action"],
        "observed_at": utc_now(),
    }
    if action["action"] in WORKING_ACTIONS:
        charge_iteration(state, run_state)
    if action["action"] == "escalate":
        state["escalation"] = {
            "reason": action["reason"],
            "detail": action["detail"],
            "checks": action["checks"],
            "next_action": ESCALATION_ACTIONS.get(action["reason"], ""),
            "head_sha": run_state["head_sha"],
            "recorded_at": utc_now(),
        }
    save_state(path, state)

    checks_path = checks_path_for(path)
    payload = {
        "result": action["action"],
        "state": str(path),
        "pr": state["pr"],
        "head_sha": run_state["head_sha"],
        "base_sha": run_state["base_sha"],
        "decision": decision,
        "action": action,
        "checks": snapshot["checks"],
        "attributions": snapshot["attributions"],
        "approval_runs": snapshot.get("approval_runs") or [],
        "escalation": state.get("escalation"),
        "iteration": run_state["iteration"],
    }
    write_result_file(checks_path, payload, "checks")
    failing = [check for check in snapshot["checks"] if check["class"] == "failed"]
    emit(
        {
            "result": action["action"],
            "state": str(path),
            "checks_path": str(checks_path),
            "head_sha": run_state["head_sha"],
            "decision": decision["decision"],
            "reason": action["reason"],
            "detail": action["detail"],
            "action_checks": action["checks"],
            "counts": {
                "total": len(snapshot["checks"]),
                **class_counts(snapshot["checks"]),
            },
            "failing": [
                {
                    "key": check["key"],
                    "name": check["name"],
                    "url": check["url"],
                    "verdict": (snapshot["attributions"].get(check["key"]) or {}).get(
                        "verdict"
                    ),
                    "reruns": rerun_count(state, check["key"]),
                }
                for check in failing
            ],
            "next_action": ESCALATION_ACTIONS.get(action["reason"], "")
            if action["action"] == "escalate"
            else "",
            "iteration": run_state["iteration"],
        }
    )


def command_attribute(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    run_state = active_run(state)
    attributions = run_state.get("attributions") or {}
    entry = attributions.get(args.check)
    if entry is None:
        known = ", ".join(sorted(attributions)) or "none"
        raise WorkflowError(
            f"no failing check is recorded under {args.check}; failing checks: {known}"
        )
    rationale = (
        load_text_input(args.rationale_file, "attribution rationale")
        if args.rationale_file
        else args.rationale.strip()
    )
    if not rationale:
        raise WorkflowError("attribution rationale must not be empty")
    permitted = allowed_verdicts(entry.get("baseline_verdict") or "unknown")
    if args.verdict not in permitted:
        raise WorkflowError(
            f"the base branch evidence does not allow the verdict {args.verdict!r} for "
            f"{entry['name']}: it concluded {entry.get('baseline_conclusion')!r} on the "
            f"base commit, so the only allowed verdict(s) are {', '.join(permitted)}"
        )
    entry.update(
        {"verdict": args.verdict, "source": "model", "rationale": rationale}
    )
    save_state(path, state)
    emit(
        {
            "result": "attributed",
            "state": str(path),
            "check": args.check,
            "name": entry["name"],
            "verdict": args.verdict,
            "baseline_conclusion": entry.get("baseline_conclusion"),
            "rationale": rationale,
        }
    )


def command_rerun(args: argparse.Namespace) -> None:
    require_tools()
    path = cli_path(args.state)
    state = load_state(path)
    run_state = active_run(state)
    attributions = run_state.get("attributions") or {}
    entry = attributions.get(args.check)
    if entry is None:
        raise WorkflowError(f"no failing check is recorded under {args.check}")
    if entry.get("verdict") != "flake":
        raise WorkflowError(
            f"only a check attributed as a flake may be re-run; {entry['name']} is "
            f"attributed {entry.get('verdict')!r}"
        )
    already = rerun_count(state, args.check)
    if already >= MAX_RERUNS_PER_CHECK:
        raise WorkflowError(
            f"{entry['name']} already used its one automatic re-run and failed again; "
            "record an escalation with reason flake_failed_twice instead"
        )
    check = next(
        (item for item in run_state.get("checks") or [] if item["key"] == args.check),
        None,
    )
    if check is None:
        raise WorkflowError(f"check {args.check} is not in this iteration's snapshot")
    reference = parse_run_reference(check.get("url"))
    if reference is None:
        state["escalation"] = {
            "reason": "no_rerun_support",
            "detail": (
                f"{entry['name']} reports no GitHub Actions run, so this loop cannot "
                "re-run it"
            ),
            "checks": [args.check],
            "next_action": ESCALATION_ACTIONS["no_rerun_support"],
            "head_sha": run_state["head_sha"],
            "recorded_at": utc_now(),
        }
        save_state(path, state)
        emit(
            {
                "result": "no_rerun_support",
                "state": str(path),
                "check": args.check,
                "name": entry["name"],
                "url": check.get("url"),
                "next_action": ESCALATION_ACTIONS["no_rerun_support"],
            }
        )
        return
    run_id = resolve_run_id(state["pr"], reference)
    rerun_failed_jobs(state["pr"], run_id)
    reruns = state.setdefault("reruns", {})
    reruns[args.check] = {
        "count": already + 1,
        "name": entry["name"],
        "run_id": run_id,
        "head_sha": run_state["head_sha"],
        "requested_at": utc_now(),
    }
    save_state(path, state)
    emit(
        {
            "result": "rerun_requested",
            "state": str(path),
            "check": args.check,
            "name": entry["name"],
            "run_id": run_id,
            "reruns": already + 1,
            "max_reruns": MAX_RERUNS_PER_CHECK,
        }
    )


def command_plan(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    run_state = active_run(state)
    require_known_checks(run_state, args.checks)
    attributions = run_state.get("attributions") or {}
    blocked = []
    for key in args.checks:
        entry = attributions.get(key) or {}
        verdict = entry.get("verdict", "unknown")
        if verdict != "pr_caused":
            blocked.append({"check": key, "name": entry.get("name"), "verdict": verdict})
    if blocked:
        raise WorkflowError(
            "only a failure attributed pr_caused may be fixed by editing this pull "
            f"request: {json.dumps(blocked, sort_keys=True)}"
        )
    names = [attributions[key]["name"] for key in args.checks]
    batch = {
        "id": args.batch,
        "label": args.label,
        "check_keys": list(args.checks),
        "check_names": names,
        "paths": args.paths or [],
        "validation": args.validation,
        "status": "planned",
        "commit": None,
        "summary": None,
        "rationale": None,
    }
    run_state["batches"] = [
        item for item in run_state.get("batches") or [] if item["id"] != args.batch
    ]
    run_state["batches"].append(batch)
    save_state(path, state)
    emit({"result": "planned", "state": str(path), "batch": batch})


def command_record(args: argparse.Namespace) -> None:
    if not args.commit and not args.rationale:
        raise WorkflowError("record requires either --commit or --rationale")
    path = cli_path(args.state)
    state = load_state(path)
    run_state = active_run(state)
    batch = find_batch(run_state, args.batch)
    commit = args.commit
    if commit:
        commit = git(Path(state["repo_root"]), "rev-parse", commit)
    batch.update(
        {
            "status": "recorded",
            "commit": commit,
            "summary": args.summary,
            "rationale": args.rationale,
        }
    )
    save_state(path, state)
    emit(
        {
            "result": "recorded",
            "state": str(path),
            "batch": args.batch,
            "check_keys": batch["check_keys"],
            "commit": commit,
            "rationale": args.rationale,
        }
    )


def command_skip(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    run_state = active_run(state)
    batch = find_batch(run_state, args.batch)
    batch.update({"status": "skipped", "rationale": args.rationale, "commit": None})
    state["escalation"] = {
        "reason": "unfixable_failure",
        "detail": args.rationale,
        "checks": batch["check_keys"],
        "next_action": ESCALATION_ACTIONS["unfixable_failure"],
        "head_sha": run_state["head_sha"],
        "recorded_at": utc_now(),
    }
    save_state(path, state)
    emit(
        {
            "result": "skipped",
            "state": str(path),
            "batch": args.batch,
            "check_keys": batch["check_keys"],
            "rationale": args.rationale,
            "next_action": ESCALATION_ACTIONS["unfixable_failure"],
        }
    )


def command_escalate(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    run_state = state.get("run") or {}
    detail = (
        load_text_input(args.detail_file, "escalation detail")
        if args.detail_file
        else args.detail.strip()
    )
    if not detail:
        raise WorkflowError("escalation detail must not be empty")
    escalation = {
        "reason": args.reason,
        "detail": detail,
        "checks": list(args.checks or []),
        "next_action": ESCALATION_ACTIONS.get(args.reason, ""),
        "head_sha": run_state.get("head_sha"),
        "recorded_at": utc_now(),
    }
    state["escalation"] = escalation
    save_state(path, state)
    emit({"result": "escalated", "state": str(path), **escalation})


def command_resolve(args: argparse.Namespace) -> None:
    require_tools()
    path = cli_path(args.state)
    state = load_state(path)
    run_state = active_run(state)
    pinned = run_state["head_sha"]
    now = dt.datetime.now(dt.timezone.utc)
    live_head, checks = fetch_rollup(state["pr"])
    if live_head != pinned:
        raise WorkflowError(
            f"PR head changed before this outcome was recorded: expected {pinned}, "
            f"got {live_head}"
        )
    approval_runs = (
        approval_blocked_runs(fetch_workflow_runs(state["pr"], pinned))
        if not checks
        else []
    )
    decision = decide(
        checks,
        now=now,
        tracking=run_state.get("tracking"),
        not_started_grace=args.not_started_grace,
        deadline_expired=True,
        approval_runs=approval_runs,
    )
    if decision["decision"] != args.outcome:
        raise WorkflowError(
            f"the live checks report {decision['decision']!r}, not {args.outcome!r}: "
            f"{decision['detail']}"
        )
    run_state["checks"] = checks
    run_state["outcome"] = args.outcome
    run_state["clean_at_head_sha"] = pinned
    state["clean_at_head_sha"] = pinned
    state["outcome"] = args.outcome
    state["escalation"] = None
    note = (
        f"CI Fix Loop skipped {state['pr']['repo_name']}#{state['pr']['number']}: "
        "the pull request head reports no applicable checks, so this repository ran "
        "no CI on it."
        if args.outcome == "no_checks"
        else None
    )
    state["skip_note"] = note
    save_state(path, state)
    emit(
        {
            "result": "resolved",
            "state": str(path),
            "outcome": args.outcome,
            "clean_at_head_sha": pinned,
            "skip_note": note,
            "counts": {"total": len(checks), **class_counts(checks)},
        }
    )


def find_push_remote(repo_root: Path, owner: str, repo: str) -> str:
    expected = f"{owner}/{repo}".lower()
    for remote in git(repo_root, "remote").splitlines():
        url = git(repo_root, "remote", "get-url", "--push", remote)
        parsed = github_repo_from_remote(url)
        if parsed and parsed.lower() == expected:
            return remote
    raise WorkflowError(f"no git remote points to PR head repository {owner}/{repo}")


def remote_head(owner: str, repo: str, branch: str) -> str | None:
    process = run(
        ["gh", "api", f"repos/{owner}/{repo}/git/ref/heads/{branch}"], check=False
    )
    if process.returncode == 1 and "HTTP 404" in process.stderr:
        return None
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip()
        raise WorkflowError(f"failed to read remote ref: {detail}")
    return json.loads(process.stdout)["object"]["sha"]


def wait_for_remote_head(
    owner: str, repo: str, branch: str, expected_head: str
) -> str | None:
    actual_head = remote_head(owner, repo, branch)
    for delay in REMOTE_REF_LAG_RETRY_DELAYS:
        if actual_head == expected_head:
            break
        time.sleep(delay)
        actual_head = remote_head(owner, repo, branch)
    return actual_head


def require_fork_head(pr: dict[str, Any]) -> None:
    if pr.get("is_fork"):
        return
    branch = pr.get("head_branch")
    if not branch or remote_head(pr["head_owner"], pr["head_repo"], branch) is None:
        raise WorkflowError(
            f"head branch {branch!r} no longer exists in {pr['head_owner']}/"
            f"{pr['head_repo']}; refusing to create it by pushing"
        )


def command_publish(args: argparse.Namespace) -> None:
    require_tools()
    path = cli_path(args.state)
    state = load_state(path)
    run_state = active_run(state)
    repo_root = Path(state["repo_root"])
    dirty = git(repo_root, "status", "--porcelain=v1")
    if dirty:
        raise WorkflowError(f"worktree is not clean:\n{dirty}")

    batches = run_state.get("batches") or []
    planned = [batch["id"] for batch in batches if batch.get("status") == "planned"]
    if planned:
        raise WorkflowError(f"batches are neither recorded nor skipped: {planned}")
    skipped = [batch["id"] for batch in batches if batch.get("status") == "skipped"]
    if skipped:
        raise WorkflowError(
            f"a batch was skipped by an unrecoverable failure: {skipped}; this run "
            "must stop without publishing partial work"
        )
    incomplete = [
        batch["id"]
        for batch in batches
        if batch.get("status") == "recorded"
        and not batch.get("summary")
        and not batch.get("rationale")
    ]
    if incomplete:
        raise WorkflowError(f"recorded batches lack publish data: {incomplete}")

    commits: list[str] = []
    for batch in batches:
        commit = batch.get("commit")
        if commit and commit not in commits:
            commits.append(commit)

    pinned = run_state["head_sha"]
    local_head = git(repo_root, "rev-parse", "HEAD")
    new_commits = [
        line
        for line in git(repo_root, "rev-list", f"{pinned}..HEAD").splitlines()
        if line
    ]
    unrecorded = [commit for commit in new_commits if commit not in set(commits)]
    missing = [commit for commit in commits if commit not in set(new_commits)]
    if unrecorded or missing:
        raise WorkflowError(
            "local commits do not match this iteration's records: "
            f"unrecorded {unrecorded}, missing {missing}"
        )
    if not commits:
        emit(
            {
                "result": "nothing_to_publish",
                "state": str(path),
                "head_sha": local_head,
            }
        )
        return

    pr = state["pr"]
    require_fork_head(pr)
    remote = find_push_remote(repo_root, pr["head_owner"], pr["head_repo"])
    if remote_head(pr["head_owner"], pr["head_repo"], pr["head_branch"]) != local_head:
        run(["git", "-C", str(repo_root), "push", remote, f"HEAD:{pr['head_branch']}"])
    pushed_head = wait_for_remote_head(
        pr["head_owner"], pr["head_repo"], pr["head_branch"], local_head
    )
    if pushed_head != local_head:
        raise WorkflowError(
            f"head ref mismatch: local {local_head}, remote {pushed_head}"
        )
    pr_head = metadata_for(parse_target(pr["pr_url"]))["head_sha"]
    if pr_head != local_head:
        time.sleep(PR_HEAD_LAG_RETRY_DELAY)
        pr_head = metadata_for(parse_target(pr["pr_url"]))["head_sha"]
    if pr_head != local_head:
        raise WorkflowError(f"PR head mismatch: local {local_head}, PR head {pr_head}")

    run_state["status"] = "published"
    run_state["published_head_sha"] = local_head
    # The published head is new, so nothing this loop learned about the old head's
    # checks still applies.
    state["reruns"] = {}
    state["clean_at_head_sha"] = None
    archive_run(state)
    save_state(path, state)
    emit(
        {
            "result": "published",
            "state": str(path),
            "head_sha": local_head,
            "commits": commits,
            "iterations": state["iterations"],
        }
    )


def stage_outcome(state: dict[str, Any]) -> str:
    """Name this run's ending in the vocabulary an orchestrator records.

    A pipeline reads greenness from GitHub rather than from here, so this states
    only how the loop itself ended: `skipped` when the head ran no applicable
    checks, `no_progress` when it neither cleared nor escalated nor moved the head.
    """
    if state.get("escalation"):
        return "escalated"
    outcome = state.get("outcome")
    if outcome == "no_checks":
        return "skipped"
    if outcome == "green":
        return "cleared"
    return "no_progress"


def status_payload(state: dict[str, Any], path: Path) -> dict[str, Any]:
    pr = state["pr"]
    run_state = state.get("run") or {}
    return {
        "result": "ready",
        "state": str(path),
        "pr": pr,
        "run": run_state,
        "history": state.get("history") or [],
        "reruns": state.get("reruns") or {},
        "escalation": state.get("escalation"),
        "outcome": state.get("outcome"),
        "stage_outcome": stage_outcome(state),
        "clean_at_head_sha": state.get("clean_at_head_sha"),
        "skip_note": state.get("skip_note"),
        "iterations": int(state.get("iterations", 0)),
    }


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
                    "run": None,
                    "escalation": None,
                    "outcome": None,
                    "stage_outcome": "no_progress",
                    "clean_at_head_sha": None,
                    "history": [],
                }
            )
            return
    else:
        path = cli_path(args.state)
    state = load_state(path)
    payload = status_payload(state, path)
    status_path = status_path_for(path)
    write_result_file(status_path, payload, "status")
    pr = state["pr"]
    run_state = state.get("run") or {}
    checks = run_state.get("checks") or []
    decision = run_state.get("decision") or {}
    emit(
        {
            "result": "ready",
            "state": str(path),
            "status_path": str(status_path),
            "pr": {
                "number": pr["number"],
                "title": pr["title"],
                "pr_url": pr["pr_url"],
                "repo_name": pr["repo_name"],
                "head_branch": pr["head_branch"],
                "base_branch": pr["base_branch"],
            },
            "run": {
                "id": run_state.get("id"),
                "status": run_state.get("status"),
                "iteration": run_state.get("iteration"),
                "head_sha": run_state.get("head_sha"),
                "decision": decision.get("decision"),
                "action": decision.get("action"),
                "reason": decision.get("reason"),
                "outcome": run_state.get("outcome"),
                "batch_statuses": count_by_status(run_state.get("batches")),
            },
            "outcome": state.get("outcome"),
            "stage_outcome": stage_outcome(state),
            "clean_at_head_sha": state.get("clean_at_head_sha"),
            "skip_note": state.get("skip_note"),
            "escalation": state.get("escalation"),
            "verdicts": {
                key: entry.get("verdict")
                for key, entry in (run_state.get("attributions") or {}).items()
            },
            "counts": {
                "batches": len(run_state.get("batches") or []),
                "changed_files": len(run_state.get("changed_files") or []),
                "checks": len(checks),
                "history": len(state.get("history") or []),
                "reruns": len(state.get("reruns") or {}),
                **class_counts(checks),
            },
            "iterations": int(state.get("iterations", 0)),
        }
    )


def command_cleanup(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    load_state(path)
    path.unlink()
    diff_path_for(path).unlink(missing_ok=True)
    preflight_path_for(path).unlink(missing_ok=True)
    checks_path_for(path).unlink(missing_ok=True)
    status_path_for(path).unlink(missing_ok=True)
    emit({"result": "cleaned_up", "state": str(path)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser(
        "preflight",
        help="verify and check out a PR, then pin the head its checks ran on",
    )
    preflight.add_argument(
        "target",
        nargs="?",
        help="PR URL or owner/repo#number; omit to use the current branch's PR",
    )
    preflight.add_argument("--repo-root")
    preflight.add_argument("--state")
    preflight.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
    preflight.set_defaults(function=command_preflight)

    checks = subparsers.add_parser(
        "checks", help="read the live checks and decide what the loop does next"
    )
    checks.add_argument("--state", required=True)
    checks.add_argument(
        "--wait",
        action="store_true",
        help="poll until the checks finish or the timeout expires",
    )
    checks.add_argument("--interval", type=int, default=DEFAULT_POLL_INTERVAL)
    checks.add_argument("--timeout", type=int, default=DEFAULT_POLL_TIMEOUT)
    checks.add_argument(
        "--not-started-grace", type=int, default=DEFAULT_NOT_STARTED_GRACE
    )
    checks.set_defaults(function=command_checks)

    attribute = subparsers.add_parser(
        "attribute", help="record one failing check's verdict"
    )
    attribute.add_argument("--state", required=True)
    attribute.add_argument("--check", required=True)
    attribute.add_argument("--verdict", choices=list(VERDICTS), required=True)
    attribute_rationale = attribute.add_mutually_exclusive_group(required=True)
    attribute_rationale.add_argument("--rationale")
    attribute_rationale.add_argument(
        "--rationale-file", help="UTF-8 rationale file, or - for standard input"
    )
    attribute.set_defaults(function=command_attribute)

    rerun = subparsers.add_parser(
        "rerun", help="re-run one suspected flake, at most once per head"
    )
    rerun.add_argument("--state", required=True)
    rerun.add_argument("--check", required=True)
    rerun.set_defaults(function=command_rerun)

    plan = subparsers.add_parser("plan", help="record one planned fix batch")
    plan.add_argument("--state", required=True)
    plan.add_argument("--batch", required=True)
    plan.add_argument("--checks", nargs="+", required=True)
    plan.add_argument("--label", required=True)
    plan.add_argument("--paths", nargs="*")
    plan.add_argument("--validation")
    plan.set_defaults(function=command_plan)

    record = subparsers.add_parser("record", help="record a fixed batch")
    record.add_argument("--state", required=True)
    record.add_argument("--batch", required=True)
    record.add_argument("--summary", required=True)
    record.add_argument("--commit")
    record.add_argument("--rationale")
    record.set_defaults(function=command_record)

    skip = subparsers.add_parser("skip", help="record a batch this loop cannot fix")
    skip.add_argument("--state", required=True)
    skip.add_argument("--batch", required=True)
    skip.add_argument("--rationale", required=True)
    skip.set_defaults(function=command_skip)

    escalate = subparsers.add_parser(
        "escalate", help="record why this loop stopped without going green"
    )
    escalate.add_argument("--state", required=True)
    escalate.add_argument("--reason", choices=list(ESCALATION_REASONS), required=True)
    escalate.add_argument("--checks", nargs="*")
    escalate_detail = escalate.add_mutually_exclusive_group(required=True)
    escalate_detail.add_argument("--detail")
    escalate_detail.add_argument(
        "--detail-file", help="UTF-8 detail file, or - for standard input"
    )
    escalate.set_defaults(function=command_escalate)

    resolve = subparsers.add_parser(
        "resolve", help="record a green or no-checks outcome at the pinned head"
    )
    resolve.add_argument("--state", required=True)
    resolve.add_argument("--outcome", choices=["green", "no_checks"], required=True)
    resolve.add_argument(
        "--not-started-grace", type=int, default=DEFAULT_NOT_STARTED_GRACE
    )
    resolve.set_defaults(function=command_resolve)

    publish = subparsers.add_parser(
        "publish", help="push this iteration's commits and verify the new head"
    )
    publish.add_argument("--state", required=True)
    publish.set_defaults(function=command_publish)

    status = subparsers.add_parser("status", help="print compact workflow state")
    status_source = status.add_mutually_exclusive_group(required=True)
    status_source.add_argument("--state")
    status_source.add_argument("--current", action="store_true")
    status.add_argument("--repo-root")
    status.set_defaults(function=command_status)

    cleanup = subparsers.add_parser("cleanup", help="delete completed external state")
    cleanup.add_argument("--state", required=True)
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
