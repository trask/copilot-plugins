#!/usr/bin/env python3
"""Deterministic mechanics for the Copilot Review Loop custom agent."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
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


COPILOT_LOGINS = {
    "copilot-pull-request-reviewer",
    "copilot-pull-request-reviewer[bot]",
}
STATE_VERSION = 3
DEFAULT_MAX_ITERATIONS = 5
IS_WINDOWS = os.name == "nt"
# A pasted review or comment fragment is accepted and ignored: the queue is always
# every unresolved Copilot comment on the pull request.
TARGET_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
    r"/?(?:#\S*)?$"
)
SHORT_TARGET_PATTERN = re.compile(
    r"^(?P<owner>[^/]+)/(?P<repo>[^#]+)#(?P<number>\d+)$"
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
        raise WorkflowError(f"{' '.join(command)} failed ({process.returncode}): {detail}")
    return process


def git(repo_root: Path, *arguments: str) -> str:
    return run(["git", "-C", str(repo_root), *arguments]).stdout.strip()


def gh_json(arguments: list[str]) -> Any:
    output = run(["gh", *arguments]).stdout
    return json.loads(output) if output.strip() else None


def gh_paginated(endpoint: str) -> list[dict[str, Any]]:
    pages = gh_json(["api", "--paginate", "--slurp", endpoint])
    return [item for page in pages for item in page]


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


def parse_timestamp(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)


def parse_target(target: str) -> dict[str, Any]:
    match = TARGET_PATTERN.match(target) or SHORT_TARGET_PATTERN.match(target)
    if not match:
        raise WorkflowError("target must be a GitHub PR URL or owner/repo#number")
    values = match.groupdict()
    return {
        "owner": values["owner"],
        "repo": values["repo"],
        "number": int(values["number"]),
        "pr_url": (
            f"https://github.com/{values['owner']}/{values['repo']}/pull/{values['number']}"
        ),
    }


def default_state_path(target: dict[str, Any]) -> Path:
    name = f"{target['owner']}--{target['repo']}--{target['number']}.json"
    return Path.home() / ".copilot" / "run" / "copilot-review-loop" / name


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


def resolve_repo_root(value: str | None) -> Path:
    cwd = cli_path(value) if value else Path.cwd()
    output = run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"]).stdout.strip()
    return Path(output).resolve()


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
        raise WorkflowError(f"current branch {branch!r} has incomplete upstream configuration")

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
    process = run(
        ["gh", "pr", "view", "--json", fields], cwd=repo_root, check=False
    )
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
        target = ref.get("target") or {}
        connection = target.get("associatedPullRequests")
        if connection is None:
            return []
        for node in connection["nodes"]:
            repository = node.get("headRepository") or {}
            if (
                node.get("state") == "OPEN"
                and node.get("headRefName") == upstream["branch"]
                and repository.get("nameWithOwner", "").lower()
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
            f"no pull request found for current branch {branch!r}, which has no configured upstream"
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


def fetch_threads(owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    query = """
query($owner:String!,$repo:String!,$number:Int!,$after:String){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$number){
            reviewThreads(first:100,after:$after){
             pageInfo{hasNextPage endCursor}
             nodes{
        id isResolved
        comments(first:100){nodes{
          databaseId url body path position originalPosition line originalLine
          author{login ... on Bot{id}}
          pullRequestReview{databaseId}
        }}
      }}
    }
  }
}
"""
    threads: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        payload = graphql(
            query,
            {"owner": owner, "repo": repo, "number": number, "after": after},
        )
        connection = payload["data"]["repository"]["pullRequest"]["reviewThreads"]
        threads.extend(connection["nodes"])
        if not connection["pageInfo"]["hasNextPage"]:
            return threads
        after = connection["pageInfo"]["endCursor"]


def fetch_threads_by_id(thread_ids: Iterable[str]) -> list[dict[str, Any]]:
    unique_ids = list(dict.fromkeys(thread_ids))
    fields = " ".join(
        f"t{index}:node(id:{json.dumps(thread_id)})"
        "{... on PullRequestReviewThread{id isResolved comments(first:100){nodes{"
        "databaseId url body path position originalPosition line originalLine "
        "author{login ... on Bot{id}} pullRequestReview{databaseId}"
        "}}}}"
        for index, thread_id in enumerate(unique_ids)
    )
    payload = graphql(f"query{{{fields}}}", {})
    return [payload["data"][f"t{index}"] for index in range(len(unique_ids))]


def is_copilot_author(author: dict[str, Any] | None) -> bool:
    return bool(author) and author.get("login") in COPILOT_LOGINS


def select_queue(threads: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Queue the root comment of every unresolved Copilot review thread.

    Also returns the distinct authors of the unresolved threads that were skipped, so a
    change to Copilot's bot login surfaces as a diagnosable result instead of an empty queue.
    """
    queue: list[dict[str, Any]] = []
    skipped: list[str] = []
    for thread in threads:
        if thread["isResolved"]:
            continue
        comments = thread["comments"]["nodes"]
        if not comments:
            continue
        comment = comments[0]
        author = comment.get("author") or {}
        if not is_copilot_author(author):
            login = author.get("login") or "unknown"
            if login not in skipped:
                skipped.append(login)
            continue
        queue.append(
            {
                "id": comment["databaseId"],
                "source": "thread",
                "thread_id": thread["id"],
                "url": comment["url"],
                "author": author.get("login"),
                "author_bot_id": author.get("id"),
                "path": comment.get("path"),
                "position": comment.get("position"),
                "original_position": comment.get("originalPosition"),
                "line": comment.get("line"),
                "original_line": comment.get("originalLine"),
                "review_id": (comment.get("pullRequestReview") or {}).get("databaseId"),
                "body": comment.get("body", ""),
                "status": "pending",
                "batch": None,
                "commit": None,
                "rationale": None,
                "summary": None,
                "reply_id": None,
                "resolved": False,
            }
        )
    return queue, skipped


