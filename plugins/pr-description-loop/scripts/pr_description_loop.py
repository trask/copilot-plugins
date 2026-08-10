#!/usr/bin/env python3
"""Deterministic mechanics for the PR Description Loop custom agent."""

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
from typing import Any


STATE_VERSION = 1
IS_WINDOWS = os.name == "nt"
PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
    r"/?(?:#\S*)?$"
)
SHORT_TARGET_PATTERN = re.compile(
    r"^(?P<owner>[^/\s]+)/(?P<repo>[^#/\s]+)#(?P<number>\d+)$"
)
BARE_TARGET_PATTERN = re.compile(r"^#?(?P<number>\d+)$")
REPO_NAME_PATTERN = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)$")


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


def gh_json(arguments: list[str], *, cwd: Path | None = None) -> Any:
    output = run(["gh", *arguments], cwd=cwd).stdout
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


def parse_repo_name(value: str) -> tuple[str, str]:
    match = REPO_NAME_PATTERN.fullmatch(value)
    if not match:
        raise WorkflowError(f"invalid GitHub repository name: {value!r}")
    return match.group("owner"), match.group("repo")


def target_for(owner: str, repo: str, number: int) -> dict[str, Any]:
    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        "repo_name": f"{owner}/{repo}",
        "pr_url": f"https://github.com/{owner}/{repo}/pull/{number}",
    }


def parse_target(target: str, *, repo_name: str | None = None) -> dict[str, Any]:
    match = PR_URL_PATTERN.fullmatch(target) or SHORT_TARGET_PATTERN.fullmatch(target)
    if match:
        values = match.groupdict()
        return target_for(values["owner"], values["repo"], int(values["number"]))
    bare = BARE_TARGET_PATTERN.fullmatch(target)
    if bare and repo_name:
        owner, repo = parse_repo_name(repo_name)
        return target_for(owner, repo, int(bare.group("number")))
    if bare:
        raise WorkflowError("a bare PR number requires repository context")
    raise WorkflowError(
        "target must be a GitHub PR URL, owner/repo#number, or bare PR number"
    )


