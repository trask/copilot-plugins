#!/usr/bin/env python3
"""Deterministic mechanics for the Self Review Loop custom agent."""

from __future__ import annotations

import argparse
import ast
import base64
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
DEFAULT_PIPELINE_MAX_ITERATIONS = 2
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
HUNK_PATTERN = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@"
)
CANDIDATE_KEYS = {"path", "line", "side", "body"}
NON_FAST_FORWARD_PATTERN = re.compile(r"fast[- ]forward|divergent", re.IGNORECASE)
SHARED_STATE_REPOSITORY_PATTERN = re.compile(
    r"^(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)$"
)
SHARED_STATE_ENV = "COPILOT_PR_FLIGHT_STATE_REPO"
SHARED_STATE_CONFIG = Path(".copilot/extensions/pr-flight/state-repo.json")
SHARED_STATE_VERSION = 1
SHARED_STATE_MAX_ATTEMPTS = 3


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


def preflight_path_for(state_path: Path) -> Path:
    return state_path.parent / f"{state_path.name}.preflight.json"


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


def last_helper_activity(state: dict[str, Any]) -> str | None:
    """When this helper last wrote its state.

    Every write stamps it, so a reader can tell a stage that was active minutes
    ago from one that has been silent for an hour. That is the whole of what it
    says. It is not proof the stage is alive: the helper writes only when a
    subcommand runs, and the agent driving it can think, wait, or hang for a long
    time between two of them.
    """
    value = state.get("updated_at")
    return value if isinstance(value, str) and value else None


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


def warn_shared_state(message: str) -> None:
    print(f"warning: could not publish PR Flight state: {message}", file=sys.stderr)


def resolve_shared_state_repo() -> str | None:
    if SHARED_STATE_ENV in os.environ:
        value = os.environ[SHARED_STATE_ENV].strip()
        if not value:
            return None
    else:
        path = Path.home() / SHARED_STATE_CONFIG
        if not path.is_file():
            return None
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            warn_shared_state(f"invalid config file {path}: {error}")
            return None
        value = config.get("repository") if isinstance(config, dict) else None
        if not isinstance(value, str):
            warn_shared_state(f"invalid repository in config file {path}")
            return None
        value = value.strip()
    if not SHARED_STATE_REPOSITORY_PATTERN.fullmatch(value):
        warn_shared_state(f"invalid repository name {value!r}; expected owner/repo")
        return None
    return value