def latest_copilot_review(
    reviews: list[dict[str, Any]], bot_id: str | None
) -> dict[str, Any] | None:
    return max(
        (
            review
            for review in reviews
            if is_copilot(review.get("user"), bot_id)
        ),
        key=lambda review: int(review["id"]),
        default=None,
    )


def latest_copilot_review_for_head(
    reviews: list[dict[str, Any]], bot_id: str | None, head_sha: str
) -> dict[str, Any] | None:
    return latest_copilot_review(
        [
            review
            for review in reviews
            if review.get("commit_id") == head_sha
            and review.get("submitted_at")
            and str(review.get("state", "")).upper() != "DISMISSED"
        ],
        bot_id,
    )


def review_has_inline_findings(
    review: dict[str, Any], threads: list[dict[str, Any]]
) -> bool:
    review_id = int(review["id"])
    return any(
        (comment.get("pullRequestReview") or {}).get("databaseId") == review_id
        for thread in threads
        for comment in thread["comments"]["nodes"]
    )


def parse_suppressed_comments(body: str | None) -> list[dict[str, Any]]:
    if not body:
        return []
    for details_match in re.finditer(
        r"<details\b[^>]*>(?P<body>.*?)</details\s*>",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        details = details_match.group("body")
        summary_match = re.search(
            r"<summary\b[^>]*>(?P<summary>.*?)</summary\s*>",
            details,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not summary_match or "suppressed comments" not in re.sub(
            r"<[^>]+>", "", summary_match.group("summary")
        ).lower():
            continue
        content = details[summary_match.end() :]
        headers = list(
            re.finditer(
                r"^\s*\*\*(?P<path>.+):(?P<line>\d+)\*\*\s*$",
                content,
                flags=re.MULTILINE,
            )
        )
        entries = []
        for index, header in enumerate(headers):
            end = headers[index + 1].start() if index + 1 < len(headers) else len(content)
            comment_body = content[header.end() : end].strip()
            if comment_body.startswith("* "):
                comment_body = comment_body[2:].lstrip()
            entries.append(
                {
                    "path": header.group("path"),
                    "line": int(header.group("line")),
                    "body": comment_body,
                }
            )
        return entries
    return []


def suppressed_queue(
    review: dict[str, Any], entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    review_id = int(review["id"])
    author = review.get("user") or {}
    return [
        {
            "id": -(review_id * 1000 + index),
            "source": "suppressed",
            "thread_id": None,
            "url": review.get("html_url"),
            "author": author.get("login"),
            "author_bot_id": author.get("node_id"),
            "path": entry["path"],
            "position": None,
            "original_position": None,
            "line": entry["line"],
            "original_line": entry["line"],
            "review_id": review_id,
            "body": entry["body"],
            "status": "pending",
            "batch": None,
            "commit": None,
            "rationale": None,
            "summary": None,
            "reply": None,
            "reply_id": None,
            "resolved": False,
        }
        for index, entry in enumerate(entries)
    ]


def metadata_for(target: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id,number,title,url,headRefName,headRefOid,headRepositoryOwner,headRepository,"
        "baseRefName,baseRefOid"
    )
    metadata = gh_json(
        [
            "pr",
            "view",
            target["pr_url"],
            "--repo",
            f"{target['owner']}/{target['repo']}",
            "--json",
            fields,
        ]
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
    return {
        "pr_node_id": metadata["id"],
        "number": metadata["number"],
        "title": metadata["title"],
        "url": metadata["url"],
        "upstream_owner": target["owner"],
        "upstream_repo": target["repo"],
        "head_owner": head_owner["login"],
        "head_repo": head_repository["name"],
        "head_branch": metadata["headRefName"],
        "head_sha": metadata["headRefOid"],
        "base_branch": metadata["baseRefName"],
        "base_sha": metadata["baseRefOid"],
    }


def verify_checkout_head(repo_root: Path, local_head: str, pr_head: str) -> None:
    if local_head == pr_head:
        return
    ancestor = run(
        [
            "git",
            "-C",
            str(repo_root),
            "merge-base",
            "--is-ancestor",
            pr_head,
            local_head,
        ],
        check=False,
    )
    if ancestor.returncode == 0:
        return
    if ancestor.returncode != 1:
        detail = ancestor.stderr.strip() or ancestor.stdout.strip() or "no output"
        raise WorkflowError(f"failed to compare local and PR heads: {detail}")
    raise WorkflowError(f"HEAD mismatch: local {local_head}, PR head {pr_head}")


def checkout_pr(
    repo_root: Path, target: dict[str, Any], metadata: dict[str, Any]
) -> bool:
    current_branch = git(repo_root, "branch", "--show-current")
    on_pr_branch = current_branch == metadata["head_branch"]
    command = ["gh", "pr", "checkout", target["pr_url"]]
    if not on_pr_branch:
        command.append("--detach")
    run(command, cwd=repo_root)
    return on_pr_branch


def windows_process_is_running(pid: int) -> bool:
    """Query a Windows process handle without delivering a console signal."""
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    error_access_denied = 5
    error_invalid_parameter = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == error_access_denied:
            return True
        if error == error_invalid_parameter:
            return False
        raise OSError(error, f"failed to query process {pid}")
    try:
        result = kernel32.WaitForSingleObject(handle, 0)
        if result == wait_timeout:
            return True
        if result == wait_object_0:
            return False
        error = ctypes.get_last_error()
        raise OSError(error, f"failed to query process {pid}")
    finally:
        kernel32.CloseHandle(handle)


def process_is_running(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if IS_WINDOWS:
        return windows_process_is_running(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def watcher_result(state: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    state["monitoring"].update({"status": "completed", "result": result})
    return result


def request_watch_cancellation(state: dict[str, Any]) -> str | None:
    monitoring = state.get("monitoring")
    if not monitoring:
        return None
    if monitoring.get("status") == "requested":
        watcher_result(state, {"result": "cancelled_locally"})
        return "cancelled_locally"
    if monitoring.get("status") != "running":
        return None
    monitoring["cancel_requested"] = True
    if process_is_running(monitoring.get("pid")):
        return "cancel_requested"
    watcher_result(state, {"result": "cancelled_locally"})
    return "cancelled_locally"


HANDLED_FIELDS = (
    "status",
    "batch",
    "commit",
    "rationale",
    "summary",
    "reply",
    "reply_id",
    "stash_ref",
)


def carry_over_progress(
    previous: list[dict[str, Any]], refreshed: list[dict[str, Any]]
) -> None:
    """Keep approved-but-unpublished work when preflight re-runs on the same PR."""
    by_id = {comment["id"]: comment for comment in previous}
    for comment in refreshed:
        prior = by_id.get(comment["id"])
        if not prior:
            continue
        for field in HANDLED_FIELDS:
            if prior.get(field) is not None:
                comment[field] = prior[field]


def command_preflight(args: argparse.Namespace) -> None:
    require_tools()
    repo_root = resolve_repo_root(args.repo_root)
    target = resolve_target(args.target, repo_root)
    state_path = cli_path(args.state) if args.state else default_state_path(target)
    prior_state = load_state(state_path) if state_path.is_file() else None
    if prior_state:
        cancellation_result = request_watch_cancellation(prior_state)
        if cancellation_result:
            save_state(state_path, prior_state)
            if cancellation_result == "cancel_requested":
                raise WorkflowError(
                    "active watcher cancellation requested; wait for its terminal result, then rerun preflight"
                )

    dirty = git(repo_root, "status", "--porcelain=v1")
    if dirty:
        raise WorkflowError(f"worktree is not clean:\n{dirty}")

    metadata = metadata_for(target)
    checked_out_branch = checkout_pr(repo_root, target, metadata)
    branch = git(repo_root, "branch", "--show-current")
    head = git(repo_root, "rev-parse", "HEAD")
    if checked_out_branch and branch != metadata["head_branch"]:
        raise WorkflowError(
            f"branch mismatch: local {branch!r}, PR head {metadata['head_branch']!r}"
        )
    verify_checkout_head(repo_root, head, metadata["head_sha"])

    threads = fetch_threads(target["owner"], target["repo"], target["number"])
    comments, skipped_authors = select_queue(threads)
    known_bot_id = next(
        (comment["author_bot_id"] for comment in comments if comment.get("author_bot_id")),
        (prior_state or {}).get("copilot_bot_id"),
    )
    reviews = fetch_reviews(target["owner"], target["repo"], target["number"])
    suppressed_review = latest_copilot_review(reviews, known_bot_id)
    suppressed_entries = parse_suppressed_comments(
        suppressed_review.get("body") if suppressed_review else None
    )
    if suppressed_review:
        comments.extend(suppressed_queue(suppressed_review, suppressed_entries))
    head_review = latest_copilot_review_for_head(reviews, known_bot_id, head)
    head_review_clean = bool(
        head_review
        and not review_has_inline_findings(head_review, threads)
        and not parse_suppressed_comments(head_review.get("body"))
    )
    state = prior_state or {"version": STATE_VERSION, "created_at": utc_now()}
    state["iterations"] = int(state.get("iterations", 0))
    previous_queue = state.get("queue") or {}
    carry_over_progress(previous_queue.get("comments") or [], comments)
    state.update(
        {
            "repo_root": str(repo_root),
            "pr": metadata,
            "queue": {
                "id": f"pr-{target['number']}",
                "status": "active",
                "comments": comments,
                "batches": [
                    batch
                    for batch in previous_queue.get("batches") or []
                    if any(
                        comment["id"] in set(batch.get("comment_ids") or [])
                        for comment in comments
                    )
                ],
            },
        }
    )
    bot_id = next(
        (comment["author_bot_id"] for comment in comments if comment.get("author_bot_id")),
        None,
    )
    if bot_id:
        state["copilot_bot_id"] = bot_id
    save_state(state_path, state)
    max_iterations = getattr(args, "max_iterations", DEFAULT_MAX_ITERATIONS)
    iteration = state["iterations"] + 1
    review_required = not comments and not head_review_clean
    if (comments or review_required) and state["iterations"] >= max_iterations:
        result = "max_iterations_reached"
    elif comments:
        result = "ready"
    elif review_required:
        result = "review_required"
    elif skipped_authors:
        result = "no_copilot_comments"
    else:
        result = "no_unresolved_comments"
    emit(
        {
            "result": result,
            "state": str(state_path),
            "repo_root": str(repo_root),
            "queue": state["queue"],
            "skipped_authors": skipped_authors,
            "suppressed_review_id": (
                int(suppressed_review["id"]) if suppressed_review else None
            ),
            "head_review_id": int(head_review["id"]) if head_review else None,
            "head_review_url": head_review.get("html_url") if head_review else None,
            "head_review_clean": head_review_clean,
            "iteration": iteration,
            "max_iterations": max_iterations,
            "pr": metadata,
        }
    )


def active_queue(state: dict[str, Any]) -> dict[str, Any]:
    queue = state.get("queue")
    if not queue:
        raise WorkflowError("state has no queue")
    return queue


def find_comments(queue: dict[str, Any], ids: Iterable[int]) -> list[dict[str, Any]]:
    by_id = {comment["id"]: comment for comment in queue["comments"]}
    missing = [comment_id for comment_id in ids if comment_id not in by_id]
    if missing:
        raise WorkflowError(f"comments are not in the queue: {missing}")
    return [by_id[comment_id] for comment_id in ids]


def command_plan(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    queue = active_queue(state)
    comments = find_comments(queue, args.comments)
    batch = {
        "id": args.batch,
        "label": args.label,
        "comment_ids": args.comments,
        "paths": args.paths or [],
        "validation": args.validation,
        "status": "planned",
    }
    queue["batches"] = [item for item in queue["batches"] if item["id"] != args.batch]
    queue["batches"].append(batch)
    for comment in comments:
        comment["batch"] = args.batch
    save_state(path, state)
    emit({"result": "planned", "state": str(path), "batch": batch})


def command_refresh(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    queue = active_queue(state)
    comments = find_comments(queue, args.comments)
    thread_comments = [
        comment for comment in comments if comment.get("source", "thread") == "thread"
    ]
    threads = fetch_threads_by_id(
        comment["thread_id"] for comment in thread_comments
    ) if thread_comments else []
    current_by_id = {
        comment["databaseId"]: (thread, comment)
        for thread in threads
        for comment in thread["comments"]["nodes"]
    }
    refreshed = []
    for stored in comments:
        if stored.get("source") == "suppressed":
            refreshed.append(stored)
            continue
        current = current_by_id.get(stored["id"])
        if current is None:
            raise WorkflowError(f"comment {stored['id']} no longer exists")
        thread, comment = current
        stored.update(
            {
                "thread_id": thread["id"],
                "url": comment["url"],
                "author": comment.get("author", {}).get("login"),
                "path": comment.get("path"),
                "position": comment.get("position"),
                "original_position": comment.get("originalPosition"),
                "line": comment.get("line"),
                "original_line": comment.get("originalLine"),
                "body": comment.get("body", ""),
                "resolved": thread["isResolved"],
            }
        )
        refreshed.append(stored)
    save_state(path, state)
    emit({"result": "refreshed", "state": str(path), "comments": refreshed})


def command_record(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    queue = active_queue(state)
    comments = find_comments(queue, args.comments)
    reply = cli_path(args.reply_file).read_text(encoding="utf-8").strip()
    if not reply:
        raise WorkflowError("reply file is empty")
    commit = args.commit
    if commit:
        commit = git(Path(state["repo_root"]), "rev-parse", commit)
    for comment in comments:
        comment.update(
            {
                "batch": args.batch,
                "status": "handled",
                "commit": commit,
                "rationale": args.rationale,
                "summary": args.summary,
                "reply": reply,
            }
        )
    for batch in queue["batches"]:
        if batch["id"] == args.batch:
            batch["status"] = "approved"
    save_state(path, state)
    emit(
        {
            "result": "recorded",
            "state": str(path),
            "comment_ids": args.comments,
            "commit": commit,
            "rationale": args.rationale,
        }
    )


def command_skip(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    queue = active_queue(state)
    comments = find_comments(queue, args.comments)
    for comment in comments:
        comment.update(
            {
                "batch": args.batch,
                "status": "skipped",
                "rationale": args.rationale,
                "stash_ref": args.stash_ref,
            }
        )
    for batch in queue["batches"]:
        if batch["id"] == args.batch:
            batch.update({"status": "skipped", "stash_ref": args.stash_ref})
    save_state(path, state)
    emit(
        {
            "result": "skipped",
            "state": str(path),
            "comment_ids": args.comments,
            "stash_ref": args.stash_ref,
        }
    )


def github_repo_from_remote(url: str) -> str | None:
    patterns = (
        re.compile(
            r"^(?:https?|git|ssh)://(?:[^@/\s]+@)?github\.com(?::\d+)?/"
            r"(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?/?$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:[^@/\s]+@)?github\.com:"
            r"(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?$",
            re.IGNORECASE,
        ),
    )
    for pattern in patterns:
        match = pattern.match(url)
        if match:
            return match.group("repo")
    return None


def find_push_remote(repo_root: Path, owner: str, repo: str) -> str:
    expected = f"{owner}/{repo}".lower()
    for remote in git(repo_root, "remote").splitlines():
        url = git(repo_root, "remote", "get-url", "--push", remote)
        parsed = github_repo_from_remote(url)
        if parsed and parsed.lower() == expected:
            return remote
    raise WorkflowError(f"no git remote points to PR head repository {owner}/{repo}")


def require_fork_head(pr: dict[str, Any]) -> None:
    upstream = f"{pr['upstream_owner']}/{pr['upstream_repo']}".lower()
    head = f"{pr['head_owner']}/{pr['head_repo']}".lower()
    if head != upstream:
        return
    # Some repositories host PR branches upstream; pushing to an existing one creates nothing new.
    if not pr.get("head_branch") or remote_head(
        pr["head_owner"], pr["head_repo"], pr["head_branch"]
    ) is None:
        raise WorkflowError(
            "PR head repository is the upstream repository and the head branch does not exist; "
            "refusing to push directly upstream"
        )


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


def is_copilot(user: dict[str, Any] | None, bot_id: str | None = None) -> bool:
    if not user:
        return False
    return user.get("login") in COPILOT_LOGINS or (
        bot_id is not None and user.get("node_id") == bot_id
    )


def fetch_reviews(owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    return gh_paginated(
        f"repos/{owner}/{repo}/pulls/{number}/reviews?per_page=100"
    )


def fetch_timeline(owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    return gh_paginated(
        f"repos/{owner}/{repo}/issues/{number}/timeline?per_page=100"
    )


def resolve_copilot_bot(state: dict[str, Any]) -> str:
    cached = state.get("copilot_bot_id")
    if cached:
        return cached
    pr = state["pr"]
    query = """
query($owner:String!,$repo:String!,$number:Int!){
 repository(owner:$owner,name:$repo){pullRequest(number:$number){
  reviewRequests(first:50){nodes{requestedReviewer{... on Bot{id login}}}}
 }}}
"""
    payload = graphql(
        query,
        {
            "owner": pr["upstream_owner"],
            "repo": pr["upstream_repo"],
            "number": pr["number"],
        },
    )
    requests = payload["data"]["repository"]["pullRequest"]["reviewRequests"]["nodes"]
    for request in requests:
        reviewer = request.get("requestedReviewer") or {}
        if reviewer.get("login") in COPILOT_LOGINS and reviewer.get("id"):
            state["copilot_bot_id"] = reviewer["id"]
            return reviewer["id"]
    for review in fetch_reviews(pr["upstream_owner"], pr["upstream_repo"], pr["number"]):
        if is_copilot(review.get("user")) and review["user"].get("node_id"):
            state["copilot_bot_id"] = review["user"]["node_id"]
            return state["copilot_bot_id"]
    for event in fetch_timeline(pr["upstream_owner"], pr["upstream_repo"], pr["number"]):
        reviewer = event.get("requested_reviewer") or event.get("reviewer")
        if reviewer and is_copilot(reviewer) and reviewer.get("node_id"):
            state["copilot_bot_id"] = reviewer["node_id"]
            return state["copilot_bot_id"]
    raise WorkflowError("could not resolve the Copilot reviewer bot node ID")


def reply_body(comment: dict[str, Any]) -> str:
    if comment.get("commit"):
        return f"Addressed in {comment['commit']}.\n\n{comment['reply']}"
    return f"No code change.\n\n{comment['reply']}"


def fetch_review_comments(owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    return gh_paginated(
        f"repos/{owner}/{repo}/pulls/{number}/comments?per_page=100"
    )


def post_missing_replies(
    state: dict[str, Any], comments: list[dict[str, Any]]
) -> dict[int, int]:
    comments = [
        comment for comment in comments if comment.get("source", "thread") == "thread"
    ]
    if not comments:
        return {}
    pr = state["pr"]
    existing = fetch_review_comments(
        pr["upstream_owner"], pr["upstream_repo"], pr["number"]
    )
    current_login = gh_json(["api", "user"])["login"]
    replies: dict[int, dict[str, Any]] = {}
    missing: list[tuple[dict[str, Any], str]] = []
    for comment in comments:
        expected_body = reply_body(comment)
        reply = next(
            (
                item
                for item in existing
                if item.get("in_reply_to_id") == comment["id"]
                and item.get("user", {}).get("login") == current_login
                and item.get("body") == expected_body
            ),
            None,
        )
        if reply is not None:
            replies[comment["id"]] = reply
        else:
            missing.append((comment, expected_body))

    def post(item: tuple[dict[str, Any], str]) -> tuple[int, dict[str, Any]]:
        comment, expected_body = item
        query = """
mutation($threadId:ID!,$body:String!){
 addPullRequestReviewThreadReply(input:{
  pullRequestReviewThreadId:$threadId,
  body:$body
 }){comment{databaseId}}
}
"""
        payload = graphql(
            query,
            {"threadId": comment["thread_id"], "body": expected_body},
        )
        reply = payload["data"]["addPullRequestReviewThreadReply"]["comment"]
        return comment["id"], {"id": reply["databaseId"]}

    if missing:
        with ThreadPoolExecutor(max_workers=min(8, len(missing))) as executor:
            for comment_id, reply in executor.map(post, missing):
                replies[comment_id] = reply

    reply_ids: dict[int, int] = {}
    for comment in comments:
        reply = replies[comment["id"]]
        comment["reply_id"] = reply["id"]
        reply_ids[comment["id"]] = reply["id"]
    return reply_ids


def resolve_threads(comments: list[dict[str, Any]]) -> None:
    thread_ids = list(
        dict.fromkeys(
            comment["thread_id"]
            for comment in comments
            if comment.get("source", "thread") == "thread"
        )
    )
    if not thread_ids:
        return
    fields = " ".join(
        f't{index}:resolveReviewThread(input:{{threadId:"{thread_id}"}})'
        "{thread{id isResolved}}"
        for index, thread_id in enumerate(thread_ids)
    )
    graphql(f"mutation{{{fields}}}", {})


def copilot_is_requested(state: dict[str, Any], bot_id: str) -> bool:
    pr = state["pr"]
    query = """
query($owner:String!,$repo:String!,$number:Int!){
 repository(owner:$owner,name:$repo){pullRequest(number:$number){
  reviewRequests(first:50){nodes{requestedReviewer{... on Bot{id login}}}}
 }}}
"""
    payload = graphql(
        query,
        {
            "owner": pr["upstream_owner"],
            "repo": pr["upstream_repo"],
            "number": pr["number"],
        },
    )
    requests = payload["data"]["repository"]["pullRequest"]["reviewRequests"]["nodes"]
    return any(
        request.get("requestedReviewer", {}).get("id") == bot_id for request in requests
    )


def request_copilot(state: dict[str, Any], path: Path) -> dict[str, Any]:
    pr = state["pr"]
    local_head = git(Path(state["repo_root"]), "rev-parse", "HEAD")
    existing = state.get("monitoring") or {}
    if (
        existing.get("head_sha") == local_head
        and existing.get("status") in {"requesting", "requested", "running"}
    ):
        if existing.get("status") == "requesting":
            request_visible = copilot_is_requested(state, existing["copilot_bot_id"])
            review_visible = matching_review(
                fetch_reviews(pr["upstream_owner"], pr["upstream_repo"], pr["number"]),
                existing,
            )
            if request_visible or review_visible:
                existing["status"] = "requested"
                save_state(path, state)
        if existing.get("status") != "requesting":
            return existing

    bot_id = resolve_copilot_bot(state)
    reviews = fetch_reviews(pr["upstream_owner"], pr["upstream_repo"], pr["number"])
    copilot_reviews = [review for review in reviews if is_copilot(review.get("user"), bot_id)]
    baseline = max((int(review["id"]) for review in copilot_reviews), default=0)
    request_start = utc_now()
    monitoring = {
        "status": "requesting",
        "head_sha": local_head,
        "request_start": request_start,
        "baseline_review_id": baseline,
        "copilot_bot_id": bot_id,
        "cancel_requested": False,
    }
    state["monitoring"] = monitoring
    save_state(path, state)
    query = """
mutation($pullRequest:ID!,$bot:ID!){
 requestReviews(input:{pullRequestId:$pullRequest,botIds:[$bot],union:true}){
  pullRequest{id}
 }
}
"""
    graphql(query, {"pullRequest": pr["pr_node_id"], "bot": bot_id})
    monitoring["status"] = "requested"
    save_state(path, state)
    return monitoring


def verify_publish(state: dict[str, Any], comments: list[dict[str, Any]]) -> dict[str, Any]:
    pr = state["pr"]
    local_head = git(Path(state["repo_root"]), "rev-parse", "HEAD")
    head_payload = gh_json(
        [
            "api",
            f"repos/{pr['upstream_owner']}/{pr['upstream_repo']}/pulls/{pr['number']}",
        ]
    )
    thread_comments = [
        comment for comment in comments if comment.get("source", "thread") == "thread"
    ]
    threads = (
        fetch_threads(pr["upstream_owner"], pr["upstream_repo"], pr["number"])
        if thread_comments
        else []
    )
    by_thread = {thread["id"]: thread for thread in threads}
    thread_results = []
    for comment in thread_comments:
        thread = by_thread.get(comment["thread_id"])
        reply_ids = {
            item["databaseId"] for item in thread["comments"]["nodes"]
        } if thread else set()
        thread_results.append(
            {
                "thread_id": comment["thread_id"],
                "resolved": bool(thread and thread["isResolved"]),
                "reply_present": comment.get("reply_id") in reply_ids,
            }
        )
    query = """
query($owner:String!,$repo:String!,$number:Int!){
 repository(owner:$owner,name:$repo){pullRequest(number:$number){
  reviewRequests(first:50){nodes{requestedReviewer{... on Bot{id login}}}}
 }}}
"""
    payload = graphql(
        query,
        {
            "owner": pr["upstream_owner"],
            "repo": pr["upstream_repo"],
            "number": pr["number"],
        },
    )
    requests = payload["data"]["repository"]["pullRequest"]["reviewRequests"]["nodes"]
    bot_id = state["monitoring"]["copilot_bot_id"]
    copilot_requested = any(
        request.get("requestedReviewer", {}).get("id") == bot_id for request in requests
    )
    completed_review = matching_review(
        fetch_reviews(pr["upstream_owner"], pr["upstream_repo"], pr["number"]),
        state["monitoring"],
    )
    result = {
        "head_matches": head_payload["head"]["sha"] == local_head,
        "head_sha": head_payload["head"]["sha"],
        "threads": thread_results,
        "copilot_requested": copilot_requested,
        "copilot_completed_review_id": (
            completed_review["id"] if completed_review else None
        ),
    }
    if not result["head_matches"] or not (copilot_requested or completed_review) or not all(
        item["resolved"] and item["reply_present"] for item in thread_results
    ):
        raise WorkflowError(f"publishing verification failed: {json.dumps(result)}")
    return result


def command_publish(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    repo_root = Path(state["repo_root"])
    dirty = git(repo_root, "status", "--porcelain=v1")
    if dirty:
        raise WorkflowError(f"worktree is not clean:\n{dirty}")
    queue = active_queue(state)
    comments = [
        comment for comment in queue["comments"] if comment["status"] == "handled"
    ]
    if not comments:
        if not args.no_comments:
            raise WorkflowError("there are no handled comments in the publishing scope")
        if queue["comments"]:
            raise WorkflowError("--no-comments requires an empty queue")
    incomplete = [
        comment["id"]
        for comment in comments
        if not comment.get("summary")
        or not comment.get("reply")
        or not (comment.get("commit") or comment.get("rationale"))
    ]
    if incomplete:
        raise WorkflowError(f"handled comments lack publish data: {incomplete}")

    pr = state["pr"]
    require_fork_head(pr)
    local_head = git(repo_root, "rev-parse", "HEAD")
    remote = find_push_remote(repo_root, pr["head_owner"], pr["head_repo"])
    if remote_head(pr["head_owner"], pr["head_repo"], pr["head_branch"]) != local_head:
        run(
            ["git", "-C", str(repo_root), "push", remote, f"HEAD:{pr['head_branch']}"]
        )
    pushed_head = remote_head(pr["head_owner"], pr["head_repo"], pr["head_branch"])
    if pushed_head != local_head:
        raise WorkflowError(f"fork ref mismatch: local {local_head}, remote {pushed_head}")

    reply_ids = post_missing_replies(state, comments) if comments else {}
    if comments:
        save_state(path, state)
        resolve_threads(comments)
        save_state(path, state)
    monitoring = request_copilot(state, path)
    verification = verify_publish(state, comments)
    queue["status"] = "published"
    state["iterations"] = int(state.get("iterations", 0)) + 1
    save_state(path, state)
    emit(
        {
            "result": "published",
            "state": str(path),
            "head_sha": local_head,
            "reply_ids": reply_ids,
            "monitoring": monitoring,
            "verification": verification,
        }
    )


def command_cancel_watch(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    result = request_watch_cancellation(state) or "cancelled_locally"
    save_state(path, state)
    emit({"result": result, "state": str(path)})


def matching_review(
    reviews: list[dict[str, Any]], monitoring: dict[str, Any]
) -> dict[str, Any] | None:
    candidates = [
        review
        for review in reviews
        if int(review["id"]) > monitoring["baseline_review_id"]
        and review.get("commit_id") == monitoring["head_sha"]
        and is_copilot(review.get("user"), monitoring["copilot_bot_id"])
        and review.get("submitted_at")
        and parse_timestamp(review["submitted_at"])
        >= parse_timestamp(monitoring["request_start"]) - dt.timedelta(seconds=1)
    ]
    return min(candidates, key=lambda review: int(review["id"]), default=None)


def command_watch(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    monitoring = state.get("monitoring")
    if not monitoring or monitoring.get("status") not in {"requested", "running"}:
        raise WorkflowError("state has no requested Copilot review to monitor")
    if monitoring.get("status") == "running":
        watcher_pid = monitoring.get("pid")
        if watcher_pid != os.getpid() and process_is_running(watcher_pid):
            raise WorkflowError(f"watcher is already running with pid {watcher_pid}")
        if monitoring.get("cancel_requested"):
            result = watcher_result(state, {"result": "cancelled_locally"})
            save_state(path, state)
            emit(result)
            return
    monitoring.update(
        {"status": "running", "pid": os.getpid(), "cancel_requested": False}
    )
    save_state(path, state)
    emit(
        {
            "result": "watching",
            "state": str(path),
            "head_sha": monitoring["head_sha"],
            "baseline_review_id": monitoring["baseline_review_id"],
        }
    )
    removal_seen_at: float | None = None
    try:
        while True:
            state = load_state(path)
            monitoring = state["monitoring"]
            if monitoring.get("cancel_requested"):
                result = watcher_result(state, {"result": "cancelled_locally"})
                save_state(path, state)
                emit(result)
                return
            pr = state["pr"]
            pr_payload = gh_json(
                [
                    "api",
                    f"repos/{pr['upstream_owner']}/{pr['upstream_repo']}/pulls/{pr['number']}",
                ]
            )
            actual_head = pr_payload["head"]["sha"]
            if actual_head != monitoring["head_sha"]:
                result = watcher_result(
                    state,
                    {
                        "result": "head_changed",
                        "expected_head": monitoring["head_sha"],
                        "actual_head": actual_head,
                    },
                )
                save_state(path, state)
                emit(result)
                return
            reviews = fetch_reviews(
                pr["upstream_owner"], pr["upstream_repo"], pr["number"]
            )
            review = matching_review(reviews, monitoring)
            if review:
                if str(review.get("state", "")).upper() == "DISMISSED":
                    result = watcher_result(
                        state,
                        {
                            "result": "review_dismissed",
                            "review_id": review["id"],
                            "review_url": review["html_url"],
                        },
                    )
                else:
                    comments = gh_paginated(
                        f"repos/{pr['upstream_owner']}/{pr['upstream_repo']}/pulls/"
                        f"{pr['number']}/reviews/{review['id']}/comments?per_page=100"
                    )
                    suppressed = parse_suppressed_comments(review.get("body"))
                    result = watcher_result(
                        state,
                        {
                            "result": (
                                "review_comments"
                                if comments or suppressed
                                else "review_no_comments"
                            ),
                            "review_id": review["id"],
                            "review_url": review["html_url"],
                            "comment_ids": [comment["id"] for comment in comments],
                            "suppressed_comment_count": len(suppressed),
                        },
                    )
                save_state(path, state)
                emit(result)
                return

            timeline = fetch_timeline(
                pr["upstream_owner"], pr["upstream_repo"], pr["number"]
            )
            removed = any(
                event.get("event") == "review_request_removed"
                and event.get("created_at")
                and parse_timestamp(event["created_at"])
                >= parse_timestamp(monitoring["request_start"])
                and is_copilot(
                    event.get("requested_reviewer") or event.get("reviewer"),
                    monitoring["copilot_bot_id"],
                )
                for event in timeline
            )
            if removed:
                removal_seen_at = removal_seen_at or time.monotonic()
                if time.monotonic() - removal_seen_at >= args.cancellation_grace:
                    result = watcher_result(state, {"result": "request_cancelled"})
                    save_state(path, state)
                    emit(result)
                    return
            else:
                removal_seen_at = None
            time.sleep(args.interval)
    except KeyboardInterrupt:
        state = load_state(path)
        state["monitoring"].update(
            {"status": "stopped", "result": {"result": "stopped"}}
        )
        save_state(path, state)
        emit({"result": "stopped"})


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
                    "pr": {
                        "number": target["number"],
                        "url": target["pr_url"],
                    },
                    "queue": None,
                    "monitoring": None,
                }
            )
            return
    else:
        path = cli_path(args.state)
    state = load_state(path)
    emit(
        {
            "result": "ready",
            "state": str(path),
            "pr": state["pr"],
            "queue": state.get("queue"),
            "monitoring": state.get("monitoring"),
        }
    )


def command_cleanup(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    monitoring = state.get("monitoring") or {}
    if monitoring.get("status") == "running":
        raise WorkflowError("cannot clean up while a watcher is running")
    path.unlink()
    emit({"result": "cleaned_up", "state": str(path)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser(
        "preflight",
        help="verify and check out a PR, then fetch its unresolved Copilot comments",
    )
    preflight.add_argument(
        "target",
        nargs="?",
        help="PR URL or owner/repo#number; omit to use the current branch's PR",
    )
    preflight.add_argument("--repo-root")
    preflight.add_argument("--state")
    preflight.add_argument(
        "--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS
    )
    preflight.set_defaults(function=command_preflight)

    plan = subparsers.add_parser("plan", help="record one planned review batch")
    plan.add_argument("--state", required=True)
    plan.add_argument("--batch", required=True)
    plan.add_argument("--comments", type=int, nargs="+", required=True)
    plan.add_argument("--label", required=True)
    plan.add_argument("--paths", nargs="*")
    plan.add_argument("--validation")
    plan.set_defaults(function=command_plan)

    refresh = subparsers.add_parser(
        "refresh", help="refresh current GitHub details for selected comments"
    )
    refresh.add_argument("--state", required=True)
    refresh.add_argument("--comments", type=int, nargs="+", required=True)
    refresh.set_defaults(function=command_refresh)

    record = subparsers.add_parser(
        "record", help="record an approved commit-backed or no-code batch"
    )
    record.add_argument("--state", required=True)
    record.add_argument("--batch", required=True)
    record.add_argument("--comments", type=int, nargs="+", required=True)
    record.add_argument("--summary", required=True)
    record.add_argument("--reply-file", required=True)
    outcome = record.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--commit")
    outcome.add_argument("--rationale")
    record.set_defaults(function=command_record)

    skip = subparsers.add_parser("skip", help="record a recoverably skipped batch")
    skip.add_argument("--state", required=True)
    skip.add_argument("--batch", required=True)
    skip.add_argument("--comments", type=int, nargs="+", required=True)
    skip.add_argument("--rationale", required=True)
    skip.add_argument("--stash-ref")
    skip.set_defaults(function=command_skip)

    publish = subparsers.add_parser(
        "publish", help="push, reply, resolve, request Copilot, and verify"
    )
    publish.add_argument("--state", required=True)
    publish.add_argument("--no-comments", action="store_true")
    publish.set_defaults(function=command_publish)

    watch = subparsers.add_parser("watch", help="watch one requested Copilot review")
    watch.add_argument("--state", required=True)
    watch.add_argument("--interval", type=float, default=30.0)
    watch.add_argument("--cancellation-grace", type=float, default=120.0)
    watch.set_defaults(function=command_watch)

    cancel = subparsers.add_parser(
        "cancel-watch", help="ask the active state watcher to stop"
    )
    cancel.add_argument("--state", required=True)
    cancel.set_defaults(function=command_cancel_watch)

    status = subparsers.add_parser("status", help="print compact workflow state")
    status_source = status.add_mutually_exclusive_group(required=True)
    status_source.add_argument("--state")
    status_source.add_argument(
        "--current",
        action="store_true",
        help="resolve state for the pull request attached to the current branch",
    )
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