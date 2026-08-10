#!/usr/bin/env python3
"""Deterministic mechanics for the PR Description Loop custom agent."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any


STATE_VERSION = 2
INDEX_KIND = "index"
RUN_KIND = "run"
IS_WINDOWS = os.name == "nt"
RESIDUAL_UPDATE_RACE = (
    "GitHub's pull request update endpoint does not support conditional unsafe "
    "requests. Another writer can still change metadata between the helper's final "
    "exact snapshot check and the PATCH request."
)
INDEX_LOCK_TIMEOUT_SECONDS = 10.0
INDEX_LOCK_STALE_SECONDS = 120.0
INDEX_LOCK_POLL_SECONDS = 0.05
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


def run_state_path(index_path: Path, run_id: str) -> Path:
    return index_path.with_name(f"{index_path.stem}--{run_id}.json")


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


def load_run_state(path: Path) -> dict[str, Any]:
    state = load_state(path)
    if state.get("kind") != RUN_KIND or not isinstance(state.get("run_id"), str):
        raise WorkflowError(f"state file is not a run state: {path}")
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


def index_lock_path(index_path: Path) -> Path:
    return index_path.with_name(f".{index_path.name}.lock")


def index_guard_path(index_path: Path) -> Path:
    return index_path.with_name(f".{index_path.name}.guard")


@contextmanager
def index_guard(
    index_path: Path,
    *,
    timeout_seconds: float,
    poll_seconds: float,
):
    path = index_guard_path(index_path)
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
        os.fsync(handle.fileno())
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    try:
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, PermissionError, OSError):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkflowError(
                        f"timed out waiting for PR state index guard {path}"
                    )
                time.sleep(min(poll_seconds, remaining))
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        return True
    return True


def read_lock_owner(path: Path) -> dict[str, Any] | None:
    try:
        owner = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError, OSError):
        return None
    if (
        not isinstance(owner, dict)
        or isinstance(owner.get("pid"), bool)
        or not isinstance(owner.get("pid"), int)
        or not isinstance(owner.get("created_at"), (int, float))
        or not isinstance(owner.get("nonce"), str)
        or not owner["nonce"]
    ):
        return None
    return owner


def reclaim_stale_index_lock(
    path: Path, *, stale_seconds: float = INDEX_LOCK_STALE_SECONDS
) -> bool:
    owner = read_lock_owner(path)
    if owner is None:
        return False
    if time.time() - owner["created_at"] < stale_seconds:
        return False
    if process_is_alive(owner["pid"]):
        return False
    confirmed = read_lock_owner(path)
    if confirmed != owner or process_is_alive(owner["pid"]):
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except PermissionError:
        return False
    return True


def release_index_lock(path: Path, nonce: str, *, timeout_seconds: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        owner = read_lock_owner(path)
        if owner is None:
            return not path.exists()
        if owner["nonce"] != nonce:
            return False
        confirmed = read_lock_owner(path)
        if confirmed != owner:
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return True
        except PermissionError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(INDEX_LOCK_POLL_SECONDS)
            continue
        return True


@contextmanager
def index_lock(
    index_path: Path,
    *,
    timeout_seconds: float = INDEX_LOCK_TIMEOUT_SECONDS,
    stale_seconds: float = INDEX_LOCK_STALE_SECONDS,
    poll_seconds: float = INDEX_LOCK_POLL_SECONDS,
):
    index_path.parent.mkdir(parents=True, exist_ok=True)
    # The OS-released guard serializes stale reclamation so a verified unlink cannot
    # race with another process replacing the owner-record lock.
    with index_guard(
        index_path,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
    ):
        path = index_lock_path(index_path)
        nonce = secrets.token_hex(16)
        owner = {
            "pid": os.getpid(),
            "created_at": time.time(),
            "nonce": nonce,
        }
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                handle = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                reclaim_stale_index_lock(path, stale_seconds=stale_seconds)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    current = read_lock_owner(path)
                    detail = (
                        f"pid {current['pid']}, nonce {current['nonce']}"
                        if current
                        else "an unreadable owner record"
                    )
                    raise WorkflowError(
                        f"timed out waiting for PR state index lock {path}: {detail}"
                    )
                time.sleep(min(poll_seconds, remaining))
                continue
            try:
                with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(owner, stream, separators=(",", ":"), sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                raise
            break
        try:
            yield
        finally:
            if not release_index_lock(path, nonce):
                raise WorkflowError(
                    "refusing to release PR state index lock not owned by this "
                    f"process: {path}"
                )


def run_summary(path: Path, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": state["run_id"],
        "state": str(path),
        "created_at": state["created_at"],
        "updated_at": state["updated_at"],
        "head_sha": state["pr"]["head_sha"],
        "title": state["pr"]["title"],
        "validated_head_sha": state.get("validated_head_sha"),
    }


def update_run_index_unlocked(
    index_path: Path, run_path: Path, state: dict[str, Any]
) -> None:
    if index_path.is_file():
        index = load_state(index_path)
        if index.get("kind") != INDEX_KIND:
            raise WorkflowError(f"PR state index has an unsupported shape: {index_path}")
        if not same_pr(index["pr"], state["pr"]):
            raise WorkflowError("PR state index belongs to a different pull request")
    else:
        index = {
            "version": STATE_VERSION,
            "kind": INDEX_KIND,
            "created_at": utc_now(),
            "runs": [],
        }
    summary = run_summary(run_path, state)
    index["runs"] = [
        item for item in index.get("runs", []) if item.get("run_id") != state["run_id"]
    ]
    index["runs"].append(summary)
    candidate_updated_at = state["updated_at"]
    if candidate_updated_at >= index.get("current_updated_at", ""):
        index["pr"] = state["pr"]
        index["latest_run_id"] = state["run_id"]
        index["latest_state"] = str(run_path)
        index["current_updated_at"] = candidate_updated_at
    validation = state.get("validation")
    if (
        state.get("validated_head_sha")
        and isinstance(validation, dict)
        and validation.get("validated_at", "")
        >= (index.get("validation") or {}).get("validated_at", "")
    ):
        index["validated_head_sha"] = state["validated_head_sha"]
        index["validation"] = validation
    save_state(index_path, index)


def update_run_index(index_path: Path, run_path: Path, state: dict[str, Any]) -> None:
    with index_lock(index_path):
        update_run_index_unlocked(index_path, run_path, state)


def refresh_run_index(run_path: Path, state: dict[str, Any]) -> None:
    value = state.get("index_path")
    if isinstance(value, str):
        update_run_index(cli_path(value), run_path, state)


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
    metadata = gh_json(
        ["api", f"repos/{target['repo_name']}/pulls/{target['number']}"]
    )
    if not isinstance(metadata, dict):
        raise WorkflowError("GitHub API did not return PR metadata")
    metadata_url = metadata.get("html_url")
    if not isinstance(metadata_url, str):
        raise WorkflowError("resolved PR metadata has no URL")
    resolved = parse_target(metadata_url)
    if (
        metadata.get("number") != target["number"]
        or resolved["repo_name"].casefold() != target["repo_name"].casefold()
    ):
        raise WorkflowError("resolved PR metadata does not match the requested target")
    title = metadata.get("title")
    body = metadata.get("body") or ""
    head = metadata.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    is_draft = metadata.get("draft")
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


def require_run_id(state: dict[str, Any], expected_run_id: str) -> str:
    run_id = state.get("run_id")
    if run_id != expected_run_id:
        raise WorkflowError(
            f"run ID mismatch: expected {expected_run_id}, state belongs to {run_id}"
        )
    return run_id


def require_live_snapshot(
    snapshot: dict[str, Any], live: dict[str, Any], expected_head: str
) -> None:
    if live["head_sha"] != expected_head:
        raise WorkflowError(
            f"PR head moved: expected {expected_head}, got {live['head_sha']}; "
            "no mutation was performed"
        )
    if (
        live["title"] != snapshot.get("title")
        or live["body"] != snapshot.get("body")
    ):
        raise WorkflowError(
            "live PR title or body no longer matches the exact approved snapshot; "
            "no mutation was performed; run preflight again"
        )


def proposal_count(state: dict[str, Any]) -> int:
    value = state.get("proposal_count", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkflowError("state has an invalid proposal counter")
    return value


def proposal_token_for(proposal: dict[str, Any]) -> str:
    bound = {
        "run_id": proposal.get("run_id"),
        "number": proposal.get("number"),
        "base": proposal.get("base"),
        "title": proposal.get("title"),
        "body": proposal.get("body"),
    }
    encoded = json.dumps(
        bound, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def command_preflight(args: argparse.Namespace) -> None:
    require_tools()
    repo_root = resolve_repo_root(args.repo_root)
    target = resolve_target(args.target, repo_root)
    metadata = metadata_for(target)
    run_id = secrets.token_hex(16)
    index_path = default_state_path(target)
    path = cli_path(args.state) if args.state else run_state_path(index_path, run_id)
    if path.exists():
        raise WorkflowError(
            f"refusing to invalidate an existing run state: {path}; "
            "start a new run without --state or choose a new path"
        )
    state = {
        "version": STATE_VERSION,
        "kind": RUN_KIND,
        "created_at": utc_now(),
        "run_id": run_id,
        "repo_root": str(repo_root),
        "pr": metadata,
        "proposal_count": 0,
        "pinned_at": utc_now(),
    }
    if not args.state:
        state["index_path"] = str(index_path)
    save_state(path, state)
    if not args.state:
        update_run_index(index_path, path, state)
    emit(
        {
            "result": "ready",
            "state": str(path),
            "index_state": str(index_path) if not args.state else None,
            "run_id": run_id,
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
    state = load_run_state(path)
    run_id = require_run_id(state, args.expected_run_id)
    body_path = cli_path(args.body_file)
    body = read_utf8(body_path)
    count = proposal_count(state) + 1
    proposal = {
        "number": count,
        "run_id": run_id,
        "base": {
            "head_sha": state["pr"]["head_sha"],
            "title": state["pr"]["title"],
            "body": state["pr"]["body"],
        },
        "title": args.title,
        "body": body,
        "proposed_at": utc_now(),
    }
    proposal["token"] = proposal_token_for(proposal)
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
            "proposal_token": proposal["token"],
            "run_id": run_id,
        }
    )


def update_pr(state_path: Path, state: dict[str, Any], proposal: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    handle, payload_name = tempfile.mkstemp(
        prefix=f".{state_path.name}.update.", suffix=".json", dir=state_path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(
                {"title": proposal["title"], "body": proposal["body"]},
                stream,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        pr = state["pr"]
        run(
            [
                "gh",
                "api",
                "--method",
                "PATCH",
                f"repos/{pr['repo_name']}/pulls/{pr['number']}",
                "--input",
                payload_name,
            ]
        )
    finally:
        try:
            os.unlink(payload_name)
        except FileNotFoundError:
            pass


def command_apply(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_run_state(path)
    run_id = require_run_id(state, args.expected_run_id)
    pinned_head = require_expected_head(state, args.expected_head)
    proposal = state.get("proposal")
    if not isinstance(proposal, dict):
        raise WorkflowError("state has no stored proposal")
    title = proposal.get("title")
    body = proposal.get("body")
    if not isinstance(title, str) or not title.strip() or not isinstance(body, str):
        raise WorkflowError("stored proposal is invalid")
    token = proposal.get("token")
    if (
        proposal.get("run_id") != run_id
        or token != args.expected_proposal_token
        or token != proposal_token_for(proposal)
    ):
        raise WorkflowError(
            "proposal token mismatch; refusing to apply a proposal from another "
            "run or a modified proposal"
        )
    base = proposal.get("base")
    if not isinstance(base, dict) or base != {
        "head_sha": pinned_head,
        "title": state["pr"].get("title"),
        "body": state["pr"].get("body"),
    }:
        raise WorkflowError(
            "proposal is not bound to this run's exact pinned snapshot"
        )
    target = target_from_state(state)
    live = metadata_for(target)
    require_live_snapshot(base, live, pinned_head)
    # GitHub does not support conditional requests for this unsafe endpoint, so keep
    # the final exact read adjacent to the direct PATCH and verify again afterward.
    immediately_before = metadata_for(target)
    require_live_snapshot(base, immediately_before, pinned_head)
    update_pr(path, state, proposal)
    verified = metadata_for(target)
    if verified["head_sha"] != pinned_head:
        raise WorkflowError(
            f"PR head moved while applying the proposal: expected {pinned_head}, "
            f"got {verified['head_sha']}; the update may already have been applied; "
            f"{RESIDUAL_UPDATE_RACE}"
        )
    if verified["title"] != title or verified["body"] != body:
        raise WorkflowError(
            "PR title or body did not exactly match the stored proposal after apply; "
            f"{RESIDUAL_UPDATE_RACE}"
        )
    state["pr"] = verified
    state["validated_head_sha"] = pinned_head
    state["validation"] = {
        "mode": "applied",
        "proposal_number": proposal.get("number"),
        "proposal_token": token,
        "run_id": run_id,
        "head_sha": pinned_head,
        "title": verified["title"],
        "body": verified["body"],
        "validated_at": utc_now(),
        "conditional_update": False,
        "precondition_strategy": "two_exact_reads_immediately_before_patch",
        "residual_race": RESIDUAL_UPDATE_RACE,
    }
    save_state(path, state)
    refresh_run_index(path, state)
    emit(
        {
            "result": "applied",
            "state": str(path),
            "head_sha": pinned_head,
            "title": verified["title"],
            "body": verified["body"],
            "validated_head_sha": pinned_head,
            "run_id": run_id,
            "proposal_token": token,
            "conditional_update": False,
            "residual_race": RESIDUAL_UPDATE_RACE,
        }
    )


def command_validate(args: argparse.Namespace) -> None:
    if not args.no_change:
        raise WorkflowError("validate requires --no-change")
    path = cli_path(args.state)
    state = load_run_state(path)
    run_id = require_run_id(state, args.expected_run_id)
    pinned_head = require_expected_head(state, args.expected_head)
    live = metadata_for(target_from_state(state))
    require_live_snapshot(state["pr"], live, pinned_head)
    state["pr"] = live
    state["validated_head_sha"] = pinned_head
    state["validation"] = {
        "mode": "no_change",
        "run_id": run_id,
        "head_sha": pinned_head,
        "title": live["title"],
        "body": live["body"],
        "validated_at": utc_now(),
    }
    save_state(path, state)
    refresh_run_index(path, state)
    emit(
        {
            "result": "validated",
            "state": str(path),
            "head_sha": pinned_head,
            "title": live["title"],
            "body": live["body"],
            "validated_head_sha": pinned_head,
            "run_id": run_id,
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
    if state.get("kind") == INDEX_KIND:
        emit(
            {
                "result": "ready",
                "state": str(path),
                "kind": INDEX_KIND,
                "pr": state["pr"],
                "latest_run_id": state.get("latest_run_id"),
                "latest_state": state.get("latest_state"),
                "runs": state.get("runs") or [],
                "validated_head_sha": state.get("validated_head_sha"),
                "validation": state.get("validation"),
            }
        )
        return
    if state.get("kind") != RUN_KIND:
        raise WorkflowError(f"state file has an unsupported shape: {path}")
    emit(
        {
            "result": "ready",
            "state": str(path),
            "kind": RUN_KIND,
            "run_id": state["run_id"],
            "pr": state["pr"],
            "proposal": state.get("proposal"),
            "proposal_count": proposal_count(state),
            "validated_head_sha": state.get("validated_head_sha"),
            "validation": state.get("validation"),
        }
    )


def command_cleanup(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    if state.get("kind") == INDEX_KIND:
        with index_lock(path):
            path.unlink()
        emit({"result": "cleaned_up", "state": str(path)})
        return
    index_value = state.get("index_path")
    if state.get("kind") == RUN_KIND and isinstance(index_value, str):
        index_path = cli_path(index_value)
        if index_path.is_file():
            with index_lock(index_path):
                index = load_state(index_path)
                if index.get("kind") == INDEX_KIND:
                    index["runs"] = [
                        item
                        for item in index.get("runs", [])
                        if item.get("run_id") != state.get("run_id")
                    ]
                    if index.get("latest_run_id") == state.get("run_id"):
                        latest = (
                            max(
                                index["runs"],
                                key=lambda item: item.get("updated_at", ""),
                            )
                            if index["runs"]
                            else None
                        )
                        index["latest_run_id"] = (
                            latest.get("run_id") if latest else None
                        )
                        index["latest_state"] = (
                            latest.get("state") if latest else None
                        )
                        index["current_updated_at"] = (
                            latest.get("updated_at") if latest else None
                        )
                    save_state(index_path, index)
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
    propose.add_argument("--expected-run-id", required=True)
    propose.add_argument("--title", required=True)
    propose.add_argument("--body-file", required=True)
    propose.set_defaults(function=command_propose)

    apply = subparsers.add_parser(
        "apply", help="apply and verify the stored proposal"
    )
    apply.add_argument("--state", required=True)
    apply.add_argument("--expected-head", required=True)
    apply.add_argument("--expected-run-id", required=True)
    apply.add_argument("--expected-proposal-token", required=True)
    apply.set_defaults(function=command_apply)

    validate = subparsers.add_parser(
        "validate", help="verify the pinned description without changing it"
    )
    validate.add_argument("--state", required=True)
    validate.add_argument("--expected-head", required=True)
    validate.add_argument("--expected-run-id", required=True)
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