def default_state_path(target: dict[str, Any]) -> Path:
    name = f"{target['owner']}--{target['repo']}--{target['number']}.json"
    return Path.home() / ".copilot" / "run" / "pr-description-loop" / name


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise WorkflowError(f"state file does not exist: {path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as error:
        raise WorkflowError(f"state file is not valid UTF-8: {path}") from error
    except json.JSONDecodeError as error:
        raise WorkflowError(f"state file is not valid JSON: {path}: {error}") from error
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        raise WorkflowError(f"unsupported state version in {path}")
    if not isinstance(state.get("pr"), dict):
        raise WorkflowError(f"state file has no pull request metadata: {path}")
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
            "current branch "
            f"{branch!r} has unsupported upstream merge ref {merge_ref!r}"
        )
    remote_url = run(
        ["git", "-C", str(repo_root), "remote", "get-url", remote_name]
    ).stdout.strip()
    remote_repo = github_repo_from_remote(remote_url)
    if remote_repo is None:
        raise WorkflowError(
            f"upstream remote {remote_name!r} is not a supported GitHub URL: "
            f"{remote_url}"
        )
    return {
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
            or head_repo.casefold() != expected_upstream["repo"].casefold()
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
        detail = process.stderr.strip() or process.stdout.strip() or "no output"
        raise WorkflowError(
            f"gh pr view failed ({process.returncode}) while resolving the current "
            f"branch's pull request: {detail}"
        )
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
            nodes{url state headRefName headRepository{nameWithOwner}}
          }
        }
      }
    }
  }
}
"""
    owner, repo = parse_repo_name(upstream["repo"])
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
                and head_repository.get("nameWithOwner", "").casefold()
                == upstream["repo"].casefold()
            ):
                target = parse_target(node["url"])
                targets[target["pr_url"]] = target
        if not connection["pageInfo"]["hasNextPage"]:
            return list(targets.values())
        after = connection["pageInfo"]["endCursor"]


def current_pr_target(repo_root: Path) -> dict[str, Any]:
    branch = git(repo_root, "branch", "--show-current")
    if not branch:
        raise WorkflowError(
            "cannot resolve the current pull request from detached HEAD"
        )
    upstream = configured_upstream(repo_root, branch)
    if upstream is None:
        target = simple_current_pr_target(repo_root, None)
        if target is not None:
            return target
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


def repository_context(repo_root: Path) -> str:
    payload = gh_json(["repo", "view", "--json", "nameWithOwner"], cwd=repo_root)
    name = payload.get("nameWithOwner") if isinstance(payload, dict) else None
    if not isinstance(name, str):
        raise WorkflowError("gh repo view did not return a repository name")
    owner, repo = parse_repo_name(name)
    return f"{owner}/{repo}"


def resolve_target(value: str | None, repo_root: Path) -> dict[str, Any]:
    if value is None:
        return current_pr_target(repo_root)
    if BARE_TARGET_PATTERN.fullmatch(value):
        return parse_target(value, repo_name=repository_context(repo_root))
    return parse_target(value)


def metadata_for(target: dict[str, Any]) -> dict[str, Any]:
    fields = "number,title,body,url,headRefOid,isDraft"
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
    title = metadata.get("title")
    body = metadata.get("body")
    head_sha = metadata.get("headRefOid")
    is_draft = metadata.get("isDraft")
    if not isinstance(title, str) or not title.strip():
        raise WorkflowError("resolved PR metadata has no title")
    if not isinstance(body, str):
        raise WorkflowError("resolved PR metadata has no body")
    if not isinstance(head_sha, str) or not head_sha:
        raise WorkflowError("resolved PR metadata has no head commit")
    if not isinstance(is_draft, bool):
        raise WorkflowError("resolved PR metadata has no draft status")
    return {
        **resolved,
        "url": resolved["pr_url"],
        "title": title,
        "body": body,
        "head_sha": head_sha,
        "is_draft": is_draft,
    }


def same_pr(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left.get("number") == right.get("number")
        and str(left.get("repo_name", "")).casefold()
        == str(right.get("repo_name", "")).casefold()
    )


def target_from_state(state: dict[str, Any]) -> dict[str, Any]:
    pr = state["pr"]
    url = pr.get("url") or pr.get("pr_url")
    if not isinstance(url, str):
        raise WorkflowError("state pull request metadata has no URL")
    target = parse_target(url)
    if not same_pr(pr, target):
        raise WorkflowError("state pull request identity is inconsistent")
    return target


def require_expected_head(state: dict[str, Any], expected_head: str) -> str:
    pinned_head = state["pr"].get("head_sha")
    if not isinstance(pinned_head, str) or not pinned_head:
        raise WorkflowError("state has no pinned PR head")
    if expected_head != pinned_head:
        raise WorkflowError(
            f"expected head does not match pinned head: expected {expected_head}, "
            f"pinned {pinned_head}"
        )
    return pinned_head


def require_live_snapshot(
    state: dict[str, Any], live: dict[str, Any], expected_head: str
) -> None:
    if live["head_sha"] != expected_head:
        raise WorkflowError(
            f"PR head moved: expected {expected_head}, got {live['head_sha']}"
        )
    pinned = state["pr"]
    if live["title"] != pinned.get("title") or live["body"] != pinned.get("body"):
        raise WorkflowError(
            "live PR title or body no longer matches the pinned state; "
            "run preflight again"
        )


def proposal_count(state: dict[str, Any]) -> int:
    value = state.get("proposal_count", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkflowError("state has an invalid proposal counter")
    return value


def command_preflight(args: argparse.Namespace) -> None:
    require_tools()
    repo_root = resolve_repo_root(args.repo_root)
    target = resolve_target(args.target, repo_root)
    path = cli_path(args.state) if args.state else default_state_path(target)
    state = load_state(path) if path.is_file() else None
    metadata = metadata_for(target)
    if state is None:
        state = {
            "version": STATE_VERSION,
            "created_at": utc_now(),
            "proposal_count": 0,
        }
    elif not same_pr(state["pr"], metadata):
        raise WorkflowError("state file belongs to a different pull request")
    count = proposal_count(state)
    state.update(
        {
            "repo_root": str(repo_root),
            "pr": metadata,
            "proposal_count": count,
            "pinned_at": utc_now(),
        }
    )
    for key in ("proposal", "validated_head_sha", "validation"):
        state.pop(key, None)
    save_state(path, state)
    emit(
        {
            "result": "ready",
            "state": str(path),
            "pr": metadata,
            "title": metadata["title"],
            "body": metadata["body"],
            "head_sha": metadata["head_sha"],
        }
    )


def read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise WorkflowError(f"body file is not valid UTF-8: {path}") from error
    except OSError as error:
        raise WorkflowError(f"could not read body file {path}: {error}") from error


def command_propose(args: argparse.Namespace) -> None:
    if not args.title.strip():
        raise WorkflowError("proposal title must not be blank")
    path = cli_path(args.state)
    state = load_state(path)
    body_path = cli_path(args.body_file)
    body = read_utf8(body_path)
    count = proposal_count(state) + 1
    proposal = {
        "number": count,
        "title": args.title,
        "body": body,
        "proposed_at": utc_now(),
    }
    state["proposal_count"] = count
    state["proposal"] = proposal
    state.pop("validated_head_sha", None)
    state.pop("validation", None)
    save_state(path, state)
    emit(
        {
            "result": "proposed",
            "state": str(path),
            "proposal": proposal,
            "proposal_count": count,
        }
    )


def edit_pr(state_path: Path, state: dict[str, Any], proposal: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    handle, body_name = tempfile.mkstemp(
        prefix=f".{state_path.name}.body.", suffix=".txt", dir=state_path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(proposal["body"])
        pr = state["pr"]
        run(
            [
                "gh",
                "pr",
                "edit",
                pr["url"],
                "--repo",
                pr["repo_name"],
                "--title",
                proposal["title"],
                "--body-file",
                body_name,
            ]
        )
    finally:
        try:
            os.unlink(body_name)
        except FileNotFoundError:
            pass


def command_apply(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    pinned_head = require_expected_head(state, args.expected_head)
    proposal = state.get("proposal")
    if not isinstance(proposal, dict):
        raise WorkflowError("state has no stored proposal")
    title = proposal.get("title")
    body = proposal.get("body")
    if not isinstance(title, str) or not title.strip() or not isinstance(body, str):
        raise WorkflowError("stored proposal is invalid")
    target = target_from_state(state)
    live = metadata_for(target)
    require_live_snapshot(state, live, pinned_head)
    edit_pr(path, state, proposal)
    verified = metadata_for(target)
    if verified["head_sha"] != pinned_head:
        raise WorkflowError(
            f"PR head moved while applying the proposal: expected {pinned_head}, "
            f"got {verified['head_sha']}"
        )
    if verified["title"] != title or verified["body"] != body:
        raise WorkflowError(
            "PR title or body did not exactly match the stored proposal after apply"
        )
    state["pr"] = verified
    state["validated_head_sha"] = pinned_head
    state["validation"] = {
        "mode": "applied",
        "proposal_number": proposal.get("number"),
        "head_sha": pinned_head,
        "title": verified["title"],
        "body": verified["body"],
        "validated_at": utc_now(),
    }
    save_state(path, state)
    emit(
        {
            "result": "applied",
            "state": str(path),
            "head_sha": pinned_head,
            "title": verified["title"],
            "body": verified["body"],
            "validated_head_sha": pinned_head,
        }
    )


def command_validate(args: argparse.Namespace) -> None:
    if not args.no_change:
        raise WorkflowError("validate requires --no-change")
    path = cli_path(args.state)
    state = load_state(path)
    pinned_head = require_expected_head(state, args.expected_head)
    live = metadata_for(target_from_state(state))
    require_live_snapshot(state, live, pinned_head)
    state["pr"] = live
    state["validated_head_sha"] = pinned_head
    state["validation"] = {
        "mode": "no_change",
        "head_sha": pinned_head,
        "title": live["title"],
        "body": live["body"],
        "validated_at": utc_now(),
    }
    save_state(path, state)
    emit(
        {
            "result": "validated",
            "state": str(path),
            "head_sha": pinned_head,
            "title": live["title"],
            "body": live["body"],
            "validated_head_sha": pinned_head,
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
                    "pr": {
                        "number": target["number"],
                        "url": target["pr_url"],
                    },
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
            "proposal": state.get("proposal"),
            "proposal_count": proposal_count(state),
            "validated_head_sha": state.get("validated_head_sha"),
            "validation": state.get("validation"),
        }
    )


def command_cleanup(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    load_state(path)
    path.unlink()
    emit({"result": "cleaned_up", "state": str(path)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser(
        "preflight", help="resolve a pull request and pin its current description"
    )
    preflight.add_argument(
        "target",
        nargs="?",
        help=(
            "PR URL, owner/repo#number, or bare number; "
            "omit to use the current branch's PR"
        ),
    )
    preflight.add_argument("--repo-root")
    preflight.add_argument("--state")
    preflight.set_defaults(function=command_preflight)

    propose = subparsers.add_parser("propose", help="store a title and body proposal")
    propose.add_argument("--state", required=True)
    propose.add_argument("--title", required=True)
    propose.add_argument("--body-file", required=True)
    propose.set_defaults(function=command_propose)

    apply = subparsers.add_parser(
        "apply", help="apply and verify the stored proposal"
    )
    apply.add_argument("--state", required=True)
    apply.add_argument("--expected-head", required=True)
    apply.set_defaults(function=command_apply)

    validate = subparsers.add_parser(
        "validate", help="verify the pinned description without changing it"
    )
    validate.add_argument("--state", required=True)
    validate.add_argument("--expected-head", required=True)
    validate.add_argument("--no-change", action="store_true", required=True)
    validate.set_defaults(function=command_validate)

    status = subparsers.add_parser("status", help="print compact workflow state")
    status_source = status.add_mutually_exclusive_group(required=True)
    status_source.add_argument("--state")
    status_source.add_argument("--current", action="store_true")
    status.add_argument("--repo-root")
    status.set_defaults(function=command_status)

    cleanup = subparsers.add_parser("cleanup", help="delete external workflow state")
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
