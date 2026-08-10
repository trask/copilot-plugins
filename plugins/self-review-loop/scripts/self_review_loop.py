#!/usr/bin/env python3
"""Deterministic mechanics for the Self Review Loop custom agent."""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterable


STATE_VERSION = 1
DEFAULT_MAX_ITERATIONS = 5
IS_WINDOWS = os.name == "nt"
PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
    r"/?(?:#\S*)?$"
)
SHORT_TARGET_PATTERN = re.compile(
    r"^(?P<owner>[^/\s]+)/(?P<repo>[^#/\s]+)#(?P<number>\d+)$"
)
HUNK_PATTERN = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@"
)
CANDIDATE_KEYS = {"path", "line", "side", "body"}


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
    return Path.home() / ".copilot" / "run" / "self-review-loop" / name


def diff_path_for(state_path: Path) -> Path:
    return state_path.parent / f"{state_path.name}.diff"


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
        "number,title,url,headRefName,headRefOid,headRepositoryOwner,headRepository,"
        "baseRefName,baseRefOid"
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
    return {
        "number": target["number"],
        "title": title.strip(),
        "pr_url": resolved["pr_url"],
        "repo_name": resolved["repo_name"],
        "upstream_owner": resolved["owner"],
        "upstream_repo": resolved["repo"],
        "head_owner": head_owner["login"],
        "head_repo": head_repository["name"],
        "head_branch": metadata["headRefName"],
        "head_sha": head_sha,
        "base_branch": metadata["baseRefName"],
        "base_sha": metadata["baseRefOid"],
    }


def require_checkout_head(local_head: str, pr_head: str) -> None:
    if local_head == pr_head:
        return
    raise WorkflowError(
        f"HEAD mismatch: local {local_head}, PR head {pr_head}; this loop reviews the "
        "authoritative remote diff, so publish or reconcile local work before preflight"
    )


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


def decode_diff_path(value: str) -> str | None:
    value = value.rstrip()
    if value == "/dev/null":
        return None
    if value.startswith('"'):
        try:
            value = ast.literal_eval(value)
        except (SyntaxError, ValueError) as error:
            raise WorkflowError(f"invalid quoted path in PR diff: {value}") from error
        try:
            value = value.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    else:
        value = value.split("\t", 1)[0]
    if value.startswith(("a/", "b/")):
        value = value[2:]
    if not value:
        raise WorkflowError("empty file path in PR diff")
    return value


def parse_unified_diff(diff_text: str) -> dict[str, dict[str, set[int]]]:
    anchors: dict[str, dict[str, set[int]]] = {}
    old_path: str | None = None
    new_path: str | None = None
    path: str | None = None
    old_line = new_line = 0
    old_remaining = new_remaining = 0
    in_hunk = False

    def finish_hunk() -> None:
        nonlocal in_hunk
        if in_hunk and (old_remaining or new_remaining):
            raise WorkflowError("PR diff ended before a hunk's declared line counts")
        in_hunk = False

    for raw_line in diff_text.split("\n"):
        raw_line = raw_line.removesuffix("\r")
        if raw_line.startswith("diff --git "):
            finish_hunk()
            old_path = new_path = path = None
            continue
        if not in_hunk and raw_line.startswith("--- "):
            old_path = decode_diff_path(raw_line[4:])
            continue
        if not in_hunk and raw_line.startswith("+++ "):
            new_path = decode_diff_path(raw_line[4:])
            path = new_path or old_path
            if path is None:
                raise WorkflowError("PR diff file has no usable path")
            anchors.setdefault(path, {"LEFT": set(), "RIGHT": set()})
            continue

        hunk = HUNK_PATTERN.match(raw_line)
        if hunk:
            finish_hunk()
            if path is None:
                raise WorkflowError("PR diff hunk appeared before file headers")
            old_line = int(hunk.group("old"))
            new_line = int(hunk.group("new"))
            old_remaining = int(hunk.group("old_count") or 1)
            new_remaining = int(hunk.group("new_count") or 1)
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if raw_line.startswith("\\"):
            continue
        if raw_line.startswith("+"):
            anchors[path]["RIGHT"].add(new_line)
            new_line += 1
            new_remaining -= 1
        elif raw_line.startswith("-"):
            anchors[path]["LEFT"].add(old_line)
            old_line += 1
            old_remaining -= 1
        elif raw_line.startswith(" "):
            old_line += 1
            new_line += 1
            old_remaining -= 1
            new_remaining -= 1
        else:
            raise WorkflowError(f"unexpected line inside PR diff hunk: {raw_line!r}")
        if old_remaining < 0 or new_remaining < 0:
            raise WorkflowError("PR diff hunk contains more lines than declared")
        if old_remaining == 0 and new_remaining == 0:
            in_hunk = False

    finish_hunk()
    return anchors


