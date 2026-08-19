#!/usr/bin/env python3
"""Deterministic mechanics for the Conflict Fix Loop custom agent."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
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
NO_PROGRESS_LIMIT = 2
MERGEABILITY_RETRY_DELAYS = (2, 4, 8, 16)
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

CONFLICT_START = re.compile(r"^<{7}(?: |$)")
CONFLICT_ANCESTOR = re.compile(r"^\|{7}(?: |$)")
CONFLICT_SEPARATOR = re.compile(r"^={7}$")
CONFLICT_END = re.compile(r"^>{7}(?: |$)")

UNMERGED_CODES = {
    "DD": "both deleted",
    "AU": "added by us",
    "UD": "deleted by them",
    "UA": "added by them",
    "DU": "deleted by us",
    "AA": "both added",
    "UU": "both modified",
}
DELETION_CONFLICT_CODES = {"DD", "UD", "DU"}

STRATEGIES = ("auto", "merge", "rebase")
ESCALATION_KINDS = (
    "contradiction",
    "max_iterations",
    "no_progress",
    "unsafe_push",
    "unknown_mergeability",
    "validation",
    "other",
)


class WorkflowError(RuntimeError):
    pass


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )
    if check and process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "no output"
        raise WorkflowError(f"{' '.join(command)} failed ({process.returncode}): {detail}")
    return process


def git(repo_root: Path, *arguments: str) -> str:
    return run(["git", "-C", str(repo_root), *arguments]).stdout.strip()


def git_try(repo_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(repo_root), *arguments], check=False)


def git_bytes(repo_root: Path, *arguments: str) -> bytes | None:
    process = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return process.stdout if process.returncode == 0 else None


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
    return Path.home() / ".copilot" / "run" / "conflict-fix-loop" / name


def preflight_path_for(state_path: Path) -> Path:
    return state_path.parent / f"{state_path.name}.preflight.json"


def conflicts_path_for(state_path: Path) -> Path:
    return state_path.parent / f"{state_path.name}.conflicts.json"


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
        raise WorkflowError(f"could not write the {label} result file: {error}") from error


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
    if path_value == "-":
        text = sys.stdin.read()
    else:
        try:
            text = cli_path(path_value).read_text(encoding="utf-8")
        except OSError as error:
            raise WorkflowError(f"could not read the {label} file: {error}") from error
    text = text.strip()
    if not text:
        raise WorkflowError(f"{label} must not be empty")
    return text


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
    remote = git_try(repo_root, "config", "--get", f"branch.{branch}.remote")
    merge = git_try(repo_root, "config", "--get", f"branch.{branch}.merge")
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

    remote_url = git(repo_root, "remote", "get-url", remote_name)
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
        "number,title,url,state,isDraft,mergeable,mergeStateStatus,headRefName,"
        "headRefOid,headRepositoryOwner,headRepository,baseRefName,baseRefOid,commits"
    )
    metadata = gh_json(
        ["pr", "view", target["pr_url"], "--repo", target["repo_name"], "--json", fields]
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
    base_sha = metadata.get("baseRefOid")
    if not isinstance(base_sha, str) or not base_sha:
        raise WorkflowError("resolved PR metadata has no base commit")
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
    return {
        "number": target["number"],
        "title": title.strip(),
        "pr_url": resolved["pr_url"],
        "repo_name": resolved["repo_name"],
        "upstream_owner": resolved["owner"],
        "upstream_repo": resolved["repo"],
        "state": metadata.get("state"),
        "is_draft": bool(metadata.get("isDraft")),
        "mergeable": metadata.get("mergeable"),
        "merge_state_status": metadata.get("mergeStateStatus"),
        "head_owner": head_owner["login"],
        "head_repo": head_repository["name"],
        "head_branch": metadata["headRefName"],
        "head_sha": head_sha,
        "base_branch": metadata["baseRefName"],
        "base_sha": base_sha,
        "commits": commits,
    }


def require_open_pull_request(metadata: dict[str, Any]) -> None:
    state = metadata.get("state")
    if state != "OPEN":
        raise WorkflowError(
            f"pull request {metadata['pr_url']} is {str(state).lower()}; "
            "this loop only operates on an open pull request"
        )


def live_mergeability(
    target: dict[str, Any], *, delays: Iterable[float] = MERGEABILITY_RETRY_DELAYS
) -> dict[str, Any]:
    """Read mergeability live, waiting while GitHub is still computing it.

    GitHub computes the value lazily, so the first read of a freshly pushed head is
    routinely UNKNOWN. Reading it again is what triggers and then observes the
    computation.
    """
    metadata = metadata_for(target)
    for delay in delays:
        if metadata.get("mergeable") != "UNKNOWN":
            return metadata
        time.sleep(delay)
        metadata = metadata_for(target)
    return metadata


def classify_mergeability(metadata: dict[str, Any]) -> str:
    mergeable = metadata.get("mergeable")
    if mergeable == "MERGEABLE":
        return "mergeable"
    if mergeable == "CONFLICTING":
        return "conflicting"
    return "unknown"


def repository_merge_methods(repo_name: str) -> dict[str, bool]:
    payload = gh_json(["api", f"repos/{repo_name}"])
    if not isinstance(payload, dict):
        raise WorkflowError(f"could not read repository settings for {repo_name}")
    return {
        "allow_merge_commit": bool(payload.get("allow_merge_commit", True)),
        "allow_squash_merge": bool(payload.get("allow_squash_merge", True)),
        "allow_rebase_merge": bool(payload.get("allow_rebase_merge", True)),
    }


def list_open_pulls(repo_name: str, parameters: dict[str, str]) -> list[dict[str, Any]]:
    arguments = ["api", "--paginate", "--method", "GET", f"repos/{repo_name}/pulls"]
    for name, value in {"state": "open", **parameters}.items():
        arguments.extend(["-f", f"{name}={value}"])
    payload = gh_json(arguments)
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise WorkflowError(f"unexpected pull request listing for {repo_name}")
    return [item for item in payload if isinstance(item, dict)]


def summarize_pull(item: dict[str, Any]) -> dict[str, Any]:
    head = item.get("head") or {}
    base = item.get("base") or {}
    head_repo = head.get("repo") or {}
    return {
        "number": item.get("number"),
        "url": item.get("html_url"),
        "head_branch": head.get("ref"),
        "head_sha": head.get("sha"),
        "head_repo": head_repo.get("full_name"),
        "base_branch": base.get("ref"),
    }


def stack_relations(pr: dict[str, Any]) -> dict[str, Any]:
    """Find the open pull requests that stack on this branch or that it stacks on.

    A dependent's base is this pull request's head branch. Rewriting this branch
    orphans that dependent's history, and pushing this branch's commits into the
    branch below marks the pull request below merged and deletes its head branch.
    """
    upstream = f"{pr['upstream_owner']}/{pr['upstream_repo']}"
    dependents: dict[int, dict[str, Any]] = {}
    for item in list_open_pulls(upstream, {"base": pr["head_branch"]}):
        summary = summarize_pull(item)
        if summary["number"] != pr["number"]:
            dependents[summary["number"]] = summary

    stacked_on = None
    for item in list_open_pulls(
        upstream, {"head": f"{pr['upstream_owner']}:{pr['base_branch']}"}
    ):
        summary = summarize_pull(item)
        if summary["number"] != pr["number"]:
            stacked_on = summary
            break

    return {
        "dependents": [dependents[key] for key in sorted(dependents)],
        "stacked_on": stacked_on,
    }


def choose_strategy(
    requested: str,
    *,
    merge_methods: dict[str, bool],
    relations: dict[str, Any],
) -> dict[str, Any]:
    """Pick the integration strategy and report every guard that constrains it.

    A merge keeps the existing commits reachable, so it is the safe default. A
    rebase rewrites the branch, which is refused outright while another open pull
    request stacks on it.
    """
    if requested not in STRATEGIES:
        raise WorkflowError(f"strategy must be one of {', '.join(STRATEGIES)}")

    dependents = relations.get("dependents") or []
    rewrite_blockers = []
    if dependents:
        listed = ", ".join(f"#{item['number']}" for item in dependents)
        rewrite_blockers.append(
            "rewriting this branch would orphan the open pull requests stacked on it: "
            f"{listed}"
        )

    merge_blockers = []
    if (
        merge_methods.get("allow_rebase_merge")
        and not merge_methods.get("allow_merge_commit")
        and not merge_methods.get("allow_squash_merge")
    ):
        merge_blockers.append(
            "the repository allows only rebase merging, so a merge commit on the head "
            "branch would block the merge button"
        )

    if requested == "merge":
        return {
            "strategy": "merge",
            "requested": requested,
            "reason": "the caller asked for a merge",
            "warnings": merge_blockers,
            "rewrite_blockers": rewrite_blockers,
        }
    if requested == "rebase":
        if rewrite_blockers:
            raise WorkflowError(
                "refusing to rebase: " + "; ".join(rewrite_blockers)
            )
        return {
            "strategy": "rebase",
            "requested": requested,
            "reason": "the caller asked for a rebase",
            "warnings": [],
            "rewrite_blockers": rewrite_blockers,
        }

    if not merge_blockers:
        return {
            "strategy": "merge",
            "requested": requested,
            "reason": "a merge resolves the conflict without rewriting the branch",
            "warnings": [],
            "rewrite_blockers": rewrite_blockers,
        }
    if rewrite_blockers:
        raise WorkflowError(
            "no safe strategy is available: "
            + "; ".join(merge_blockers + rewrite_blockers)
        )
    return {
        "strategy": "rebase",
        "requested": requested,
        "reason": merge_blockers[0],
        "warnings": [],
        "rewrite_blockers": rewrite_blockers,
    }


def push_safety_blockers(pr: dict[str, Any], relations: dict[str, Any]) -> list[str]:
    """Report every reason this branch must not be pushed.

    The refspec this helper builds always names the head branch, so the checks
    here exist to catch a pull request whose own metadata makes that destination
    the same ref as something else.
    """
    blockers = []
    head_branch = pr.get("head_branch")
    base_branch = pr.get("base_branch")
    head_repo = f"{pr['head_owner']}/{pr['head_repo']}".lower()
    upstream_repo = f"{pr['upstream_owner']}/{pr['upstream_repo']}".lower()
    if not head_branch:
        blockers.append("the pull request has no head branch")
    if head_repo == upstream_repo and head_branch == base_branch:
        blockers.append(
            f"the head branch and the base branch are both {head_branch!r} in "
            f"{head_repo}, so a push would write to the base branch"
        )
    stacked_on = relations.get("stacked_on")
    if stacked_on and stacked_on.get("head_branch") == head_branch:
        blockers.append(
            f"the base branch belongs to open pull request #{stacked_on['number']} "
            "and resolves to this same head branch"
        )
    return blockers


def parse_status_z(output: str) -> list[dict[str, str]]:
    """Parse `git status --porcelain=v1 -z` into ordered code and path records."""
    fields = [field for field in output.split("\0")]
    while fields and fields[-1] == "":
        fields.pop()
    entries: list[dict[str, str]] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4 or record[2] != " ":
            raise WorkflowError(f"unparsable git status record: {record!r}")
        code = record[:2]
        path = record[3:]
        origin = None
        if "R" in code or "C" in code:
            if index >= len(fields):
                raise WorkflowError(f"git status record {record!r} has no origin path")
            origin = fields[index]
            index += 1
        entry = {"code": code, "path": path}
        if origin is not None:
            entry["origin"] = origin
        entries.append(entry)
    return entries


def unmerged_entries(repo_root: Path) -> list[dict[str, str]]:
    output = run(
        ["git", "-C", str(repo_root), "status", "--porcelain=v1", "-z"]
    ).stdout
    return [
        {**entry, "kind": UNMERGED_CODES[entry["code"]]}
        for entry in parse_status_z(output)
        if entry["code"] in UNMERGED_CODES
    ]


def parse_conflict_markers(text: str) -> dict[str, Any]:
    """Locate every conflict region a merge left in a file.

    The scan reports unbalanced markers rather than guessing, because a partly
    edited region is exactly the state that silently ships a broken file.
    """
    regions: list[dict[str, Any]] = []
    problems: list[str] = []
    current: dict[str, Any] | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if CONFLICT_START.match(line):
            if current is not None:
                problems.append(
                    f"line {number}: a conflict region opened inside the region "
                    f"opened on line {current['start_line']}"
                )
            current = {
                "start_line": number,
                "ancestor_line": None,
                "separator_line": None,
                "end_line": None,
            }
            continue
        if current is None:
            if CONFLICT_END.match(line):
                problems.append(f"line {number}: a conflict region closed unopened")
            continue
        if CONFLICT_ANCESTOR.match(line) and current["separator_line"] is None:
            current["ancestor_line"] = number
        elif CONFLICT_SEPARATOR.match(line) and current["separator_line"] is None:
            current["separator_line"] = number
        elif CONFLICT_END.match(line):
            current["end_line"] = number
            if current["separator_line"] is None:
                problems.append(
                    f"line {number}: the conflict region opened on line "
                    f"{current['start_line']} has no separator"
                )
            regions.append(current)
            current = None
    if current is not None:
        problems.append(
            f"the conflict region opened on line {current['start_line']} never closed"
        )
        regions.append(current)
    return {"regions": regions, "problems": problems}


def read_worktree_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace")


def normalize_content(data: bytes | None) -> bytes | None:
    return None if data is None else data.replace(b"\r\n", b"\n")


def stage_blobs(repo_root: Path, path: str) -> dict[str, bytes | None]:
    return {
        "ancestor": git_bytes(repo_root, "show", f":1:{path}"),
        "head": git_bytes(repo_root, "show", f":2:{path}"),
        "base": git_bytes(repo_root, "show", f":3:{path}"),
    }


def commits_touching(
    repo_root: Path, revision_range: str, path: str
) -> list[dict[str, str]]:
    output = git_try(
        repo_root,
        "log",
        "--no-merges",
        "--format=%H%x1f%an%x1f%aI%x1f%s",
        revision_range,
        "--",
        path,
    )
    if output.returncode != 0:
        return []
    commits = []
    for line in output.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        commits.append(
            {
                "sha": parts[0],
                "author": parts[1],
                "date": parts[2],
                "subject": parts[3],
            }
        )
    return commits


def conflict_signature(paths: Iterable[str]) -> str:
    joined = "\n".join(sorted(paths))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def detect_no_progress(history: list[dict[str, Any]], signature: str) -> int:
    """Count how many finished attempts in a row ended on this same conflict set."""
    repeats = 0
    for entry in reversed(history):
        if entry.get("conflict_signature") != signature:
            break
        repeats += 1
    return repeats


def collect_conflicts(
    repo_root: Path, *, head_sha: str, base_sha: str, merge_base: str
) -> list[dict[str, Any]]:
    conflicts = []
    for entry in unmerged_entries(repo_root):
        path = entry["path"]
        blobs = stage_blobs(repo_root, path)
        text = read_worktree_text(repo_root / path)
        markers = (
            parse_conflict_markers(text)
            if text is not None
            else {"regions": [], "problems": []}
        )
        conflicts.append(
            {
                "path": path,
                "code": entry["code"],
                "kind": entry["kind"],
                "binary": text is None,
                "deletion": entry["code"] in DELETION_CONFLICT_CODES,
                "marker_regions": markers["regions"],
                "marker_problems": markers["problems"],
                "present_stages": sorted(
                    name for name, blob in blobs.items() if blob is not None
                ),
                "head_commits": commits_touching(
                    repo_root, f"{merge_base}..{head_sha}", path
                ),
                "base_commits": commits_touching(
                    repo_root, f"{merge_base}..{base_sha}", path
                ),
                "status": "conflicted",
                "rationale": None,
                "one_side": None,
            }
        )
    return sorted(conflicts, key=lambda item: item["path"])


def rebase_in_progress(repo_root: Path) -> bool:
    for name in ("rebase-merge", "rebase-apply"):
        location = git_try(repo_root, "rev-parse", "--git-path", name)
        if location.returncode == 0 and Path(location.stdout.strip()).exists():
            return True
    return False


def merge_in_progress(repo_root: Path) -> bool:
    return git_try(repo_root, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0


def integration_in_progress(repo_root: Path) -> str | None:
    if rebase_in_progress(repo_root):
        return "rebase"
    if merge_in_progress(repo_root):
        return "merge"
    return None


def require_no_integration_in_progress(repo_root: Path) -> None:
    in_progress = integration_in_progress(repo_root)
    if in_progress:
        raise WorkflowError(
            f"a {in_progress} is already in progress in {repo_root}; finish it with "
            "continue or undo it with abort before starting another attempt"
        )


def require_clean_worktree(repo_root: Path) -> None:
    dirty = git(repo_root, "status", "--porcelain=v1")
    if dirty:
        raise WorkflowError(f"worktree is not clean:\n{dirty}")


def find_remote(repo_root: Path, repo_name: str, *, push: bool) -> str:
    expected = repo_name.lower()
    for remote in git(repo_root, "remote").splitlines():
        url = git(
            repo_root, "remote", "get-url", *(("--push",) if push else ()), remote
        )
        parsed = github_repo_from_remote(url)
        if parsed and parsed.lower() == expected:
            return remote
    raise WorkflowError(f"no git remote points to {repo_name}")


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
    upstream = f"{pr['upstream_owner']}/{pr['upstream_repo']}".lower()
    head = f"{pr['head_owner']}/{pr['head_repo']}".lower()
    if head != upstream:
        return
    if not pr.get("head_branch") or remote_head(
        pr["head_owner"], pr["head_repo"], pr["head_branch"]
    ) is None:
        raise WorkflowError(
            "PR head repository is the upstream repository and the head branch does "
            "not exist; refusing to push directly upstream"
        )


def commit_subjects(repo_root: Path, revision_range: str) -> list[str]:
    output = git_try(repo_root, "log", "--reverse", "--format=%s", revision_range)
    if output.returncode != 0:
        raise WorkflowError(f"could not list commits in {revision_range}")
    return [line for line in output.stdout.splitlines() if line.strip()]


def missing_subjects(original: list[str], rewritten: list[str]) -> list[str]:
    """Report the commit subjects a rewrite dropped.

    A rebase changes commit SHAs and, where it resolved a conflict, patch content
    as well. Subjects survive both, so they are what proves no commit vanished.
    """
    remaining = list(rewritten)
    missing = []
    for subject in original:
        if subject in remaining:
            remaining.remove(subject)
        else:
            missing.append(subject)
    return missing


def verify_push_range(
    repo_root: Path,
    *,
    strategy: str,
    previous_remote_head: str | None,
    local_head: str,
    merge_base: str,
    original_subjects: list[str],
) -> dict[str, Any]:
    """Prove the push moved the head branch the way this strategy intends."""
    report: dict[str, Any] = {"strategy": strategy, "checks": []}
    if strategy == "merge":
        if previous_remote_head is None:
            raise WorkflowError(
                "refusing to push a merge onto a head branch that does not exist"
            )
        ancestry = git_try(
            repo_root, "merge-base", "--is-ancestor", previous_remote_head, local_head
        )
        if ancestry.returncode != 0:
            raise WorkflowError(
                "refusing to push: the merge result does not contain the current "
                f"remote head {previous_remote_head}, so the push would not be a "
                "fast-forward"
            )
        report["checks"].append("the pushed head contains the previous remote head")
        report["added_commits"] = [
            line
            for line in git(
                repo_root, "rev-list", f"{previous_remote_head}..{local_head}"
            ).splitlines()
            if line
        ]
        return report

    rewritten = commit_subjects(repo_root, f"{merge_base}..{local_head}")
    dropped = missing_subjects(original_subjects, rewritten)
    if dropped:
        raise WorkflowError(
            "refusing to force-push: the rebase dropped commits with these subjects: "
            + "; ".join(dropped)
        )
    report["checks"].append("every original commit subject survived the rebase")
    report["rewritten_commits"] = rewritten
    return report


def active_attempt(state: dict[str, Any]) -> dict[str, Any]:
    attempt = state.get("attempt")
    if not attempt:
        raise WorkflowError("state has no attempt; run preflight first")
    if attempt.get("status") in {"published", "aborted"}:
        raise WorkflowError(
            f"this attempt is already {attempt['status']}; run preflight to start "
            "the next one"
        )
    return attempt


def find_conflicts(
    attempt: dict[str, Any], paths: Iterable[str]
) -> list[dict[str, Any]]:
    by_path = {conflict["path"]: conflict for conflict in attempt.get("conflicts") or []}
    missing = [path for path in paths if path not in by_path]
    if missing:
        raise WorkflowError(f"paths are not conflicted in this attempt: {missing}")
    return [by_path[path] for path in paths]


def archive_attempt(state: dict[str, Any]) -> None:
    """Fold a finished attempt into the durable history."""
    attempt = state.get("attempt")
    if not attempt or attempt.get("status") not in {"published", "aborted", "escalated"}:
        return
    history = state.setdefault("history", [])
    if any(entry.get("id") == attempt.get("id") for entry in history):
        return
    history.append(
        {
            "id": attempt.get("id"),
            "iteration": attempt.get("iteration"),
            "strategy": attempt.get("strategy"),
            "status": attempt.get("status"),
            "head_sha": attempt.get("head_sha"),
            "base_sha": attempt.get("base_sha"),
            "published_head_sha": attempt.get("published_head_sha"),
            "conflict_signature": attempt.get("conflict_signature"),
            "conflict_paths": [
                conflict["path"] for conflict in attempt.get("conflicts") or []
            ],
            "resolutions": [
                {
                    "path": conflict["path"],
                    "kind": conflict.get("kind"),
                    "rationale": conflict.get("rationale"),
                    "one_side": conflict.get("one_side"),
                }
                for conflict in attempt.get("conflicts") or []
                if conflict.get("status") == "resolved"
            ],
            "started_at": attempt.get("started_at"),
            "ended_at": utc_now(),
        }
    )


def record_escalation(
    state: dict[str, Any],
    *,
    kind: str,
    reason: str,
    recommended_action: str | None,
    iteration: int | None,
) -> dict[str, Any]:
    escalation = {
        "kind": kind,
        "reason": reason,
        "recommended_action": recommended_action,
        "iteration": iteration,
        "recorded_at": utc_now(),
    }
    state["escalation"] = escalation
    return escalation


def attempt_summary(attempt: dict[str, Any] | None) -> dict[str, Any] | None:
    if attempt is None:
        return None
    return {
        "id": attempt.get("id"),
        "status": attempt.get("status"),
        "iteration": attempt.get("iteration"),
        "strategy": attempt.get("strategy"),
        "head_sha": attempt.get("head_sha"),
        "base_sha": attempt.get("base_sha"),
        "merge_base": attempt.get("merge_base"),
        "published_head_sha": attempt.get("published_head_sha"),
        "mergeable_at_head_sha": attempt.get("mergeable_at_head_sha"),
        "conflict_signature": attempt.get("conflict_signature"),
        "conflict_statuses": count_by_status(attempt.get("conflicts")),
    }


def fetch_reference(repo_root: Path, remote: str, branch: str, sha: str) -> None:
    fetch = git_try(repo_root, "fetch", "--no-tags", remote, sha)
    if fetch.returncode != 0:
        fetch = git_try(
            repo_root, "fetch", "--no-tags", remote, f"refs/heads/{branch}"
        )
    if fetch.returncode != 0:
        detail = fetch.stderr.strip() or fetch.stdout.strip() or "no output"
        raise WorkflowError(f"could not fetch {remote}/{branch}: {detail}")
    if git_try(repo_root, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        raise WorkflowError(
            f"commit {sha} is missing after fetching {remote}/{branch}"
        )


def checkout_pr_branch(
    repo_root: Path, target: dict[str, Any], metadata: dict[str, Any]
) -> None:
    """Check out the pull request's own branch.

    Resolving a conflict has to commit onto the head branch, so a detached head is
    never acceptable here even though it would be enough to read a diff.
    """
    run(["gh", "pr", "checkout", target["pr_url"]], cwd=repo_root)
    branch = git(repo_root, "branch", "--show-current")
    if branch != metadata["head_branch"]:
        raise WorkflowError(
            f"branch mismatch: local {branch!r}, PR head {metadata['head_branch']!r}"
        )
    local_head = git(repo_root, "rev-parse", "HEAD")
    if local_head != metadata["head_sha"]:
        raise WorkflowError(
            f"HEAD mismatch: local {local_head}, PR head {metadata['head_sha']}; "
            "this loop resolves the authoritative remote branch, so publish or "
            "reconcile local work before preflight"
        )


def command_preflight(args: argparse.Namespace) -> None:
    require_tools()
    repo_root = resolve_repo_root(args.repo_root)
    target = resolve_target(args.target, repo_root)
    state_path = cli_path(args.state) if args.state else default_state_path(target)
    state = load_state(state_path) if state_path.is_file() else None

    require_clean_worktree(repo_root)
    require_no_integration_in_progress(repo_root)

    metadata = live_mergeability(target)
    require_open_pull_request(metadata)
    checkout_pr_branch(repo_root, target, metadata)

    relations = stack_relations(metadata)
    merge_methods = repository_merge_methods(metadata["repo_name"])
    push_blockers = push_safety_blockers(metadata, relations)

    if state is None:
        state = {
            "version": STATE_VERSION,
            "created_at": utc_now(),
            "iterations": 0,
            "history": [],
            "escalation": None,
        }
    archive_attempt(state)
    state["iterations"] = int(state.get("iterations", 0))
    state["repo_root"] = str(repo_root)
    state["pr"] = metadata
    state["relations"] = relations
    state["merge_methods"] = merge_methods

    max_iterations = getattr(args, "max_iterations", DEFAULT_MAX_ITERATIONS)
    iteration = state["iterations"] + 1
    mergeability = classify_mergeability(metadata)

    strategy_choice: dict[str, Any] | None = None
    strategy_error: str | None = None
    try:
        strategy_choice = choose_strategy(
            args.strategy, merge_methods=merge_methods, relations=relations
        )
    except WorkflowError as error:
        strategy_error = str(error)

    if push_blockers:
        result = "unsafe_push"
    elif state["iterations"] >= max_iterations:
        result = "max_iterations_reached"
    elif mergeability == "mergeable":
        result = "mergeable"
    elif mergeability == "unknown":
        result = "unknown_mergeability"
    elif strategy_error is not None:
        result = "no_safe_strategy"
    else:
        result = "ready"

    if result in {"mergeable", "ready"}:
        attempt = {
            "id": f"pr-{metadata['number']}-iteration-{iteration}",
            "status": "mergeable" if result == "mergeable" else "planned",
            "iteration": iteration,
            "strategy": None if strategy_choice is None else strategy_choice["strategy"],
            "strategy_reason": None
            if strategy_choice is None
            else strategy_choice["reason"],
            "strategy_warnings": []
            if strategy_choice is None
            else strategy_choice["warnings"],
            "head_sha": metadata["head_sha"],
            "base_sha": metadata["base_sha"],
            "merge_base": None,
            "mergeable": metadata.get("mergeable"),
            "merge_state_status": metadata.get("merge_state_status"),
            "started_at": utc_now(),
            "conflicts": [],
            "conflict_signature": None,
            "published_head_sha": None,
            "mergeable_at_head_sha": metadata["head_sha"]
            if result == "mergeable"
            else None,
        }
        state["attempt"] = attempt
    else:
        state["attempt"] = None

    if result in {"unsafe_push", "max_iterations_reached", "no_safe_strategy", "unknown_mergeability"}:
        record_escalation(
            state,
            kind={
                "unsafe_push": "unsafe_push",
                "max_iterations_reached": "max_iterations",
                "no_safe_strategy": "unsafe_push",
                "unknown_mergeability": "unknown_mergeability",
            }[result],
            reason=strategy_error
            or ("; ".join(push_blockers) if push_blockers else result),
            recommended_action="a person must decide how to proceed on this pull request",
            iteration=iteration,
        )
    elif result in {"mergeable", "ready"}:
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
        "mergeability": mergeability,
        "relations": relations,
        "merge_methods": merge_methods,
        "push_blockers": push_blockers,
        "strategy": strategy_choice,
        "strategy_error": strategy_error,
        "escalation": state.get("escalation"),
        "history": state["history"],
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
                "is_draft": metadata["is_draft"],
            },
            "head_sha": metadata["head_sha"],
            "base_sha": metadata["base_sha"],
            "mergeability": mergeability,
            "merge_state_status": metadata.get("merge_state_status"),
            "strategy": None if strategy_choice is None else strategy_choice["strategy"],
            "strategy_reason": None
            if strategy_choice is None
            else strategy_choice["reason"],
            "strategy_error": strategy_error,
            "push_blockers": push_blockers,
            "counts": {
                "dependents": len(relations["dependents"]),
                "history": len(state["history"]),
            },
            "stacked_on": relations["stacked_on"],
            "escalation": state.get("escalation"),
            "iteration": iteration,
            "max_iterations": max_iterations,
        }
    )


def write_conflicts_result(
    state_path: Path, state: dict[str, Any], attempt: dict[str, Any], result: str
) -> Path:
    conflicts_path = conflicts_path_for(state_path)
    write_result_file(
        conflicts_path,
        {
            "result": result,
            "state": str(state_path),
            "pr": state["pr"],
            "attempt": attempt,
            "conflicts": attempt.get("conflicts") or [],
        },
        "conflicts",
    )
    return conflicts_path


def emit_conflicts(
    state_path: Path,
    conflicts_path: Path,
    state: dict[str, Any],
    attempt: dict[str, Any],
    result: str,
    extra: dict[str, Any] | None = None,
) -> None:
    conflicts = attempt.get("conflicts") or []
    payload = {
        "result": result,
        "state": str(state_path),
        "conflicts_path": str(conflicts_path),
        "repo_root": state["repo_root"],
        "attempt": attempt_summary(attempt),
        "conflict_paths": [conflict["path"] for conflict in conflicts],
        "counts": {
            "conflicts": len(conflicts),
            "binary": sum(1 for conflict in conflicts if conflict["binary"]),
            "deletion": sum(1 for conflict in conflicts if conflict["deletion"]),
            "marker_regions": sum(
                len(conflict["marker_regions"]) for conflict in conflicts
            ),
        },
    }
    if extra:
        payload.update(extra)
    emit(payload)


def start_integration(
    repo_root: Path, attempt: dict[str, Any], base_sha: str
) -> subprocess.CompletedProcess[str]:
    if attempt["strategy"] == "merge":
        return git_try(
            repo_root, "merge", "--no-commit", "--no-ff", base_sha
        )
    environment = {**os.environ, "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"}
    return run(
        ["git", "-C", str(repo_root), "rebase", base_sha],
        check=False,
        env=environment,
    )


def command_attempt(args: argparse.Namespace) -> None:
    require_tools()
    state_path = cli_path(args.state)
    state = load_state(state_path)
    attempt = active_attempt(state)
    if attempt["status"] != "planned":
        raise WorkflowError(
            f"this attempt is already {attempt['status']}; run preflight to start "
            "the next one"
        )
    repo_root = Path(state["repo_root"])
    pr = state["pr"]
    require_clean_worktree(repo_root)
    require_no_integration_in_progress(repo_root)

    branch = git(repo_root, "branch", "--show-current")
    if branch != pr["head_branch"]:
        raise WorkflowError(
            f"branch mismatch: local {branch!r}, PR head {pr['head_branch']!r}"
        )
    local_head = git(repo_root, "rev-parse", "HEAD")
    if local_head != attempt["head_sha"]:
        raise WorkflowError(
            f"HEAD mismatch: local {local_head}, pinned head {attempt['head_sha']}"
        )

    upstream_repo = f"{pr['upstream_owner']}/{pr['upstream_repo']}"
    remote = find_remote(repo_root, upstream_repo, push=False)
    fetch_reference(repo_root, remote, pr["base_branch"], pr["base_sha"])
    merge_base = git(repo_root, "merge-base", pr["base_sha"], attempt["head_sha"])
    attempt["merge_base"] = merge_base
    attempt["original_subjects"] = commit_subjects(
        repo_root, f"{merge_base}..{attempt['head_sha']}"
    )

    process = start_integration(repo_root, attempt, pr["base_sha"])
    conflicts = collect_conflicts(
        repo_root,
        head_sha=attempt["head_sha"],
        base_sha=pr["base_sha"],
        merge_base=merge_base,
    )
    attempt["conflicts"] = conflicts
    attempt["conflict_signature"] = conflict_signature(
        conflict["path"] for conflict in conflicts
    )
    attempt["command_output"] = (process.stdout + process.stderr).strip()

    if conflicts:
        attempt["status"] = "conflicted"
        repeats = detect_no_progress(state["history"], attempt["conflict_signature"])
        result = "no_progress" if repeats >= NO_PROGRESS_LIMIT else "conflicted"
        if result == "no_progress":
            attempt["status"] = "escalated"
            record_escalation(
                state,
                kind="no_progress",
                reason=(
                    f"the last {repeats} finished attempts ended on this same set of "
                    f"conflicted files: {', '.join(conflict['path'] for conflict in conflicts)}"
                ),
                recommended_action="a person must resolve this conflict by hand",
                iteration=attempt["iteration"],
            )
        save_state(state_path, state)
        conflicts_path = write_conflicts_result(state_path, state, attempt, result)
        emit_conflicts(
            state_path,
            conflicts_path,
            state,
            attempt,
            result,
            {"escalation": state.get("escalation")},
        )
        return

    if process.returncode != 0:
        detail = (process.stderr.strip() or process.stdout.strip() or "no output")
        raise WorkflowError(
            f"{attempt['strategy']} failed without leaving a conflicted file: {detail}"
        )

    if attempt["strategy"] == "merge":
        already_integrated = not merge_in_progress(repo_root)
        detail = f"merging {pr['base_branch']} into {pr['head_branch']} changed nothing"
    else:
        already_integrated = git(repo_root, "rev-parse", "HEAD") == attempt["head_sha"]
        detail = f"rebasing {pr['head_branch']} onto {pr['base_branch']} changed nothing"
    if already_integrated:
        attempt["status"] = "escalated"
        escalation = record_escalation(
            state,
            kind="other",
            reason=(
                f"{detail}, so this conflict does not come from the base branch this "
                "loop can integrate"
            ),
            recommended_action="a person must work out why GitHub still reports a conflict",
            iteration=attempt["iteration"],
        )
        archive_attempt(state)
        save_state(state_path, state)
        emit(
            {
                "result": "already_integrated",
                "state": str(state_path),
                "attempt": attempt_summary(attempt),
                "escalation": escalation,
            }
        )
        return

    attempt["status"] = "integrated" if attempt["strategy"] == "merge" else "resolved"
    save_state(state_path, state)
    emit(
        {
            "result": "no_conflicts",
            "state": str(state_path),
            "attempt": attempt_summary(attempt),
            "next": "continue" if attempt["strategy"] == "merge" else "publish",
        }
    )


def command_resolved(args: argparse.Namespace) -> None:
    state_path = cli_path(args.state)
    state = load_state(state_path)
    attempt = active_attempt(state)
    if attempt["status"] not in {"conflicted", "escalated"}:
        raise WorkflowError(
            f"no conflicted files are recorded for this attempt (status "
            f"{attempt['status']})"
        )
    repo_root = Path(state["repo_root"])
    conflicts = find_conflicts(attempt, args.paths)
    rationale = (
        load_text_input(args.rationale_file, "rationale")
        if args.rationale_file
        else args.rationale
    )

    recorded = []
    for conflict in conflicts:
        path = conflict["path"]
        blobs = stage_blobs(repo_root, path)
        target = repo_root / path
        deleted = not target.exists()
        if deleted and not args.accept_deletion:
            raise WorkflowError(
                f"{path} no longer exists in the worktree; a deletion resolution needs "
                "--accept-deletion together with the reason both sides allow it"
            )
        one_side = None
        if not deleted:
            text = read_worktree_text(target)
            if text is not None:
                markers = parse_conflict_markers(text)
                if markers["regions"] or markers["problems"]:
                    lines = ", ".join(
                        str(region["start_line"]) for region in markers["regions"]
                    )
                    raise WorkflowError(
                        f"{path} still holds conflict markers"
                        + (f" starting on lines {lines}" if lines else "")
                        + "".join(f"; {problem}" for problem in markers["problems"])
                    )
            content = normalize_content(target.read_bytes())
            if content is not None and content == normalize_content(blobs["head"]):
                one_side = "head"
            elif content is not None and content == normalize_content(blobs["base"]):
                one_side = "base"
        if one_side and not args.accept_one_side:
            raise WorkflowError(
                f"{path} is byte-for-byte the {one_side} side, so this resolution "
                "keeps only one side's work; combine both sides, or pass "
                "--accept-one-side with the reason the other side's change must not "
                "survive"
            )
        add = git_try(repo_root, "add", "--all", "--", path)
        if add.returncode != 0:
            detail = add.stderr.strip() or add.stdout.strip() or "no output"
            raise WorkflowError(f"could not stage {path}: {detail}")
        conflict["status"] = "resolved"
        conflict["rationale"] = rationale
        conflict["one_side"] = one_side
        conflict["deleted"] = deleted
        conflict["resolved_at"] = utc_now()
        recorded.append(
            {"path": path, "one_side": one_side, "deleted": deleted}
        )

    save_state(state_path, state)
    remaining = [
        conflict["path"]
        for conflict in attempt["conflicts"]
        if conflict["status"] != "resolved"
    ]
    emit(
        {
            "result": "recorded",
            "state": str(state_path),
            "resolved": recorded,
            "remaining_conflicts": remaining,
            "next": "continue" if not remaining else "resolved",
        }
    )


def merge_commit_message(state: dict[str, Any], attempt: dict[str, Any]) -> str:
    pr = state["pr"]
    lines = [
        f"Merge branch '{pr['base_branch']}' into {pr['head_branch']}",
        "",
        "Keep what both sides meant to do in every conflicted file.",
        "",
    ]
    for conflict in attempt.get("conflicts") or []:
        if conflict.get("status") != "resolved":
            continue
        rationale = (conflict.get("rationale") or "").strip() or "resolved"
        lines.append(f"{conflict['path']}: {rationale}")
    return "\n".join(lines).rstrip() + "\n"


def command_continue(args: argparse.Namespace) -> None:
    require_tools()
    state_path = cli_path(args.state)
    state = load_state(state_path)
    attempt = active_attempt(state)
    repo_root = Path(state["repo_root"])

    unresolved = [
        conflict["path"]
        for conflict in attempt.get("conflicts") or []
        if conflict.get("status") != "resolved"
    ]
    if unresolved:
        raise WorkflowError(f"these conflicted files are not resolved yet: {unresolved}")
    still_unmerged = [entry["path"] for entry in unmerged_entries(repo_root)]
    if still_unmerged:
        raise WorkflowError(
            f"git still reports these paths as unmerged: {still_unmerged}"
        )

    if attempt["strategy"] == "merge":
        if attempt["status"] not in {"conflicted", "integrated"}:
            raise WorkflowError(
                f"a merge cannot be completed from status {attempt['status']}"
            )
        if not merge_in_progress(repo_root):
            raise WorkflowError("no merge is in progress; run attempt again")
        handle, message_name = tempfile.mkstemp(prefix="conflict-fix-loop.", suffix=".txt")
        os.close(handle)
        message_path = Path(message_name)
        try:
            message_path.write_text(
                merge_commit_message(state, attempt), encoding="utf-8", newline="\n"
            )
            commit = git_try(repo_root, "commit", "--file", str(message_path))
        finally:
            message_path.unlink(missing_ok=True)
        if commit.returncode != 0:
            detail = commit.stderr.strip() or commit.stdout.strip() or "no output"
            raise WorkflowError(f"could not create the merge commit: {detail}")
        attempt["status"] = "resolved"
        attempt["resolved_head_sha"] = git(repo_root, "rev-parse", "HEAD")
        save_state(state_path, state)
        emit(
            {
                "result": "resolved",
                "state": str(state_path),
                "attempt": attempt_summary(attempt),
                "resolved_head_sha": attempt["resolved_head_sha"],
                "next": "publish",
            }
        )
        return

    environment = {**os.environ, "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"}
    process = run(
        ["git", "-C", str(repo_root), "rebase", "--continue"],
        check=False,
        env=environment,
    )
    output = (process.stdout + process.stderr).strip()
    if process.returncode != 0 and "no changes" in output.lower():
        emit(
            {
                "result": "empty_commit",
                "state": str(state_path),
                "detail": output,
                "next": "skip-empty",
            }
        )
        return

    conflicts = collect_conflicts(
        repo_root,
        head_sha=attempt["head_sha"],
        base_sha=state["pr"]["base_sha"],
        merge_base=attempt["merge_base"],
    )
    if conflicts:
        attempt["conflicts"] = conflicts
        attempt["conflict_signature"] = conflict_signature(
            conflict["path"] for conflict in conflicts
        )
        attempt["status"] = "conflicted"
        attempt["command_output"] = output
        save_state(state_path, state)
        conflicts_path = write_conflicts_result(state_path, state, attempt, "conflicted")
        emit_conflicts(state_path, conflicts_path, state, attempt, "conflicted")
        return

    if process.returncode != 0 or rebase_in_progress(repo_root):
        raise WorkflowError(f"rebase --continue did not finish: {output or 'no output'}")

    attempt["status"] = "resolved"
    attempt["resolved_head_sha"] = git(repo_root, "rev-parse", "HEAD")
    save_state(state_path, state)
    emit(
        {
            "result": "resolved",
            "state": str(state_path),
            "attempt": attempt_summary(attempt),
            "resolved_head_sha": attempt["resolved_head_sha"],
            "next": "publish",
        }
    )


def command_abort(args: argparse.Namespace) -> None:
    state_path = cli_path(args.state)
    state = load_state(state_path)
    attempt = state.get("attempt")
    repo_root = Path(state["repo_root"])
    in_progress = integration_in_progress(repo_root)
    if in_progress == "rebase":
        run(["git", "-C", str(repo_root), "rebase", "--abort"])
    elif in_progress == "merge":
        run(["git", "-C", str(repo_root), "merge", "--abort"])
    if attempt is not None and attempt.get("status") not in {"published"}:
        attempt["status"] = "aborted"
        archive_attempt(state)
        state["attempt"] = None
    save_state(state_path, state)
    emit(
        {
            "result": "aborted",
            "state": str(state_path),
            "undone": in_progress,
            "head_sha": git(repo_root, "rev-parse", "HEAD"),
        }
    )


def command_escalate(args: argparse.Namespace) -> None:
    state_path = cli_path(args.state)
    state = load_state(state_path)
    attempt = state.get("attempt")
    reason = (
        load_text_input(args.reason_file, "reason") if args.reason_file else args.reason
    )
    escalation = record_escalation(
        state,
        kind=args.kind,
        reason=reason,
        recommended_action=args.recommended_action,
        iteration=None if attempt is None else attempt.get("iteration"),
    )
    if attempt is not None and attempt.get("status") not in {"published", "aborted"}:
        attempt["status"] = "escalated"
        archive_attempt(state)
    save_state(state_path, state)
    emit(
        {
            "result": "escalated",
            "state": str(state_path),
            "escalation": escalation,
            "attempt": attempt_summary(attempt),
        }
    )


def command_publish(args: argparse.Namespace) -> None:
    require_tools()
    state_path = cli_path(args.state)
    state = load_state(state_path)
    attempt = active_attempt(state)
    repo_root = Path(state["repo_root"])
    pr = state["pr"]

    if attempt["status"] != "resolved":
        raise WorkflowError(
            f"only a resolved attempt can be published; this one is {attempt['status']}"
        )
    require_clean_worktree(repo_root)
    require_no_integration_in_progress(repo_root)

    branch = git(repo_root, "branch", "--show-current")
    if branch != pr["head_branch"]:
        raise WorkflowError(
            f"refusing to push from branch {branch!r}, which is not the PR head "
            f"branch {pr['head_branch']!r}"
        )

    relations = stack_relations(pr)
    state["relations"] = relations
    blockers = push_safety_blockers(pr, relations)
    if attempt["strategy"] == "rebase" and relations["dependents"]:
        listed = ", ".join(f"#{item['number']}" for item in relations["dependents"])
        blockers.append(
            "refusing to force-push a branch that open pull requests stack on: "
            f"{listed}"
        )
    if blockers:
        escalation = record_escalation(
            state,
            kind="unsafe_push",
            reason="; ".join(blockers),
            recommended_action="a person must decide how to publish this resolution",
            iteration=attempt.get("iteration"),
        )
        attempt["status"] = "escalated"
        archive_attempt(state)
        save_state(state_path, state)
        emit(
            {
                "result": "unsafe_push",
                "state": str(state_path),
                "push_blockers": blockers,
                "escalation": escalation,
            }
        )
        return

    require_fork_head(pr)
    local_head = git(repo_root, "rev-parse", "HEAD")
    if local_head == attempt["head_sha"]:
        raise WorkflowError(
            "the local head still equals the pinned PR head, so there is nothing "
            "this attempt resolved to publish"
        )

    previous_remote_head = remote_head(
        pr["head_owner"], pr["head_repo"], pr["head_branch"]
    )
    base_before = remote_head(
        pr["upstream_owner"], pr["upstream_repo"], pr["base_branch"]
    )
    dependents_before = {
        item["number"]: item["head_sha"] for item in relations["dependents"]
    }

    verification = verify_push_range(
        repo_root,
        strategy=attempt["strategy"],
        previous_remote_head=previous_remote_head,
        local_head=local_head,
        merge_base=attempt["merge_base"],
        original_subjects=attempt.get("original_subjects") or [],
    )

    remote = find_remote(
        repo_root, f"{pr['head_owner']}/{pr['head_repo']}", push=True
    )
    refspec = f"HEAD:refs/heads/{pr['head_branch']}"
    command = ["git", "-C", str(repo_root), "push"]
    if attempt["strategy"] == "rebase":
        if previous_remote_head is None:
            raise WorkflowError(
                "refusing to force-push a head branch that does not exist remotely"
            )
        command.append(
            f"--force-with-lease=refs/heads/{pr['head_branch']}:{previous_remote_head}"
        )
    command.extend([remote, refspec])
    if previous_remote_head != local_head:
        run(command)

    pushed_head = wait_for_remote_head(
        pr["head_owner"], pr["head_repo"], pr["head_branch"], local_head
    )
    if pushed_head != local_head:
        raise WorkflowError(
            f"head branch mismatch after push: local {local_head}, remote {pushed_head}"
        )
    base_after = remote_head(
        pr["upstream_owner"], pr["upstream_repo"], pr["base_branch"]
    )
    if base_after != base_before:
        raise WorkflowError(
            f"the base branch {pr['base_branch']} moved during the push: "
            f"{base_before} became {base_after}; inspect it at once"
        )
    dependents_after = {
        item["number"]: item["head_sha"] for item in stack_relations(pr)["dependents"]
    }
    disturbed = sorted(
        number
        for number, sha in dependents_before.items()
        if dependents_after.get(number, sha) != sha
    )
    if disturbed:
        raise WorkflowError(
            "the push disturbed open pull requests that stack on this branch: "
            + ", ".join(f"#{number}" for number in disturbed)
        )
    verification["checks"].append("no other branch moved during the push")

    target = parse_target(pr["pr_url"])
    refreshed = metadata_for(target)
    if refreshed["head_sha"] != local_head:
        time.sleep(PR_HEAD_LAG_RETRY_DELAY)
        refreshed = metadata_for(target)
    if refreshed["head_sha"] != local_head:
        raise WorkflowError(
            f"PR head mismatch: local {local_head}, PR head {refreshed['head_sha']}"
        )

    final = live_mergeability(target)
    mergeability = classify_mergeability(final)
    attempt["status"] = "published"
    attempt["published_head_sha"] = local_head
    attempt["push_verification"] = verification
    attempt["mergeable"] = final.get("mergeable")
    attempt["merge_state_status"] = final.get("merge_state_status")
    attempt["mergeable_at_head_sha"] = (
        local_head if mergeability == "mergeable" else None
    )
    state["iterations"] = int(state.get("iterations", 0)) + 1
    state["pr"] = final
    archive_attempt(state)
    save_state(state_path, state)
    emit(
        {
            "result": "published",
            "state": str(state_path),
            "head_sha": local_head,
            "previous_head_sha": attempt["head_sha"],
            "mergeability": mergeability,
            "mergeable_at_head_sha": attempt["mergeable_at_head_sha"],
            "push_verification": verification,
            "iterations": state["iterations"],
        }
    )


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
                    "attempt": None,
                    "escalation": None,
                    "history": [],
                }
            )
            return
    else:
        path = cli_path(args.state)
    state = load_state(path)
    pr = state["pr"]
    attempt = state.get("attempt")
    history = state.get("history") or []
    payload = {
        "result": "ready",
        "state": str(path),
        "pr": pr,
        "attempt": attempt,
        "relations": state.get("relations"),
        "merge_methods": state.get("merge_methods"),
        "escalation": state.get("escalation"),
        "history": history,
        "iterations": int(state.get("iterations", 0)),
    }
    status_path = status_path_for(path)
    write_result_file(status_path, payload, "status")
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
            "attempt": attempt_summary(attempt),
            "escalation": state.get("escalation"),
            "mergeable_at_head_sha": (attempt or {}).get("mergeable_at_head_sha"),
            "counts": {
                "conflicts": len(((attempt or {}).get("conflicts")) or []),
                "dependents": len(
                    ((state.get("relations") or {}).get("dependents")) or []
                ),
                "history": len(history),
            },
            "iterations": int(state.get("iterations", 0)),
        }
    )


def command_cleanup(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    load_state(path)
    path.unlink()
    preflight_path_for(path).unlink(missing_ok=True)
    conflicts_path_for(path).unlink(missing_ok=True)
    status_path_for(path).unlink(missing_ok=True)
    emit({"result": "cleaned_up", "state": str(path)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser(
        "preflight",
        help="resolve and check out a PR, read live mergeability, and pick a strategy",
    )
    preflight.add_argument(
        "target",
        nargs="?",
        help="PR URL or owner/repo#number; omit to use the current branch's PR",
    )
    preflight.add_argument("--repo-root")
    preflight.add_argument("--state")
    preflight.add_argument("--strategy", choices=list(STRATEGIES), default="auto")
    preflight.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
    preflight.set_defaults(function=command_preflight)

    attempt = subparsers.add_parser(
        "attempt", help="start the integration and report every conflicted file"
    )
    attempt.add_argument("--state", required=True)
    attempt.set_defaults(function=command_attempt)

    resolved = subparsers.add_parser(
        "resolved", help="record conflicted files this run resolved and stage them"
    )
    resolved.add_argument("--state", required=True)
    resolved.add_argument("--paths", nargs="+", required=True)
    rationale = resolved.add_mutually_exclusive_group(required=True)
    rationale.add_argument("--rationale")
    rationale.add_argument(
        "--rationale-file", help="UTF-8 rationale file, or - for standard input"
    )
    resolved.add_argument(
        "--accept-one-side",
        action="store_true",
        help="allow a resolution that is byte-for-byte one side of the conflict",
    )
    resolved.add_argument(
        "--accept-deletion",
        action="store_true",
        help="allow a resolution that leaves the file deleted",
    )
    resolved.set_defaults(function=command_resolved)

    continue_parser = subparsers.add_parser(
        "continue", help="finish the merge commit or replay the next rebased commit"
    )
    continue_parser.add_argument("--state", required=True)
    continue_parser.set_defaults(function=command_continue)

    abort = subparsers.add_parser(
        "abort", help="undo the in-progress merge or rebase and end the attempt"
    )
    abort.add_argument("--state", required=True)
    abort.set_defaults(function=command_abort)

    escalate = subparsers.add_parser(
        "escalate", help="record why this run stopped and needs a person"
    )
    escalate.add_argument("--state", required=True)
    escalate.add_argument("--kind", choices=list(ESCALATION_KINDS), required=True)
    reason = escalate.add_mutually_exclusive_group(required=True)
    reason.add_argument("--reason")
    reason.add_argument(
        "--reason-file", help="UTF-8 reason file, or - for standard input"
    )
    escalate.add_argument("--recommended-action")
    escalate.set_defaults(function=command_escalate)

    publish = subparsers.add_parser(
        "publish", help="push the resolved head branch and verify what moved"
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