def shared_state_bytes(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def shared_state_timestamp(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise WorkflowError(f"invalid shared state timestamp {value!r}") from error
    if parsed.tzinfo is None:
        raise WorkflowError(f"invalid shared state timestamp {value!r}")
    return parsed


def gh_failure_status(process: subprocess.CompletedProcess[str]) -> int | None:
    detail = f"{process.stderr}\n{process.stdout}"
    match = re.search(r"\bHTTP\s+(\d{3})\b", detail, re.IGNORECASE)
    return int(match.group(1)) if match else None


def read_shared_state(
    state_repo: str, repository: str
) -> tuple[dict[str, Any], bytes, str | None]:
    owner, repo = repository.split("/", 1)
    endpoint = f"repos/{state_repo}/contents/prs/{owner}/{repo}.json"
    process = run(
        ["gh", "api", "--method", "GET", endpoint, "-f", "ref=main"],
        check=False,
    )
    if process.returncode != 0:
        if gh_failure_status(process) == 404:
            return (
                {
                    "version": SHARED_STATE_VERSION,
                    "repository": repository,
                    "pull_requests": {},
                },
                b"",
                None,
            )
        detail = process.stderr.strip() or process.stdout.strip() or "no output"
        raise WorkflowError(f"shared state read failed: {detail}")
    try:
        response = json.loads(process.stdout)
        encoded = response["content"]
        sha = response["sha"]
        content = base64.b64decode(encoded)
        document = json.loads(content.decode("utf-8"))
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise WorkflowError(f"shared state response is invalid: {error}") from error
    if (
        not isinstance(document, dict)
        or document.get("version") != SHARED_STATE_VERSION
        or document.get("repository") != repository
        or not isinstance(document.get("pull_requests"), dict)
        or not isinstance(sha, str)
    ):
        raise WorkflowError("shared state document has an unsupported shape")
    return document, content, sha


def merge_shared_state(
    document: dict[str, Any],
    *,
    number: int,
    section: str,
    field: str,
    value: str | None,
    updated_at: str,
) -> None:
    pull_requests = document["pull_requests"]
    key = str(number)
    entry = pull_requests.setdefault(key, {})
    if not isinstance(entry, dict):
        raise WorkflowError(f"shared state pull request entry {key} is invalid")
    existing = entry.get(section)
    if existing is not None:
        existing_updated_at = (
            existing.get("updated_at") if isinstance(existing, dict) else None
        )
        if not isinstance(existing_updated_at, str):
            raise WorkflowError(
                f"shared state pull request section {key}.{section} is invalid"
            )
        if shared_state_timestamp(existing_updated_at) >= shared_state_timestamp(
            updated_at
        ):
            return
    entry[section] = {field: value, "updated_at": updated_at}


def write_shared_state(
    state_repo: str,
    repository: str,
    number: int,
    content: bytes,
    sha: str | None,
) -> subprocess.CompletedProcess[str]:
    owner, repo = repository.split("/", 1)
    payload = {
        "message": f"Update PR Flight state for {repository}#{number}",
        "content": base64.b64encode(content).decode("ascii"),
    }
    if sha is not None:
        payload["sha"] = sha
    return run(
        [
            "gh",
            "api",
            "--method",
            "PUT",
            f"repos/{state_repo}/contents/prs/{owner}/{repo}.json",
            "--input",
            "-",
        ],
        input_text=json.dumps(payload, sort_keys=True),
        check=False,
    )


def publish_shared_state(
    pr: dict[str, Any],
    *,
    section: str,
    field: str,
    value: str | None,
    updated_at: str,
) -> None:
    state_repo = resolve_shared_state_repo()
    if state_repo is None:
        return
    repository = pr["repo_name"]
    number = pr["number"]
    try:
        for attempt in range(SHARED_STATE_MAX_ATTEMPTS):
            document, previous, sha = read_shared_state(state_repo, repository)
            merge_shared_state(
                document,
                number=number,
                section=section,
                field=field,
                value=value,
                updated_at=updated_at,
            )
            content = shared_state_bytes(document)
            if content == previous:
                return
            process = write_shared_state(
                state_repo, repository, number, content, sha
            )
            if process.returncode == 0:
                return
            status = gh_failure_status(process)
            if status in {409, 422} and attempt + 1 < SHARED_STATE_MAX_ATTEMPTS:
                continue
            detail = process.stderr.strip() or process.stdout.strip() or "no output"
            raise WorkflowError(f"shared state write failed: {detail}")
        raise WorkflowError("shared state write exhausted conflict retries")
    except (OSError, WorkflowError) as error:
        warn_shared_state(str(error))


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
        "baseRefName,baseRefOid,commits"
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
        "head_owner": head_owner["login"],
        "head_repo": head_repository["name"],
        "head_branch": metadata["headRefName"],
        "head_sha": head_sha,
        "base_branch": metadata["baseRefName"],
        "base_sha": metadata["baseRefOid"],
        "commits": commits,
    }


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
    try:
        run(command, cwd=repo_root)
    except WorkflowError as checkout_error:
        if not on_pr_branch or not NON_FAST_FORWARD_PATTERN.search(str(checkout_error)):
            raise
        reconcile_equivalent_local_head(repo_root, metadata, checkout_error)
    return on_pr_branch


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
        unique_merges = git(repo_root, "rev-list", "--merges", f"{pr_head}..{local_head}")
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
            details = []
            if unknown:
                details.append(f"unexpected keys: {', '.join(sorted(unknown))}")
            if missing:
                details.append(f"missing keys: {', '.join(sorted(missing))}")
            raise WorkflowError(
                f"candidate {index} has invalid keys ({'; '.join(details)}); "
                "expected exactly: path, line, side, body"
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
        if path not in anchors:
            raise WorkflowError(
                f"candidate {index} anchor path is not in the pinned diff: {path}; "
                f"changed paths: {', '.join(sorted(anchors))}"
            )
        accepted_lines = anchors[path][side]
        if line not in set(accepted_lines):
            if accepted_lines:
                nearest = min(accepted_lines, key=lambda value: (abs(value - line), value))
                guidance = (
                    f"nearest valid {side} line: {nearest}; "
                    f"accepted {side} lines: {', '.join(map(str, accepted_lines))}"
                )
            else:
                other_side = "LEFT" if side == "RIGHT" else "RIGHT"
                other_lines = anchors[path][other_side]
                guidance = f"{path} has no changed {side} lines"
                if other_lines:
                    guidance += (
                        f"; accepted {other_side} lines: "
                        f"{', '.join(map(str, other_lines))}"
                    )
            raise WorkflowError(
                f"candidate {index} anchor is not a changed {side} line: "
                f"{path}:{line}; {guidance}"
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


def compare_history_commits(
    history: list[dict[str, Any]], pr_commits: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    pr_commit_shas = {commit["sha"] for commit in pr_commits}
    return [
        {
            "history_id": entry["id"],
            "commit": entry["commit"],
            "in_pr_commits": entry["commit"] in pr_commit_shas,
        }
        for entry in history
        if entry.get("commit")
    ]


def pipeline_iteration_value(pipeline_iteration: Any) -> int | None:
    """Read the caller's loop counter, or nothing when it named no usable one.

    An iteration this loop cannot compare is treated as absent rather than
    guessed at, which leaves the run token to scope the budget on its own.
    """
    if isinstance(pipeline_iteration, bool) or not isinstance(pipeline_iteration, int):
        return None
    if pipeline_iteration < 1:
        return None
    return pipeline_iteration


def whole_number(value: Any, fallback: int) -> int:
    """Read a counter out of stored state, falling back when it holds anything else."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return fallback
    return value


def pipeline_scope(
    state: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any] | None:
    """Scope the iteration budget to an outer loop's position rather than a launch.

    An invocation is not a sound unit of budget. An outer loop relaunches a stage
    within one of its iterations as a matter of course, so a budget that resets on
    launch is reset by the one event it must ignore, and nothing bounds the total.

    The caller supplies the whole position and this loop never constructs any part
    of it. Nothing this loop can observe about itself, such as a new head, a
    relaunch, a re-run, or a commit it just pushed, reaches this function, so a
    reset cannot be self-triggered. That is the whole point of the budget.

    The run identity is opaque and compared only for equality, never parsed and
    never ordered. The iteration is ordered, but only against an iteration of the
    same run. An outer loop numbers its iterations from one, so a second run on the
    same pull request legitimately presents a lower number than one already
    recorded here; comparing across runs would refuse to reset again for the rest
    of the pull request's life, and this state outlives any one run.

    Within a run the comparison stays strict, so a relaunch replaying an earlier
    iteration, or repeating the current one, buys nothing.

    The two halves are not symmetric for a reader. An iteration with no run asks
    which run it belongs to and nothing can answer, so it is ignored. A run with
    no iteration still answers the question the run token exists for, whether this
    loop has seen the run before, so it scopes the budget on equality alone. The
    caller mints one token per run and repeats it on every relaunch, so that
    degrades to a coarser run-scoped budget rather than to a launch-scoped one.
    Ignoring it instead would leave the durable count untouched and refuse a pull
    request that already reached the cap for the rest of its life.

    Both budgets are expressed as baselines against the durable per-pull-request
    count, so a reset never rewrites that count. ``baseline`` moves on every
    advance and bounds one outer iteration. ``run_baseline`` moves only on a new
    run and bounds the whole run, so an advance cannot refresh the ceiling.

    Returns ``None`` when no outer loop is driving this stage, which leaves a
    standalone invocation exactly as it was. Absent arguments never read as a new
    run.
    """
    run = getattr(args, "pipeline_run", None)
    if not isinstance(run, str) or not run:
        return None
    iteration = pipeline_iteration_value(getattr(args, "pipeline_iteration", None))
    spent = int(state.get("iterations", 0))
    recorded = state.get("pipeline_budget") or {}
    if recorded.get("run") != run:
        return {
            "run": run,
            "iteration": iteration,
            "baseline": spent,
            "run_baseline": spent,
        }
    run_baseline = whole_number(recorded.get("run_baseline"), spent)
    seen = pipeline_iteration_value(recorded.get("iteration"))
    if iteration is not None and seen is not None and iteration > seen:
        return {
            "run": run,
            "iteration": iteration,
            "baseline": spent,
            "run_baseline": run_baseline,
        }
    return {
        "run": run,
        "iteration": max(
            (value for value in (seen, iteration) if value is not None), default=None
        ),
        "baseline": whole_number(recorded.get("baseline"), spent),
        "run_baseline": run_baseline,
    }


def absolute_iteration_cap(
    scope: dict[str, Any] | None, max_iterations: int, pipeline_max_iterations: Any
) -> int | None:
    """Bound the total work one outer run may spend on a pull request.

    Derived from the caller's own cap rather than hardcoded, so raising the outer
    iteration limit raises this with it. It is enforced even though the caller
    advancing its own loop at most that many times already implies it, because a
    bound that depends on a peer behaving is not a bound.

    Only the outer cap is optional. Omitting it falls back rather than removing the
    ceiling, so a caller cannot lift the bound by leaving the value out.
    """
    if scope is None:
        return None
    outer = (
        pipeline_max_iterations
        if isinstance(pipeline_max_iterations, int)
        and not isinstance(pipeline_max_iterations, bool)
        and pipeline_max_iterations > 0
        else DEFAULT_PIPELINE_MAX_ITERATIONS
    )
    return max_iterations * outer


def budget_spent(
    state: dict[str, Any], scope: dict[str, Any] | None
) -> tuple[int, int]:
    """How much of the per-iteration budget and of the whole run this PR has used.

    Without an outer loop both are the durable count itself, which is the flat
    per-pull-request cap this loop has always applied.
    """
    spent = int(state.get("iterations", 0))
    if scope is None:
        return spent, spent
    return (
        max(0, spent - whole_number(scope.get("baseline"), spent)),
        max(0, spent - whole_number(scope.get("run_baseline"), spent)),
    )


def exhausted_budget(
    state: dict[str, Any],
    scope: dict[str, Any] | None,
    max_iterations: int,
    absolute_cap: int | None,
) -> str | None:
    """Name the budget this pull request has used up, if it has used one up."""
    iteration_spent, run_spent = budget_spent(state, scope)
    if absolute_cap is not None and run_spent >= absolute_cap:
        return "absolute"
    if iteration_spent >= max_iterations:
        return "iteration"
    return None


def command_preflight(args: argparse.Namespace) -> None:
    require_tools()
    repo_root = resolve_repo_root(args.repo_root)
    target = resolve_target(args.target, repo_root)
    state_path = cli_path(args.state) if args.state else default_state_path(target)
    state = load_state(state_path) if state_path.is_file() else None
    is_new_state = state is None
    previous_clean_at_head_sha = (
        (state.get("review") or {}).get("clean_at_head_sha") if state else None
    )

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
    pr_commits = commit_provenance(repo_root, metadata["commits"])
    pr_authored_files = sorted(
        {file for commit in pr_commits for file in commit["files"]}
    )
    diff_only_files = sorted(set(anchors) - set(pr_authored_files))

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
    history_commit_presence = compare_history_commits(state["history"], pr_commits)
    history_commits_missing = sum(
        not entry["in_pr_commits"] for entry in history_commit_presence
    )
    max_iterations = getattr(args, "max_iterations", DEFAULT_MAX_ITERATIONS)
    scope = pipeline_scope(state, args)
    if scope is not None:
        state["pipeline_budget"] = scope
    absolute_cap = absolute_iteration_cap(
        scope, max_iterations, getattr(args, "pipeline_max_iterations", None)
    )
    exhausted = exhausted_budget(state, scope, max_iterations, absolute_cap)
    completed_iterations = budget_spent(state, scope)[0]
    # Numbered from the durable count rather than from the budget, because this id
    # is what `archive_review` dedupes history on and a duplicate is dropped rather
    # than recorded. Any budget that rewrote that count instead of taking a
    # baseline against it would restart the numbering and lose an entry.
    iteration = state["iterations"] + 1
    result = "max_iterations_reached" if exhausted else "ready"
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
                "pr_commits": pr_commits,
                "pr_authored_files": pr_authored_files,
                "diff_only_files": diff_only_files,
                "history_commit_presence": history_commit_presence,
                "anchors": serialize_anchors(anchors),
                "candidates": [],
                "batches": [],
            },
        }
    )
    save_state(state_path, state)
    if is_new_state or previous_clean_at_head_sha is not None:
        publish_shared_state(
            state["pr"],
            section="self_review",
            field="clean_at_head_sha",
            value=None,
            updated_at=state["updated_at"],
        )
    changed_files = sorted(anchors)
    preflight_path = preflight_path_for(state_path)
    payload = {
        "result": result,
        "state": str(state_path),
        "repo_root": str(repo_root),
        "pr": metadata,
        "head_sha": metadata["head_sha"],
        "diff_path": str(diff_path),
        "changed_files": changed_files,
        "pr_commits": pr_commits,
        "pr_authored_files": pr_authored_files,
        "diff_only_files": diff_only_files,
        "history": state["history"],
        "history_commit_presence": history_commit_presence,
        "iteration": iteration,
        "max_iterations": max_iterations,
        "completed_iterations": completed_iterations,
        "absolute_cap": absolute_cap,
        "budget_exhausted": exhausted,
        "pipeline_run": None if scope is None else scope["run"],
        "pipeline_iteration": None if scope is None else scope["iteration"],
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
            },
            "head_sha": metadata["head_sha"],
            "diff_path": str(diff_path),
            "diff_bytes": len(diff_text.encode("utf-8")),
            "counts": {
                "changed_files": len(changed_files),
                "diff_only_files": len(diff_only_files),
                "history": len(state["history"]),
                "history_commits_missing": history_commits_missing,
                "pr_authored_files": len(pr_authored_files),
                "pr_commits": len(pr_commits),
            },
            "iteration": iteration,
            "max_iterations": max_iterations,
            "completed_iterations": completed_iterations,
            "absolute_cap": absolute_cap,
            "budget_exhausted": exhausted,
            "pipeline_run": None if scope is None else scope["run"],
            "pipeline_iteration": None if scope is None else scope["iteration"],
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
    rationale_file = getattr(args, "rationale_file", None)
    rationale = (
        load_text_input(rationale_file, "drop rationale")
        if rationale_file
        else args.rationale.strip()
    )
    if not rationale:
        raise WorkflowError("drop rationale must not be empty")
    for candidate in candidates:
        candidate.update({"status": "dropped", "rationale": rationale})
    save_state(path, state)
    emit(
        {
            "result": "dropped",
            "state": str(path),
            "candidate_ids": args.candidates,
            "rationale": rationale,
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
    previous_clean_at_head_sha = review.get("clean_at_head_sha")
    review["clean_at_head_sha"] = review["head_sha"]
    save_state(path, state)
    if previous_clean_at_head_sha != review["clean_at_head_sha"]:
        publish_shared_state(
            state["pr"],
            section="self_review",
            field="clean_at_head_sha",
            value=review["clean_at_head_sha"],
            updated_at=state["updated_at"],
        )
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
    pushed_head = wait_for_remote_head(
        pr["head_owner"], pr["head_repo"], pr["head_branch"], local_head
    )
    if pushed_head != local_head:
        raise WorkflowError(f"fork ref mismatch: local {local_head}, remote {pushed_head}")
    pr_head = metadata_for(parse_target(pr["pr_url"]))["head_sha"]
    if pr_head != local_head:
        time.sleep(PR_HEAD_LAG_RETRY_DELAY)
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


def recorded_clean_at_head_sha(state: dict[str, Any]) -> str | None:
    """Return the clean-at-head SHA this state records, or None when it records none.

    `resolve` is the only command that writes this pair, and `preflight` replaces
    the whole review when the next iteration starts, so the pair is the single
    durable fact that says a review came out clean at a known head.
    """

    review = state.get("review")
    if not isinstance(review, dict) or review.get("outcome") != "clean":
        return None
    value = review.get("clean_at_head_sha")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def stage_outcome(state: dict[str, Any]) -> str | None:
    """Name this run's ending in the vocabulary an orchestrator records.

    `resolve` is the only command that records an ending, so `cleared` is the
    only word this state can support, and it is read straight off the same
    clean-at-head record a reader consults for the review's cleanness. This says
    how the run ended. It never says whether the review is clean.

    Returning `None` means this state supports no claim about an ending, and the
    field is then left out so a reader sees an absent answer rather than a
    manufactured one. State exists from the moment `preflight` writes it, so a
    run killed at any point leaves exactly the same state as a run still in
    flight. Nothing in that state distinguishes them, so neither is `no_progress`
    and neither is `escalated`.

    A blocked batch and a state at its iteration cap are conditions that persist
    across runs, not endings that happened. Both outlive the run that caused
    them, so a run that never started would inherit them and answer for a run it
    never made. The agent that watched the run reports those endings itself,
    through the orchestrator's own `finish`.

    A reader is entitled to take any value it finds at face value, so a value
    this function cannot support must not appear at all.
    """

    if recorded_clean_at_head_sha(state) is not None:
        return "cleared"
    return None


def stage_outcome_fields(state: dict[str, Any]) -> dict[str, str]:
    """Carry the stage outcome only when the state supports naming one."""
    outcome = stage_outcome(state)
    return {"stage_outcome": outcome} if outcome else {}


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
    pr = state["pr"]
    review = state.get("review")
    history = state.get("history") or []
    payload = {
        "result": "ready",
        "state": str(path),
        "pr": pr,
        "review": review,
        "history": history,
        **stage_outcome_fields(state),
        "iterations": int(state.get("iterations", 0)),
        "last_helper_activity": last_helper_activity(state),
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
            "review": None
            if review is None
            else {
                "id": review.get("id"),
                "status": review.get("status"),
                "iteration": review.get("iteration"),
                "head_sha": review.get("head_sha"),
                "diff_path": review.get("diff_path"),
                "outcome": review.get("outcome"),
                "clean_at_head_sha": review.get("clean_at_head_sha"),
                "candidate_statuses": count_by_status(review.get("candidates")),
                "batch_statuses": count_by_status(review.get("batches")),
            },
            "counts": {
                "batches": len(((review or {}).get("batches")) or []),
                "candidates": len(((review or {}).get("candidates")) or []),
                "changed_files": len(((review or {}).get("anchors")) or {}),
                "diff_only_files": len(((review or {}).get("diff_only_files")) or []),
                "history": len(history),
                "pr_commits": len(((review or {}).get("pr_commits")) or []),
            },
            **stage_outcome_fields(state),
            "iterations": int(state.get("iterations", 0)),
            "last_helper_activity": last_helper_activity(state),
        }
    )


def command_cleanup(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    load_state(path)
    path.unlink()
    diff_path_for(path).unlink(missing_ok=True)
    preflight_path_for(path).unlink(missing_ok=True)
    status_path_for(path).unlink(missing_ok=True)
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
    preflight.add_argument(
        "--pipeline-run",
        help=(
            "opaque identifier for one outer run, compared only for equality; "
            "a different one starts both budgets over"
        ),
    )
    preflight.add_argument(
        "--pipeline-iteration",
        type=int,
        help=(
            "the orchestrator's own loop counter; a higher one within the same run "
            "refreshes the per-iteration budget"
        ),
    )
    preflight.add_argument(
        "--pipeline-max-iterations",
        type=int,
        help="the orchestrator's own iteration cap, which derives the ceiling",
    )
    preflight.set_defaults(function=command_preflight)

    candidates = subparsers.add_parser(
        "candidates", help="register this iteration's candidate findings"
    )
    candidates.add_argument("--state", required=True)
    candidates.add_argument(
        "--input",
        required=True,
        help=(
            "JSON array file, or - for standard input; each object must contain "
            "exactly path (string), line (integer), side (LEFT or RIGHT), and "
            "body (string)"
        ),
    )
    candidates.set_defaults(function=command_candidates)

    drop = subparsers.add_parser("drop", help="record evaluator-rejected candidates")
    drop.add_argument("--state", required=True)
    drop.add_argument("--candidates", type=int, nargs="+", required=True)
    drop_rationale = drop.add_mutually_exclusive_group(required=True)
    drop_rationale.add_argument("--rationale")
    drop_rationale.add_argument(
        "--rationale-file",
        help="UTF-8 rationale file, or - for standard input",
    )
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