def fetch_authoritative_diff(pr: dict[str, Any]) -> str:
    return run(["gh", "pr", "diff", pr["pr_url"], "--repo", pr["repo_name"]]).stdout


def serialize_anchors(
    anchors: dict[str, dict[str, set[int]]]
) -> dict[str, dict[str, list[int]]]:
    return {
        path: {side: sorted(lines) for side, lines in sides.items()}
        for path, sides in anchors.items()
    }


def load_candidate_input(path_value: str) -> list[dict[str, Any]]:
    try:
        text = (
            sys.stdin.read()
            if path_value == "-"
            else cli_path(path_value).read_text(encoding="utf-8")
        )
    except OSError as error:
        raise WorkflowError(f"could not read candidates JSON: {error}") from error
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise WorkflowError(f"candidates are not valid JSON: {error}") from error
    if not isinstance(payload, list):
        raise WorkflowError("candidates JSON must be an array")
    return payload


def validate_candidates(
    candidates: list[dict[str, Any]],
    anchors: dict[str, dict[str, list[int]]],
) -> list[dict[str, Any]]:
    if not candidates:
        raise WorkflowError("at least one candidate is required")
    normalized: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise WorkflowError(f"candidate {index} must be an object")
        unknown = set(candidate) - CANDIDATE_KEYS
        missing = CANDIDATE_KEYS - set(candidate)
        if unknown or missing:
            raise WorkflowError(
                f"candidate {index} must contain exactly path, line, side, and body"
            )
        path = candidate["path"]
        line = candidate["line"]
        side = candidate["side"]
        body = candidate["body"]
        if not isinstance(path, str) or not path:
            raise WorkflowError(f"candidate {index} has an invalid path")
        if isinstance(line, bool) or not isinstance(line, int) or line <= 0:
            raise WorkflowError(f"candidate {index} has an invalid line")
        if not isinstance(side, str) or side not in {"LEFT", "RIGHT"}:
            raise WorkflowError(f"candidate {index} side must be LEFT or RIGHT")
        if not isinstance(body, str) or not body.strip():
            raise WorkflowError(f"candidate {index} body must not be empty")
        if path not in anchors or line not in set(anchors[path][side]):
            raise WorkflowError(
                f"candidate {index} anchor is not a changed {side} line: {path}:{line}"
            )
        normalized.append({"path": path, "line": line, "side": side, "body": body.strip()})
    return normalized


def active_review(state: dict[str, Any]) -> dict[str, Any]:
    review = state.get("review")
    if not review:
        raise WorkflowError("state has no review")
    if review.get("status") == "published":
        raise WorkflowError(
            "this iteration is already published; run preflight to start the next one"
        )
    return review


def find_candidates(review: dict[str, Any], ids: Iterable[int]) -> list[dict[str, Any]]:
    by_id = {candidate["id"]: candidate for candidate in review["candidates"]}
    missing = [candidate_id for candidate_id in ids if candidate_id not in by_id]
    if missing:
        raise WorkflowError(f"candidates are not registered: {missing}")
    return [by_id[candidate_id] for candidate_id in ids]


def history_outcome(candidate: dict[str, Any]) -> str:
    status = candidate.get("status")
    if status == "handled":
        return "addressed" if candidate.get("commit") else "no_code"
    if status in {"dropped", "skipped"}:
        return status
    return "unresolved"


def archive_review(state: dict[str, Any]) -> None:
    """Fold a finished iteration's resolved candidates into the carried-forward history.

    Candidates an interrupted run never resolved are deliberately left out so a later
    review can raise them again.
    """
    review = state.get("review")
    if not review:
        return
    history = state.setdefault("history", [])
    recorded = {entry["id"] for entry in history}
    for candidate in review.get("candidates") or []:
        if candidate["id"] in recorded or candidate.get("status") not in {
            "handled",
            "dropped",
        }:
            continue
        history.append(
            {
                "id": candidate["id"],
                "iteration": review.get("iteration"),
                "path": candidate["path"],
                "line": candidate["line"],
                "side": candidate["side"],
                "body": candidate["body"],
                "outcome": history_outcome(candidate),
                "detail": candidate.get("rationale") or candidate.get("summary"),
                "commit": candidate.get("commit"),
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
    anchors = parse_unified_diff(diff_text)
    refreshed = metadata_for(target)
    if refreshed["head_sha"] != metadata["head_sha"]:
        raise WorkflowError(
            "PR head changed while the authoritative diff was fetched: expected "
            f"{metadata['head_sha']}, got {refreshed['head_sha']}"
        )

    if state is None:
        state = {
            "version": STATE_VERSION,
            "created_at": utc_now(),
            "iterations": 0,
            "next_candidate_id": 1,
            "history": [],
        }
    archive_review(state)
    state["iterations"] = int(state.get("iterations", 0))
    max_iterations = getattr(args, "max_iterations", DEFAULT_MAX_ITERATIONS)
    iteration = state["iterations"] + 1
    result = "max_iterations_reached" if state["iterations"] >= max_iterations else "ready"
    diff_path = diff_path_for(state_path)
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(diff_text, encoding="utf-8", newline="")
    state.update(
        {
            "repo_root": str(repo_root),
            "pr": metadata,
            "review": {
                "id": f"pr-{metadata['number']}-iteration-{iteration}",
                "status": "active",
                "iteration": iteration,
                "head_sha": metadata["head_sha"],
                "diff_path": str(diff_path),
                "anchors": serialize_anchors(anchors),
                "candidates": [],
                "batches": [],
            },
        }
    )
    save_state(state_path, state)
    emit(
        {
            "result": result,
            "state": str(state_path),
            "repo_root": str(repo_root),
            "pr": metadata,
            "head_sha": metadata["head_sha"],
            "diff_path": str(diff_path),
            "changed_files": sorted(anchors),
            "history": state["history"],
            "iteration": iteration,
            "max_iterations": max_iterations,
        }
    )


def command_candidates(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    review = active_review(state)
    if review["candidates"]:
        raise WorkflowError(
            "candidates are already registered for this iteration; "
            "run preflight to start the next one"
        )
    validated = validate_candidates(load_candidate_input(args.input), review["anchors"])
    next_id = int(state.get("next_candidate_id", 1))
    registered = []
    for candidate in validated:
        registered.append({"id": next_id, "status": "pending", **candidate})
        next_id += 1
    review["candidates"] = registered
    state["next_candidate_id"] = next_id
    save_state(path, state)
    emit({"result": "registered", "state": str(path), "candidates": registered})


def command_drop(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    review = active_review(state)
    candidates = find_candidates(review, args.candidates)
    for candidate in candidates:
        candidate.update({"status": "dropped", "rationale": args.rationale})
    save_state(path, state)
    emit(
        {
            "result": "dropped",
            "state": str(path),
            "candidate_ids": args.candidates,
            "rationale": args.rationale,
        }
    )


def command_plan(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    review = active_review(state)
    candidates = find_candidates(review, args.candidates)
    dropped = [
        candidate["id"] for candidate in candidates if candidate["status"] == "dropped"
    ]
    if dropped:
        raise WorkflowError(f"dropped candidates cannot be planned: {dropped}")
    batch = {
        "id": args.batch,
        "label": args.label,
        "candidate_ids": args.candidates,
        "paths": args.paths or [],
        "validation": args.validation,
        "status": "planned",
    }
    review["batches"] = [item for item in review["batches"] if item["id"] != args.batch]
    review["batches"].append(batch)
    for candidate in candidates:
        candidate["batch"] = args.batch
    save_state(path, state)
    emit({"result": "planned", "state": str(path), "batch": batch})


def command_record(args: argparse.Namespace) -> None:
    if not args.commit and not args.rationale:
        raise WorkflowError("record requires either --commit or --rationale")
    path = cli_path(args.state)
    state = load_state(path)
    review = active_review(state)
    candidates = find_candidates(review, args.candidates)
    commit = args.commit
    if commit:
        commit = git(Path(state["repo_root"]), "rev-parse", commit)
    for candidate in candidates:
        candidate.update(
            {
                "batch": args.batch,
                "status": "handled",
                "commit": commit,
                "rationale": args.rationale,
                "summary": args.summary,
            }
        )
    for batch in review["batches"]:
        if batch["id"] == args.batch:
            batch["status"] = "approved"
    save_state(path, state)
    emit(
        {
            "result": "recorded",
            "state": str(path),
            "candidate_ids": args.candidates,
            "commit": commit,
            "rationale": args.rationale,
        }
    )


def command_skip(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    review = active_review(state)
    candidates = find_candidates(review, args.candidates)
    for candidate in candidates:
        candidate.update(
            {"batch": args.batch, "status": "skipped", "rationale": args.rationale}
        )
    for batch in review["batches"]:
        if batch["id"] == args.batch:
            batch["status"] = "skipped"
    save_state(path, state)
    emit(
        {
            "result": "skipped",
            "state": str(path),
            "candidate_ids": args.candidates,
            "rationale": args.rationale,
        }
    )


def command_resolve(args: argparse.Namespace) -> None:
    if args.outcome != "clean":
        raise WorkflowError("resolve outcome must be clean")
    path = cli_path(args.state)
    state = load_state(path)
    review = active_review(state)
    disallowed = [
        {"id": candidate["id"], "status": candidate.get("status")}
        for candidate in review.get("candidates") or []
        if candidate.get("status") != "dropped"
    ]
    if disallowed:
        raise WorkflowError(
            "a review can be marked clean only with no candidates or when every "
            f"candidate is dropped: {disallowed}"
        )
    target = parse_target(state["pr"]["pr_url"])
    live_head = metadata_for(target)["head_sha"]
    if live_head != review["head_sha"]:
        raise WorkflowError(
            f"PR head changed before clean resolution: expected {review['head_sha']}, "
            f"got {live_head}"
        )
    review["outcome"] = args.outcome
    review["clean_at_head_sha"] = review["head_sha"]
    save_state(path, state)
    emit(
        {
            "result": "resolved",
            "state": str(path),
            "outcome": args.outcome,
            "clean_at_head_sha": review["clean_at_head_sha"],
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


def command_publish(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    review = active_review(state)
    repo_root = Path(state["repo_root"])
    dirty = git(repo_root, "status", "--porcelain=v1")
    if dirty:
        raise WorkflowError(f"worktree is not clean:\n{dirty}")

    pending = [
        candidate["id"]
        for candidate in review["candidates"]
        if candidate["status"] == "pending"
    ]
    if pending:
        raise WorkflowError(f"candidates are neither dropped nor handled: {pending}")
    skipped = [
        candidate["id"]
        for candidate in review["candidates"]
        if candidate["status"] == "skipped"
    ]
    if skipped:
        raise WorkflowError(
            f"a batch was skipped by an unrecoverable validation failure: {skipped}; "
            "this run must stop without publishing partial work"
        )
    handled = [
        candidate
        for candidate in review["candidates"]
        if candidate["status"] == "handled"
    ]
    incomplete = [
        candidate["id"]
        for candidate in handled
        if not candidate.get("summary")
        or not (candidate.get("commit") or candidate.get("rationale"))
    ]
    if incomplete:
        raise WorkflowError(f"handled candidates lack publish data: {incomplete}")

    commits: list[str] = []
    for candidate in handled:
        commit = candidate.get("commit")
        if commit and commit not in commits:
            commits.append(commit)

    pinned = review["head_sha"]
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
    pushed_head = remote_head(pr["head_owner"], pr["head_repo"], pr["head_branch"])
    if pushed_head != local_head:
        raise WorkflowError(f"fork ref mismatch: local {local_head}, remote {pushed_head}")
    pr_head = metadata_for(parse_target(pr["pr_url"]))["head_sha"]
    if pr_head != local_head:
        raise WorkflowError(f"PR head mismatch: local {local_head}, PR head {pr_head}")

    review["status"] = "published"
    review["published_head_sha"] = local_head
    state["iterations"] = int(state.get("iterations", 0)) + 1
    archive_review(state)
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
                    "review": None,
                    "history": [],
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
            "review": state.get("review"),
            "history": state.get("history") or [],
            "iterations": int(state.get("iterations", 0)),
        }
    )


def command_cleanup(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    load_state(path)
    path.unlink()
    diff_path_for(path).unlink(missing_ok=True)
    emit({"result": "cleaned_up", "state": str(path)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser(
        "preflight",
        help="verify and check out a PR, then pin its authoritative diff snapshot",
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

    candidates = subparsers.add_parser(
        "candidates", help="register this iteration's candidate findings"
    )
    candidates.add_argument("--state", required=True)
    candidates.add_argument("--input", required=True, help="JSON file, or - for standard input")
    candidates.set_defaults(function=command_candidates)

    drop = subparsers.add_parser("drop", help="record evaluator-rejected candidates")
    drop.add_argument("--state", required=True)
    drop.add_argument("--candidates", type=int, nargs="+", required=True)
    drop.add_argument("--rationale", required=True)
    drop.set_defaults(function=command_drop)

    plan = subparsers.add_parser("plan", help="record one planned fix batch")
    plan.add_argument("--state", required=True)
    plan.add_argument("--batch", required=True)
    plan.add_argument("--candidates", type=int, nargs="+", required=True)
    plan.add_argument("--label", required=True)
    plan.add_argument("--paths", nargs="*")
    plan.add_argument("--validation")
    plan.set_defaults(function=command_plan)

    record = subparsers.add_parser("record", help="record a handled batch")
    record.add_argument("--state", required=True)
    record.add_argument("--batch", required=True)
    record.add_argument("--candidates", type=int, nargs="+", required=True)
    record.add_argument("--summary", required=True)
    record.add_argument("--commit")
    record.add_argument("--rationale")
    record.set_defaults(function=command_record)

    skip = subparsers.add_parser("skip", help="record a batch stopped by validation")
    skip.add_argument("--state", required=True)
    skip.add_argument("--batch", required=True)
    skip.add_argument("--candidates", type=int, nargs="+", required=True)
    skip.add_argument("--rationale", required=True)
    skip.set_defaults(function=command_skip)

    resolve = subparsers.add_parser("resolve", help="record a clean review outcome")
    resolve.add_argument("--state", required=True)
    resolve.add_argument("--outcome", choices=["clean"], required=True)
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
