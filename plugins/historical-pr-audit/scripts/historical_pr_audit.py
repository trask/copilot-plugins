#!/usr/bin/env python3
"""Deterministic mechanics for the Historical PR Audit custom agent."""

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
import time
from typing import Any, Iterable
import uuid


STATE_VERSION = 1
DEFAULT_MAX_ITERATIONS = 5
REMOTE_REF_LAG_RETRY_DELAYS = (1, 2, 4)
IS_WINDOWS = os.name == "nt"
AUDIT_BRANCH_PREFIX = "trask-pr-audit-"
AUDIT_BRANCH_PATTERN = re.compile(rf"^{re.escape(AUDIT_BRANCH_PREFIX)}(?P<number>\d+)$")
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
GITHUB_PR_DIFF = "github_pr_diff"
CUMULATIVE_GIT_DIFF = "cumulative_git_diff"

# Five commit identities travel through this workflow and none of them is
# interchangeable with another:
#
# - the pinned original head, `original.head_sha`, is the commit the pull
#   request merged from. It never moves for the whole run.
# - the iteration head, `audit.iteration_head_sha`, is the commit this iteration
#   started from. `publish` measures every new commit from it.
# - the local head is whatever `git rev-parse HEAD` reports right now.
# - the published head, `audit.published_head_sha`, is the commit `publish`
#   pushed and verified on the remote audit branch.
# - the clean head, `audit.clean_at_head_sha`, is the commit `resolve` recorded
#   as the one a whole pass found nothing in.

# Every GitHub call this helper makes is an explicit read. The audit reads a
# merged pull request and must leave it exactly as it found it, so the allowlist
# names the two read subcommands plus `api`, and `api` is then checked for a
# mutating HTTP method or a GraphQL mutation.
GH_READ_ONLY_COMMANDS = {("pr", "view"), ("pr", "diff"), ("api",)}
GH_MUTATING_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
GH_API_FIELD_FLAGS = {"-f", "-F", "--field", "--raw-field", "--input"}
GRAPHQL_MUTATION_PATTERN = re.compile(r"\bmutation\b", re.IGNORECASE)


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


def git_try(repo_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(repo_root), *arguments], check=False)


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)


def gh_method(arguments: list[str]) -> str:
    """Name the HTTP method a `gh api` invocation would use."""
    implicit_post = False
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"--method", "-X"}:
            return arguments[index + 1].upper() if index + 1 < len(arguments) else ""
        if argument.startswith("--method="):
            return argument.split("=", 1)[1].upper()
        if argument.startswith("-X") and argument != "-X":
            return argument[2:].upper()
        if (
            argument in GH_API_FIELD_FLAGS
            or argument.startswith(("-f=", "-F=", "--field=", "--raw-field=", "--input="))
            or (
                argument.startswith(("-f", "-F"))
                and argument not in {"-f", "-F"}
            )
        ):
            implicit_post = True
        index += 1
    return "POST" if implicit_post else "GET"


def graphql_queries(arguments: list[str]) -> list[str]:
    """Return only GraphQL query field values from a `gh api graphql` call."""
    queries: list[str] = []
    for index, argument in enumerate(arguments):
        value = None
        if argument in {"-f", "-F", "--field", "--raw-field"}:
            if index + 1 < len(arguments):
                value = arguments[index + 1]
        else:
            for prefix in (
                "-fquery=",
                "-Fquery=",
                "--field=query=",
                "--raw-field=query=",
            ):
                if argument.startswith(prefix):
                    queries.append(argument[len(prefix) :])
                    break
        if isinstance(value, str) and value.startswith("query="):
            queries.append(value.removeprefix("query="))
    return queries


def require_read_only_gh(arguments: list[str]) -> None:
    """Refuse any GitHub call that could change the audited pull request.

    The audit reads history. A single mutating call would rewrite the record it
    exists to examine, so the check sits at the one place every call passes
    through rather than in each caller.
    """
    words = [argument for argument in arguments if not argument.startswith("-")]
    prefix = tuple(words[:2])
    if prefix not in GH_READ_ONLY_COMMANDS and prefix[:1] not in GH_READ_ONLY_COMMANDS:
        raise WorkflowError(
            f"refusing a GitHub command outside the read-only allowlist: gh {' '.join(words[:2])}"
        )
    if prefix[:1] != ("api",):
        return
    method = gh_method(arguments)
    is_graphql = prefix == ("api", "graphql")
    if method in GH_MUTATING_METHODS and not (is_graphql and method == "POST"):
        raise WorkflowError(
            f"refusing a mutating GitHub request: gh api --method {method}"
        )
    if not is_graphql:
        return
    queries = graphql_queries(arguments)
    if not queries:
        raise WorkflowError("refusing a GraphQL request without an explicit query")
    for query in queries:
        if GRAPHQL_MUTATION_PATTERN.search(query):
            raise WorkflowError("refusing a GraphQL mutation")


def gh(
    arguments: list[str], *, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    require_read_only_gh(arguments)
    return run(["gh", *arguments], cwd=cwd, check=check)


def gh_json(arguments: list[str]) -> Any:
    output = gh(arguments).stdout
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


def audit_branch_name(number: int) -> str:
    return f"{AUDIT_BRANCH_PREFIX}{number}"


def audit_branch_number(branch: str) -> int | None:
    match = AUDIT_BRANCH_PATTERN.fullmatch(branch or "")
    return int(match.group("number")) if match else None


def default_state_path(target: dict[str, Any]) -> Path:
    name = f"{target['owner']}--{target['repo']}--{target['number']}.json"
    return Path.home() / ".copilot" / "run" / "historical-pr-audit" / name


def diff_path_for(state_path: Path) -> Path:
    return state_path.parent / f"{state_path.name}.diff"


def preflight_path_for(state_path: Path) -> Path:
    return state_path.parent / f"{state_path.name}.preflight.json"


def status_path_for(state_path: Path) -> Path:
    return state_path.parent / f"{state_path.name}.status.json"


def context_path_for(state_path: Path) -> Path:
    return state_path.parent / f"{state_path.name}.context.json"


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


def find_remote(repo_root: Path, owner: str, repo: str, *, push: bool) -> str:
    expected = f"{owner}/{repo}".lower()
    for remote in git(repo_root, "remote").splitlines():
        arguments = ["remote", "get-url"] + (["--push"] if push else []) + [remote]
        parsed = github_repo_from_remote(git(repo_root, *arguments))
        if parsed and parsed.lower() == expected:
            return remote
    raise WorkflowError(f"no git remote points to {owner}/{repo}")


def repo_name_from_remotes(repo_root: Path) -> str:
    for remote in ["origin", *git(repo_root, "remote").splitlines()]:
        result = git_try(repo_root, "remote", "get-url", remote)
        if result.returncode != 0:
            continue
        parsed = github_repo_from_remote(result.stdout.strip())
        if parsed:
            return parsed
    raise WorkflowError("no git remote points at a GitHub repository")


def merged_metadata_for(target: dict[str, Any]) -> dict[str, Any]:
    """Pin the merged pull request's own base and head commits.

    ``baseRefOid`` and ``headRefOid`` are exactly what this audit wants. They
    name the commits the pull request was merged from, and neither follows the
    branch afterwards. Reading a branch tip instead would silently swap the
    historical snapshot for whatever the repository looks like today.
    """
    fields = (
        "number,title,url,state,mergedAt,mergeCommit,baseRefName,baseRefOid,"
        "headRefName,headRefOid,headRepositoryOwner,headRepository,commits"
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
    state = metadata.get("state")
    if state != "MERGED":
        raise WorkflowError(
            f"this audit runs only on a merged pull request; "
            f"{resolved['pr_url']} is {state or 'in an unknown state'}"
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
    base_branch = metadata.get("baseRefName")
    if not isinstance(base_branch, str) or not base_branch:
        raise WorkflowError("resolved PR metadata has no base branch")
    head_branch = metadata.get("headRefName")
    if not isinstance(head_branch, str) or not head_branch:
        raise WorkflowError("resolved PR metadata has no head branch")
    merge_commit = metadata.get("mergeCommit")
    return {
        "number": target["number"],
        "title": title.strip(),
        "pr_url": resolved["pr_url"],
        "repo_name": resolved["repo_name"],
        "state": state,
        "merged_at": metadata.get("mergedAt"),
        "merge_commit": merge_commit.get("oid")
        if isinstance(merge_commit, dict)
        else None,
        "upstream_owner": resolved["owner"],
        "upstream_repo": resolved["repo"],
        "head_owner": optional_login(metadata.get("headRepositoryOwner")),
        "head_repo": optional_name(metadata.get("headRepository")),
        "head_branch": head_branch,
        "head_sha": head_sha,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "commits": normalized_commits(metadata.get("commits")),
    }


def optional_login(value: Any) -> str | None:
    """Name the head repository owner, which a deleted fork no longer has."""
    login = value.get("login") if isinstance(value, dict) else None
    return login if isinstance(login, str) and login else None


def optional_name(value: Any) -> str | None:
    name = value.get("name") if isinstance(value, dict) else None
    return name if isinstance(name, str) and name else None


def normalized_commits(raw_commits: Any) -> list[dict[str, str]]:
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
    return commits


REVIEW_THREADS_QUERY = """
query($owner:String!,$repo:String!,$number:Int!,$after:String){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$number){
      reviewThreads(first:50,after:$after){
        pageInfo{hasNextPage endCursor}
        nodes{
          isResolved isOutdated path line originalLine
          comments(first:50){nodes{author{login} body createdAt}}
        }
      }
    }
  }
}
"""


def review_threads_for(target: dict[str, Any]) -> list[dict[str, Any]]:
    after: str | None = None
    threads: list[dict[str, Any]] = []
    while True:
        payload = graphql(
            REVIEW_THREADS_QUERY,
            {
                "owner": target["owner"],
                "repo": target["repo"],
                "number": target["number"],
                "after": after,
            },
        )
        repository = (payload.get("data") or {}).get("repository") or {}
        pull_request = repository.get("pullRequest") or {}
        connection = pull_request.get("reviewThreads")
        if connection is None:
            return threads
        threads.extend(connection.get("nodes") or [])
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return threads
        after = page.get("endCursor")


def original_context_for(target: dict[str, Any]) -> dict[str, Any]:
    """Capture the discussion the merged pull request carried when it merged."""
    payload = gh_json(
        [
            "pr",
            "view",
            target["pr_url"],
            "--repo",
            target["repo_name"],
            "--json",
            "body,closingIssuesReferences,comments,reviews",
        ]
    )
    if not isinstance(payload, dict):
        raise WorkflowError("gh pr view did not return PR discussion")
    return {
        "body": payload.get("body") or "",
        "closing_issues": payload.get("closingIssuesReferences") or [],
        "issue_comments": payload.get("comments") or [],
        "reviews": payload.get("reviews") or [],
        "review_threads": review_threads_for(target),
    }


def commit_paths(repo_root: Path, sha: str) -> list[str]:
    """List every repository path one commit touches, sorted and deduplicated.

    `-m` makes a merge commit report its changes against each parent instead of
    reporting nothing, and `--root` does the same for a repository's first
    commit, so no commit answers with an empty list it does not deserve.
    """
    output = run(
        [
            "git",
            "-C",
            str(repo_root),
            "-c",
            "core.quotePath=false",
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-z",
            "-r",
            "-m",
            sha,
        ]
    ).stdout
    return sorted({path for path in output.split("\0") if path})


def commit_provenance(
    repo_root: Path, commits: list[dict[str, str]]
) -> list[dict[str, Any]]:
    return [
        {**commit, "files": commit_paths(repo_root, commit["sha"])}
        for commit in commits
    ]


def audit_commits(repo_root: Path, original_head_sha: str) -> list[dict[str, str]]:
    """List the commits this audit added on top of the original head, oldest first."""
    output = git(
        repo_root,
        "log",
        "--reverse",
        "--format=%H%x1f%s",
        f"{original_head_sha}..HEAD",
    )
    commits = []
    for line in output.splitlines():
        if not line.strip():
            continue
        sha, _, message = line.partition("\x1f")
        commits.append({"sha": sha.strip(), "message": message.strip()})
    return commits


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


def fetch_pr_diff(pr: dict[str, Any]) -> str:
    """Read the diff GitHub reports for the merged pull request itself."""
    return gh(["pr", "diff", pr["pr_url"], "--repo", pr["repo_name"]]).stdout


def cumulative_diff(repo_root: Path, base_sha: str, head_sha: str) -> str:
    """Diff the original merge base against the current audit head.

    The three-dot form matches the pull request diff even when the base advanced
    after the head branch split. Both named commits are pinned, so no branch
    that moved after the merge can reach this changeset.
    """
    return run(
        [
            "git",
            "-C",
            str(repo_root),
            "diff",
            "--no-color",
            f"{base_sha}...{head_sha}",
        ]
    ).stdout


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


def active_audit(state: dict[str, Any]) -> dict[str, Any]:
    audit = state.get("audit")
    if not audit:
        raise WorkflowError("state has no audit")
    if audit.get("status") == "published":
        raise WorkflowError(
            "this iteration is already published; run preflight to start the next one"
        )
    return audit


def find_candidates(audit: dict[str, Any], ids: Iterable[int]) -> list[dict[str, Any]]:
    by_id = {candidate["id"]: candidate for candidate in audit["candidates"]}
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


def archive_audit(state: dict[str, Any]) -> None:
    """Fold a finished iteration's resolved candidates into the carried-forward history.

    Candidates an interrupted run never resolved are deliberately left out so a later
    audit can raise them again.
    """
    audit = state.get("audit")
    if not audit:
        return
    history = state.setdefault("history", [])
    recorded = {entry["id"] for entry in history}
    for candidate in audit.get("candidates") or []:
        if candidate["id"] in recorded or candidate.get("status") not in {
            "handled",
            "dropped",
        }:
            continue
        history.append(
            {
                "id": candidate["id"],
                "iteration": audit.get("iteration"),
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
    history: list[dict[str, Any]], commits: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    shas = {commit["sha"] for commit in commits}
    return [
        {
            "history_id": entry["id"],
            "commit": entry["commit"],
            "in_audit_commits": entry["commit"] in shas,
        }
        for entry in history
        if entry.get("commit")
    ]


def require_clean_worktree(repo_root: Path) -> None:
    dirty = git(repo_root, "status", "--porcelain=v1")
    if dirty:
        raise WorkflowError(f"worktree is not clean:\n{dirty}")


def current_branch(repo_root: Path) -> str:
    return git(repo_root, "branch", "--show-current")


def local_branch_exists(repo_root: Path, branch: str) -> bool:
    return (
        git_try(
            repo_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
        ).returncode
        == 0
    )


def commit_present(repo_root: Path, sha: str) -> bool:
    return git_try(repo_root, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    return (
        git_try(repo_root, "merge-base", "--is-ancestor", ancestor, descendant).returncode
        == 0
    )


def remote_head(owner: str, repo: str, branch: str) -> str | None:
    process = gh(["api", f"repos/{owner}/{repo}/git/ref/heads/{branch}"], check=False)
    if process.returncode != 0 and "HTTP 404" in f"{process.stderr}{process.stdout}":
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


def starting_point_reference(
    repo_root: Path, *, remote: str, branch: str, base_branch: str
) -> str:
    """Name the remote reference a fresh session branch was created from.

    A fresh app worktree branches from the default branch and pushes nothing, so
    the proof that it holds no unique work has to run against a remote reference
    rather than against an upstream the branch does not have yet.
    """
    candidates: list[str] = []
    configured_remote = git_try(
        repo_root, "config", "--get", f"branch.{branch}.remote"
    )
    configured_merge = git_try(repo_root, "config", "--get", f"branch.{branch}.merge")
    if configured_remote.returncode == 0 and configured_merge.returncode == 0:
        merge_ref = configured_merge.stdout.strip()
        prefix = "refs/heads/"
        if merge_ref.startswith(prefix) and merge_ref != prefix:
            candidates.append(
                f"{configured_remote.stdout.strip()}/{merge_ref[len(prefix):]}"
            )
    symbolic = git_try(
        repo_root, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD"
    )
    if symbolic.returncode == 0 and symbolic.stdout.strip():
        candidates.append(symbolic.stdout.strip())
    candidates.append(f"{remote}/{base_branch}")
    for reference in candidates:
        if (
            git_try(
                repo_root, "rev-parse", "--verify", "--quiet", f"{reference}^{{commit}}"
            ).returncode
            == 0
        ):
            return reference
    raise WorkflowError(
        "cannot prove the starting branch holds no unique work: none of "
        f"{', '.join(candidates)} resolves to a commit"
    )


def unique_local_work(repo_root: Path, reference: str) -> list[str]:
    """List the commits this branch holds that the remote reference does not.

    A commit whose patch already landed upstream is not unique work, so the
    equivalence check runs before anything is refused.
    """
    revisions = [
        line for line in git(repo_root, "rev-list", f"{reference}..HEAD").splitlines()
        if line
    ]
    if not revisions:
        return []
    cherry = git(repo_root, "cherry", reference, "HEAD")
    return [
        line[2:].strip()
        for line in cherry.splitlines()
        if line.startswith("+ ") and line[2:].strip()
    ]


def fetch_commit(
    repo_root: Path, remote: str, sha: str, fallback_ref: str | None
) -> None:
    """Bring one historical commit into this checkout by its exact SHA.

    The head branch of a merged pull request is often deleted, force-pushed, or
    restacked, so fetching a branch name can bring back a commit that is not the
    one that merged. The fallback reads the pull request's own ref, which GitHub
    keeps for the exact merged head.
    """
    if commit_present(repo_root, sha):
        return
    fetch = git_try(repo_root, "fetch", "--no-tags", remote, sha)
    if fetch.returncode != 0 and fallback_ref:
        fetch = git_try(repo_root, "fetch", "--no-tags", remote, fallback_ref)
    if fetch.returncode != 0:
        detail = fetch.stderr.strip() or fetch.stdout.strip() or "no output"
        raise WorkflowError(f"could not fetch commit {sha} from {remote}: {detail}")
    if not commit_present(repo_root, sha):
        raise WorkflowError(f"commit {sha} is missing after fetching from {remote}")


def realign_branch(repo_root: Path, branch: str, sha: str) -> None:
    """Move a proven-empty branch onto the historical head without a reset.

    Detaching first is a plain checkout that git itself refuses when it would
    discard local modifications. The branch then moves while nothing has it
    checked out, so no working tree is rewritten out from under anyone.
    """
    git(repo_root, "switch", "--detach", sha)
    git(repo_root, "branch", "--force", branch, sha)
    git(repo_root, "switch", branch)


def resume_head_for(state: dict[str, Any]) -> str:
    """Name the commit a next-iteration preflight is allowed to resume from.

    Only a published iteration leaves a head this audit can prove it created,
    so every other stored state is refused rather than adopted. Without that
    proof any local branch whose tip happens to contain the pinned original
    head, such as a renamed live default branch, would look like the audit.
    """
    audit = state.get("audit")
    if not isinstance(audit, dict):
        raise WorkflowError(
            "this state records no previous iteration to resume from; delete the "
            "state file and start the audit again"
        )
    status = audit.get("status")
    if status != "published":
        raise WorkflowError(
            f"the previous iteration is {status or 'in an unknown state'!r}, not "
            "'published'; an iteration that never published leaves no head this "
            "audit can prove it created, so this run refuses to resume from it"
        )
    published = audit.get("published_head_sha")
    if not isinstance(published, str) or not published.strip():
        raise WorkflowError(
            "the previous iteration records no published head, so this run refuses "
            "to resume from it"
        )
    return published.strip()


def prepare_audit_branch(
    repo_root: Path,
    *,
    pr: dict[str, Any],
    audit_branch: str,
    original_head_sha: str,
    original_base_sha: str,
    resuming: bool,
    expected_resume_head: str | None = None,
) -> dict[str, Any]:
    """Put this worktree on the audit branch, at or above the original head."""
    branch = current_branch(repo_root)
    if not branch:
        raise WorkflowError(
            "this worktree has a detached HEAD; check out "
            f"{audit_branch!r} before preflight"
        )
    if branch in {pr["head_branch"], pr["base_branch"]}:
        raise WorkflowError(
            f"refusing to run on branch {branch!r}: the audit never uses the pull "
            "request's own head branch or its base branch"
        )
    if branch != audit_branch:
        if local_branch_exists(repo_root, audit_branch):
            raise WorkflowError(
                f"branch {audit_branch!r} already exists locally but is not checked "
                f"out here (current branch {branch!r}); this audit refuses to reuse "
                "or move a branch another checkout may hold"
            )
        raise WorkflowError(
            f"current branch is {branch!r}; rename this session's branch so it is "
            f"exactly {audit_branch!r}, then run preflight again"
        )

    local_head = git(repo_root, "rev-parse", "HEAD")
    if resuming:
        if not expected_resume_head:
            raise WorkflowError(
                f"refusing to resume {audit_branch!r}: no published head from the "
                "previous iteration was supplied"
            )
        if local_head != expected_resume_head:
            raise WorkflowError(
                f"branch {audit_branch!r} is at {local_head}, not the "
                f"{expected_resume_head} the previous iteration published; this "
                "audit refuses to adopt a branch it cannot prove it created"
            )
        if not commit_present(repo_root, original_head_sha) or not is_ancestor(
            repo_root, original_head_sha, local_head
        ):
            raise WorkflowError(
                f"branch {audit_branch!r} at {local_head} no longer contains the "
                f"pinned original head {original_head_sha}"
            )
        published_remote_head = remote_head(
            pr["upstream_owner"], pr["upstream_repo"], audit_branch
        )
        if published_remote_head != expected_resume_head:
            raise WorkflowError(
                f"remote branch {audit_branch!r} is at "
                f"{published_remote_head or 'no commit'}, not the "
                f"{expected_resume_head} the previous iteration published; this "
                "audit refuses to resume over a branch that moved"
            )
        return {
            "branch": audit_branch,
            "branch_action": "resumed",
            "local_head": local_head,
            "reference": None,
        }

    remote = find_remote(repo_root, pr["upstream_owner"], pr["upstream_repo"], push=False)
    existing_remote_head = remote_head(
        pr["upstream_owner"], pr["upstream_repo"], audit_branch
    )
    if existing_remote_head is not None:
        raise WorkflowError(
            f"remote branch {audit_branch!r} already exists at {existing_remote_head}; "
            "an earlier audit left it behind, so this run refuses to start over it"
        )
    reference = starting_point_reference(
        repo_root, remote=remote, branch=branch, base_branch=pr["base_branch"]
    )
    unique = unique_local_work(repo_root, reference)
    if unique:
        raise WorkflowError(
            f"branch {branch!r} holds unique work that {reference} does not "
            f"({', '.join(unique)}); this audit refuses to move a branch that "
            "carries commits of its own"
        )
    fetch_commit(
        repo_root,
        remote,
        original_head_sha,
        f"refs/pull/{pr['number']}/head",
    )
    fetch_commit(
        repo_root,
        remote,
        original_base_sha,
        f"refs/heads/{pr['base_branch']}",
    )
    action = "already_at_original_head"
    if local_head != original_head_sha:
        realign_branch(repo_root, audit_branch, original_head_sha)
        action = "realigned"
    settled_branch = current_branch(repo_root)
    settled_head = git(repo_root, "rev-parse", "HEAD")
    if settled_branch != audit_branch or settled_head != original_head_sha:
        raise WorkflowError(
            f"audit branch preparation left {settled_branch!r} at {settled_head}; "
            f"expected {audit_branch!r} at {original_head_sha}"
        )
    return {
        "branch": audit_branch,
        "branch_action": action,
        "local_head": settled_head,
        "reference": reference,
    }


def stored_audit_summary(state: dict[str, Any]) -> dict[str, Any] | None:
    """Summarize the stored audit compactly enough to report a stop without reading state."""
    audit = state.get("audit")
    if not isinstance(audit, dict):
        return None
    return {
        "id": audit.get("id"),
        "status": audit.get("status"),
        "iteration": audit.get("iteration"),
        "branch": audit.get("branch"),
        "iteration_head_sha": audit.get("iteration_head_sha"),
        "published_head_sha": audit.get("published_head_sha"),
        "outcome": audit.get("outcome"),
        "clean_at_head_sha": audit.get("clean_at_head_sha"),
        "candidate_statuses": count_by_status(audit.get("candidates")),
        "batch_statuses": count_by_status(audit.get("batches")),
    }


def whole_number(value: Any, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return fallback
    return value


def invocation_scope(
    state: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any] | None:
    """Scope a standalone budget to one explicit user invocation."""
    if getattr(args, "new_invocation", False):
        spent = int(state.get("iterations", 0))
        return {
            "run": uuid.uuid4().hex,
            "baseline": spent,
        }
    run = getattr(args, "invocation_run", None)
    if not isinstance(run, str) or not run:
        return None
    recorded = state.get("invocation_budget")
    if not isinstance(recorded, dict) or recorded.get("run") != run:
        raise WorkflowError(
            "invocation run does not match the active invocation; start a new "
            "explicit invocation with --new-invocation"
        )
    spent = int(state.get("iterations", 0))
    return {
        "run": run,
        "baseline": whole_number(recorded.get("baseline"), spent),
    }


def invocation_iterations(
    state: dict[str, Any], scope: dict[str, Any] | None
) -> int:
    spent = int(state.get("iterations", 0))
    if scope is None:
        return spent
    return max(0, spent - whole_number(scope.get("baseline"), spent))


def stored_stop_envelope(
    result: str, state_path: Path, state: dict[str, Any], max_iterations: int
) -> dict[str, Any]:
    """Carry enough of the stored state for the agent to report a stop it caused.

    A stop reads state and changes nothing: no GitHub call, no branch move, no
    archived iteration, and no rewritten result file. Everything the final
    response needs therefore has to travel in this envelope.
    """
    pr = state.get("pr") or {}
    iterations = int(state.get("iterations", 0))
    invocation = (
        state.get("invocation_budget")
        if state.get("budget_scope") == "invocation"
        else None
    )
    return {
        "result": result,
        "state": str(state_path),
        "repo_root": state.get("repo_root"),
        "context_path": state.get("context_path"),
        "pr": {
            "number": pr.get("number"),
            "title": pr.get("title"),
            "pr_url": pr.get("pr_url"),
            "repo_name": pr.get("repo_name"),
            "state": pr.get("state"),
            "merged_at": pr.get("merged_at"),
            "head_branch": pr.get("head_branch"),
            "base_branch": pr.get("base_branch"),
        },
        "audit_branch": state.get("audit_branch"),
        "audit": stored_audit_summary(state),
        "original_head_sha": (state.get("original") or {}).get("head_sha"),
        "history": state.get("history") or [],
        "local_validation": state.get("local_validation") or [],
        **stage_outcome_fields(state),
        "iterations": iterations,
        "completed_iterations": invocation_iterations(state, invocation),
        "max_iterations": max_iterations,
        "budget_scope": state.get("budget_scope", "lifetime"),
        "invocation_run": (
            invocation.get("run") if isinstance(invocation, dict) else None
        ),
        "pushed": False,
    }


def command_preflight(args: argparse.Namespace) -> None:
    require_tools()
    repo_root = resolve_repo_root(args.repo_root)
    target = parse_target(args.target)
    state_path = cli_path(args.state) if args.state else default_state_path(target)
    state = load_state(state_path) if state_path.is_file() else None
    audit_branch = audit_branch_name(target["number"])
    context_path = context_path_for(state_path)
    max_iterations = getattr(args, "max_iterations", DEFAULT_MAX_ITERATIONS)
    budget_state = state if state is not None else {"iterations": 0}
    invocation = invocation_scope(budget_state, args)
    if (
        state is not None
        and invocation is None
        and state.get("budget_scope") == "invocation"
        and isinstance(state.get("invocation_budget"), dict)
    ):
        raise WorkflowError(
            "an explicit standalone invocation is active; pass its --invocation-run "
            "token to continue it, or use --new-invocation for a new user invocation"
        )
    if invocation is not None:
        budget_state["invocation_budget"] = invocation
        budget_state["budget_scope"] = "invocation"

    # Both stops read stored state and answer from it alone. Anything below this
    # point reads GitHub, moves the audit branch, archives the previous
    # iteration, or overwrites a result file, and a run that must stop has no
    # business doing any of it.
    if state is not None:
        clean_head = recorded_clean_at_head_sha(state)
        if clean_head is not None:
            emit(
                {
                    **stored_stop_envelope(
                        "already_complete", state_path, state, max_iterations
                    ),
                    "clean_at_head_sha": clean_head,
                }
            )
            return
        if invocation_iterations(state, invocation) >= max_iterations:
            emit(
                stored_stop_envelope(
                    "max_iterations_reached", state_path, state, max_iterations
                )
            )
            return

    resuming = bool(state and state.get("original"))
    expected_resume_head = resume_head_for(state) if resuming else None

    require_clean_worktree(repo_root)

    metadata = merged_metadata_for(target)
    if resuming:
        original = state["original"]
    else:
        context = original_context_for(target)
        diff_text = fetch_pr_diff(metadata)
        refreshed = merged_metadata_for(target)
        if (
            refreshed["head_sha"] != metadata["head_sha"]
            or refreshed["base_sha"] != metadata["base_sha"]
        ):
            raise WorkflowError(
                "head_moved: the merged snapshot changed while the pull request diff "
                f"was captured: expected {metadata['base_sha']}..{metadata['head_sha']}, "
                f"got {refreshed['base_sha']}..{refreshed['head_sha']}"
            )
        original = {
            "base_sha": metadata["base_sha"],
            "head_sha": metadata["head_sha"],
            "base_branch": metadata["base_branch"],
            "head_branch": metadata["head_branch"],
            "merge_commit": metadata["merge_commit"],
            "merged_at": metadata["merged_at"],
            "captured_at": utc_now(),
            "commits": metadata["commits"],
        }

    branch_state = prepare_audit_branch(
        repo_root,
        pr=metadata,
        audit_branch=audit_branch,
        original_head_sha=original["head_sha"],
        original_base_sha=original["base_sha"],
        resuming=resuming,
        expected_resume_head=expected_resume_head,
    )
    local_head = branch_state["local_head"]

    if resuming:
        diff_text = cumulative_diff(repo_root, original["base_sha"], local_head)
        diff_source = CUMULATIVE_GIT_DIFF
    else:
        diff_source = GITHUB_PR_DIFF
        original["commits"] = commit_provenance(repo_root, original["commits"])
        write_result_file(
            context_path,
            {
                "captured_at": original["captured_at"],
                "pr": metadata,
                "original": {
                    key: value for key, value in original.items() if key != "commits"
                },
                "commits": original["commits"],
                **context,
            },
            "context",
        )
    anchors = parse_unified_diff(diff_text)
    commits_added = audit_commits(repo_root, original["head_sha"])

    if state is None:
        state = {
            "version": STATE_VERSION,
            "created_at": utc_now(),
            "iterations": 0,
            "next_candidate_id": 1,
            "history": [],
        }
    if invocation is not None:
        state["invocation_budget"] = invocation
        state["budget_scope"] = "invocation"
    elif "budget_scope" not in state:
        state["budget_scope"] = "lifetime"
    if not resuming:
        state["context_counts"] = {
            "issue_comments": len(context["issue_comments"]),
            "review_threads": len(context["review_threads"]),
            "reviews": len(context["reviews"]),
            "closing_issues": len(context["closing_issues"]),
        }
    archive_audit(state)
    state["iterations"] = int(state.get("iterations", 0))
    history_commit_presence = compare_history_commits(state["history"], commits_added)
    history_commits_missing = sum(
        not entry["in_audit_commits"] for entry in history_commit_presence
    )
    iteration = state["iterations"] + 1
    completed_iterations = invocation_iterations(state, invocation)
    result = "ready"

    diff_path = diff_path_for(state_path)
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(diff_text, encoding="utf-8", newline="")

    audit_block = {
        "branch": audit_branch,
        "base_sha": original["base_sha"],
        "head_sha": original["head_sha"],
        "local_head": local_head,
        "iteration_head_sha": local_head,
        "diff_source": diff_source,
        "branch_action": branch_state["branch_action"],
        "head_ref_moved": metadata["head_sha"] != original["head_sha"],
        "base_ref_moved": metadata["base_sha"] != original["base_sha"],
    }
    state.update(
        {
            "repo_root": str(repo_root),
            "pr": metadata,
            "original": original,
            "context_path": str(context_path),
            "audit_branch": audit_branch,
            "audit": {
                "id": f"pr-{metadata['number']}-audit-{iteration}",
                "status": "active",
                "iteration": iteration,
                "iteration_head_sha": local_head,
                "diff_path": str(diff_path),
                "diff_source": diff_source,
                "branch": audit_branch,
                "audit_commits": commits_added,
                "history_commit_presence": history_commit_presence,
                "anchors": serialize_anchors(anchors),
                "candidates": [],
                "batches": [],
            },
        }
    )
    save_state(state_path, state)

    changed_files = sorted(anchors)
    counts = {
        "changed_files": len(changed_files),
        "original_commits": len(original["commits"]),
        "audit_commits": len(commits_added),
        "history": len(state["history"]),
        "history_commits_missing": history_commits_missing,
        **state.get("context_counts", {}),
    }
    preflight_path = preflight_path_for(state_path)
    payload = {
        "result": result,
        "state": str(state_path),
        "context_path": str(context_path),
        "repo_root": str(repo_root),
        "pr": metadata,
        "original": original,
        "audit": audit_block,
        "head_sha": original["head_sha"],
        "diff_path": str(diff_path),
        "changed_files": changed_files,
        "original_commits": original["commits"],
        "audit_commits": commits_added,
        "history": state["history"],
        "history_commit_presence": history_commit_presence,
        "iteration": iteration,
        "completed_iterations": completed_iterations,
        "max_iterations": max_iterations,
        "budget_scope": state["budget_scope"],
        "invocation_run": None if invocation is None else invocation["run"],
    }
    write_result_file(preflight_path, payload, "preflight")
    emit(
        {
            "result": result,
            "state": str(state_path),
            "preflight_path": str(preflight_path),
            "context_path": str(context_path),
            "repo_root": str(repo_root),
            "pr": {
                "number": metadata["number"],
                "title": metadata["title"],
                "pr_url": metadata["pr_url"],
                "repo_name": metadata["repo_name"],
                "state": metadata["state"],
                "merged_at": metadata["merged_at"],
                "head_branch": metadata["head_branch"],
                "base_branch": metadata["base_branch"],
            },
            "audit": audit_block,
            "head_sha": original["head_sha"],
            "diff_path": str(diff_path),
            "diff_bytes": len(diff_text.encode("utf-8")),
            "counts": counts,
            "iteration": iteration,
            "completed_iterations": completed_iterations,
            "max_iterations": max_iterations,
            "budget_scope": state["budget_scope"],
            "invocation_run": None if invocation is None else invocation["run"],
        }
    )


def command_candidates(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    audit = active_audit(state)
    if audit["candidates"]:
        raise WorkflowError(
            "candidates are already registered for this iteration; "
            "run preflight to start the next one"
        )
    validated = validate_candidates(load_candidate_input(args.input), audit["anchors"])
    next_id = int(state.get("next_candidate_id", 1))
    registered = []
    for candidate in validated:
        registered.append({"id": next_id, "status": "pending", **candidate})
        next_id += 1
    audit["candidates"] = registered
    state["next_candidate_id"] = next_id
    save_state(path, state)
    emit({"result": "registered", "state": str(path), "candidates": registered})


def command_drop(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    audit = active_audit(state)
    candidates = find_candidates(audit, args.candidates)
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
    audit = active_audit(state)
    candidates = find_candidates(audit, args.candidates)
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
    audit["batches"] = [item for item in audit["batches"] if item["id"] != args.batch]
    audit["batches"].append(batch)
    for candidate in candidates:
        candidate["batch"] = args.batch
    save_state(path, state)
    emit({"result": "planned", "state": str(path), "batch": batch})


def find_batch(audit: dict[str, Any], batch_id: str) -> dict[str, Any] | None:
    for batch in audit.get("batches") or []:
        if batch.get("id") == batch_id:
            return batch
    return None


def planned_batch_paths(audit: dict[str, Any]) -> list[str]:
    """Collect every path the planned batches of this iteration declared."""
    paths: set[str] = set()
    for batch in audit.get("batches") or []:
        paths.update(path for path in (batch.get("paths") or []) if path)
    return sorted(paths)


def require_declared_commit_paths(
    repo_root: Path, commit: str, paths: Iterable[str], *, label: str
) -> list[str]:
    """Refuse a commit that touches a path the plan never declared."""
    declared = set(paths)
    touched = commit_paths(repo_root, commit)
    undeclared = [path for path in touched if path not in declared]
    if undeclared:
        raise WorkflowError(
            f"commit {commit} touches paths {label} does not declare: {undeclared}; "
            f"declared paths are {sorted(declared)}"
        )
    return touched


def command_record(args: argparse.Namespace) -> None:
    if not args.commit and not args.rationale:
        raise WorkflowError("record requires either --commit or --rationale")
    path = cli_path(args.state)
    state = load_state(path)
    audit = active_audit(state)
    candidates = find_candidates(audit, args.candidates)
    commit = args.commit
    touched: list[str] = []
    if commit:
        repo_root = Path(state["repo_root"])
        commit = git(repo_root, "rev-parse", commit)
        batch = find_batch(audit, args.batch)
        if batch is None:
            raise WorkflowError(
                f"batch {args.batch!r} was never planned; run plan before you record "
                "a commit for it"
            )
        planned_ids = sorted(batch.get("candidate_ids") or [])
        if planned_ids != sorted(args.candidates):
            raise WorkflowError(
                f"batch {args.batch!r} plans candidates {planned_ids}, so it cannot "
                f"record {sorted(args.candidates)}"
            )
        declared = [item for item in (batch.get("paths") or []) if item]
        if not declared:
            raise WorkflowError(
                f"batch {args.batch!r} declares no paths, so a commit cannot be "
                "checked against it; plan the batch again with --paths"
            )
        touched = require_declared_commit_paths(
            repo_root, commit, declared, label=f"batch {args.batch!r}"
        )
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
    for batch in audit["batches"]:
        if batch["id"] == args.batch:
            batch["status"] = "approved"
            if commit:
                batch["commit"] = commit
                batch["commit_paths"] = touched
    save_state(path, state)
    emit(
        {
            "result": "recorded",
            "state": str(path),
            "candidate_ids": args.candidates,
            "commit": commit,
            "commit_paths": touched,
            "rationale": args.rationale,
        }
    )


def command_skip(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    audit = active_audit(state)
    candidates = find_candidates(audit, args.candidates)
    for candidate in candidates:
        candidate.update(
            {"batch": args.batch, "status": "skipped", "rationale": args.rationale}
        )
    for batch in audit["batches"]:
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
    """Record that a whole pass found nothing, and keep the state that says so.

    The state file survives this command on purpose. It carries the clean head,
    the carried-forward history, and the local validation record, and a later
    reader needs all of it to analyze the audit. A later `preflight` against
    this state answers `already_complete` instead of starting an iteration, so
    one audit branch stays one audit. Auditing the same pull request again is a
    deliberate act: run `cleanup` and deal with the remote audit branch first.
    """
    if args.outcome != "clean":
        raise WorkflowError("resolve outcome must be clean")
    path = cli_path(args.state)
    state = load_state(path)
    audit = active_audit(state)
    disallowed = [
        {"id": candidate["id"], "status": candidate.get("status")}
        for candidate in audit.get("candidates") or []
        if candidate.get("status") != "dropped"
    ]
    if disallowed:
        raise WorkflowError(
            "an audit can be marked clean only with no candidates or when every "
            f"candidate is dropped: {disallowed}"
        )
    repo_root = Path(state["repo_root"])
    branch = current_branch(repo_root)
    if branch != state["audit_branch"]:
        raise WorkflowError(
            f"audit branch mismatch: local {branch!r}, expected "
            f"{state['audit_branch']!r}"
        )
    local_head = git(repo_root, "rev-parse", "HEAD")
    if local_head != audit["iteration_head_sha"]:
        raise WorkflowError(
            "audit head changed before clean resolution: expected "
            f"{audit['iteration_head_sha']}, got {local_head}"
        )
    audit["outcome"] = args.outcome
    audit["clean_at_head_sha"] = audit["iteration_head_sha"]
    save_state(path, state)
    emit(
        {
            "result": "resolved",
            "state": str(path),
            "outcome": args.outcome,
            "clean_at_head_sha": audit["clean_at_head_sha"],
        }
    )


def local_validation_entry(
    args: argparse.Namespace,
    published_head_sha: str,
    validation_commits: list[str] | None = None,
) -> dict[str, Any]:
    """Describe the local validation behind one publication.

    Four answers are distinct and a reader needs all four. `passed` names the
    commands that ran and passed, `partial` names the ones that passed next to
    the reason the rest could not run, `skipped` carries the reason none ran,
    and `unreported` says the publication claimed nothing either way. `rewrote`
    names the subset that changed files, because a fixing command's rewrites
    have to reach the commits being pushed, and `validation_commits` names the
    commits that carry those rewrites.

    Nothing here refuses a push. A historical snapshot often offers no command
    that still runs, so a malformed claim is folded into a coherent record rather
    than raised: naming a command as rewriting implies it ran, so it counts as
    validated too.
    """
    entry: dict[str, Any] = {"head_sha": published_head_sha}
    rewrote = [command.strip() for command in (args.rewrote or []) if command.strip()]
    commands = [
        command.strip() for command in (args.validated or []) if command.strip()
    ]
    for command in rewrote:
        if command not in commands:
            commands.append(command)
    reason = (getattr(args, "not_validated", None) or "").strip()
    if commands:
        entry["status"] = "partial" if reason else "passed"
        entry["commands"] = commands
        entry["rewrote"] = rewrote
        if reason:
            entry["reason"] = reason
    elif reason:
        entry["status"] = "skipped"
        entry["reason"] = reason
    else:
        entry["status"] = "unreported"
    if validation_commits:
        entry["validation_commits"] = list(validation_commits)
    return entry


def resolved_validation_commits(
    repo_root: Path,
    audit: dict[str, Any],
    requested: list[str] | None,
    new_commits: list[str],
) -> list[str]:
    """Admit the commits that carry a fixing command's rewrites.

    A covering check that rewrites files runs after the batch commits it
    validates, so its rewrites need a commit of their own. That commit belongs
    to no candidate, which is why publication has to be told about it. It is
    admitted only when it sits on this iteration and stays inside the paths the
    batches already planned, so it can never smuggle in an undeclared change.
    """
    resolved: list[str] = []
    for value in requested or []:
        reference = value.strip()
        if not reference:
            continue
        commit = git(repo_root, "rev-parse", reference)
        if commit in resolved:
            continue
        if commit not in new_commits:
            raise WorkflowError(
                f"validation commit {commit} is not one of this iteration's commits "
                f"({audit['iteration_head_sha']}..HEAD)"
            )
        require_declared_commit_paths(
            repo_root, commit, planned_batch_paths(audit), label="any planned batch"
        )
        resolved.append(commit)
    return resolved


def command_publish(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_state(path)
    audit = active_audit(state)
    repo_root = Path(state["repo_root"])
    require_clean_worktree(repo_root)

    pending = [
        candidate["id"]
        for candidate in audit["candidates"]
        if candidate["status"] == "pending"
    ]
    if pending:
        raise WorkflowError(f"candidates are neither dropped nor handled: {pending}")
    skipped = [
        candidate["id"]
        for candidate in audit["candidates"]
        if candidate["status"] == "skipped"
    ]
    if skipped:
        raise WorkflowError(
            f"a batch was skipped by an unrecoverable validation failure: {skipped}; "
            "this run must stop without publishing partial work"
        )
    handled = [
        candidate
        for candidate in audit["candidates"]
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

    iteration_head_sha = audit["iteration_head_sha"]
    local_head = git(repo_root, "rev-parse", "HEAD")
    new_commits = [
        line
        for line in git(
            repo_root, "rev-list", f"{iteration_head_sha}..HEAD"
        ).splitlines()
        if line
    ]
    validation_commits = resolved_validation_commits(
        repo_root, audit, getattr(args, "validation_commit", None), new_commits
    )
    allowed = set(commits) | set(validation_commits)
    unrecorded = [commit for commit in new_commits if commit not in allowed]
    missing = [commit for commit in commits if commit not in set(new_commits)]
    if unrecorded or missing:
        raise WorkflowError(
            "local commits do not match this iteration's records: "
            f"unrecorded {unrecorded}, missing {missing}"
        )
    published_commits = commits + [
        commit for commit in validation_commits if commit not in commits
    ]
    if not published_commits:
        emit(
            {
                "result": "nothing_to_publish",
                "state": str(path),
                "head_sha": local_head,
                "pushed": False,
            }
        )
        return

    pr = state["pr"]
    audit_branch = state["audit_branch"]
    if audit_branch in {pr["head_branch"], pr["base_branch"], state["original"]["head_branch"]}:
        raise WorkflowError(
            f"refusing to push {audit_branch!r}: it is the pull request's own branch "
            "or its base branch"
        )
    branch = current_branch(repo_root)
    if branch != audit_branch:
        raise WorkflowError(
            f"refusing to push: local branch is {branch!r}, expected {audit_branch!r}"
        )
    remote = find_remote(repo_root, pr["upstream_owner"], pr["upstream_repo"], push=True)
    if (
        remote_head(pr["upstream_owner"], pr["upstream_repo"], audit_branch)
        != local_head
    ):
        run(
            [
                "git",
                "-C",
                str(repo_root),
                "push",
                remote,
                f"HEAD:refs/heads/{audit_branch}",
            ]
        )
    pushed_head = wait_for_remote_head(
        pr["upstream_owner"], pr["upstream_repo"], audit_branch, local_head
    )
    if pushed_head != local_head:
        raise WorkflowError(
            f"audit branch mismatch: local {local_head}, remote {pushed_head}"
        )

    audit["status"] = "published"
    audit["published_head_sha"] = local_head
    audit["validation_commits"] = validation_commits
    validation = local_validation_entry(args, local_head, validation_commits)
    state.setdefault("local_validation", []).append(validation)
    state["iterations"] = int(state.get("iterations", 0)) + 1
    archive_audit(state)
    save_state(path, state)
    emit(
        {
            "result": "published",
            "state": str(path),
            "branch": audit_branch,
            "head_sha": local_head,
            "iteration_head_sha": iteration_head_sha,
            "commits": published_commits,
            "validation_commits": validation_commits,
            "iterations": state["iterations"],
            "local_validation": validation,
            "pushed": True,
        }
    )


def recorded_clean_at_head_sha(state: dict[str, Any]) -> str | None:
    """Return the clean-at-head SHA this state records, or None when it records none.

    `resolve` is the only command that writes this pair, and `preflight` replaces
    the whole audit when the next iteration starts, so the pair is the single
    durable fact that says an audit came out clean at a known head.
    """
    audit = state.get("audit")
    if not isinstance(audit, dict) or audit.get("outcome") != "clean":
        return None
    value = audit.get("clean_at_head_sha")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def stage_outcome(state: dict[str, Any]) -> str | None:
    """Name this run's ending in the vocabulary an orchestrator records.

    `resolve` is the only command that records an ending, so `cleared` is the
    only word this state can support. Returning `None` means this state supports
    no claim about an ending, and the field is then left out so a reader sees an
    absent answer rather than a manufactured one.
    """
    if recorded_clean_at_head_sha(state) is not None:
        return "cleared"
    return None


def stage_outcome_fields(state: dict[str, Any]) -> dict[str, str]:
    """Carry the stage outcome only when the state supports naming one."""
    outcome = stage_outcome(state)
    return {"stage_outcome": outcome} if outcome else {}


def current_audit_target(repo_root: Path) -> dict[str, Any]:
    """Resolve the audited pull request from the checked-out audit branch.

    The branch name carries the pull request number, so the lookup never ranks
    saved state files by timestamp or by any other rule of thumb.
    """
    branch = current_branch(repo_root)
    number = audit_branch_number(branch)
    if number is None:
        raise WorkflowError(
            f"current branch {branch or 'a detached HEAD'!r} is not an audit branch; "
            f"expected a name like {AUDIT_BRANCH_PREFIX}123, or pass --state"
        )
    return parse_target(f"{repo_name_from_remotes(repo_root)}#{number}")


def command_status(args: argparse.Namespace) -> None:
    if args.current:
        require_tools()
        repo_root = resolve_repo_root(args.repo_root)
        target = current_audit_target(repo_root)
        path = default_state_path(target)
        if not path.is_file():
            emit(
                {
                    "result": "no_state",
                    "state": str(path),
                    "pr": {"number": target["number"], "url": target["pr_url"]},
                    "audit": None,
                    "history": [],
                    "local_validation": [],
                }
            )
            return
    else:
        path = cli_path(args.state)
    state = load_state(path)
    pr = state["pr"]
    audit = state.get("audit")
    history = state.get("history") or []
    payload = {
        "result": "ready",
        "state": str(path),
        "pr": pr,
        "original": state.get("original"),
        "audit": audit,
        "history": history,
        "local_validation": state.get("local_validation") or [],
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
                "state": pr.get("state"),
                "head_branch": pr["head_branch"],
                "base_branch": pr["base_branch"],
            },
            "audit": None
            if audit is None
            else {
                "id": audit.get("id"),
                "status": audit.get("status"),
                "iteration": audit.get("iteration"),
                "branch": audit.get("branch"),
                "iteration_head_sha": audit.get("iteration_head_sha"),
                "published_head_sha": audit.get("published_head_sha"),
                "diff_path": audit.get("diff_path"),
                "diff_source": audit.get("diff_source"),
                "outcome": audit.get("outcome"),
                "clean_at_head_sha": audit.get("clean_at_head_sha"),
                "candidate_statuses": count_by_status(audit.get("candidates")),
                "batch_statuses": count_by_status(audit.get("batches")),
            },
            "counts": {
                "batches": len(((audit or {}).get("batches")) or []),
                "candidates": len(((audit or {}).get("candidates")) or []),
                "changed_files": len(((audit or {}).get("anchors")) or {}),
                "audit_commits": len(((audit or {}).get("audit_commits")) or []),
                "history": len(history),
            },
            "local_validation": state.get("local_validation") or [],
            **stage_outcome_fields(state),
            "iterations": int(state.get("iterations", 0)),
            "last_helper_activity": last_helper_activity(state),
        }
    )


def command_cleanup(args: argparse.Namespace) -> None:
    """Delete this pull request's audit state and every file the run wrote.

    Nothing else deletes them. A finished run keeps its state so the audit can
    be read afterwards, so this command is the explicit act that lets the same
    pull request be audited again. Deleting the remote audit branch is a
    separate act, and it happens outside this helper.
    """
    path = cli_path(args.state)
    load_state(path)
    path.unlink()
    diff_path_for(path).unlink(missing_ok=True)
    preflight_path_for(path).unlink(missing_ok=True)
    status_path_for(path).unlink(missing_ok=True)
    context_path_for(path).unlink(missing_ok=True)
    emit({"result": "cleaned_up", "state": str(path)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser(
        "preflight",
        help=(
            "pin a merged pull request's own base and head commits, prepare the "
            "audit branch, and snapshot the changeset for this pass"
        ),
    )
    preflight.add_argument(
        "target",
        help="merged PR URL or owner/repo#number",
    )
    preflight.add_argument("--repo-root")
    preflight.add_argument("--state")
    preflight.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
    invocation = preflight.add_mutually_exclusive_group()
    invocation.add_argument(
        "--new-invocation",
        action="store_true",
        help="start a fresh audit budget and return its invocation run token",
    )
    invocation.add_argument(
        "--invocation-run",
        help="reuse the invocation run token returned by its first preflight",
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

    resolve = subparsers.add_parser("resolve", help="record a clean audit outcome")
    resolve.add_argument("--state", required=True)
    resolve.add_argument("--outcome", choices=["clean"], required=True)
    resolve.set_defaults(function=command_resolve)

    publish = subparsers.add_parser(
        "publish", help="push the audit branch and verify the new head"
    )
    publish.add_argument("--state", required=True)
    publish.add_argument(
        "--validated",
        action="append",
        metavar="COMMAND",
        help="a covering check that ran locally and passed; repeat for each one",
    )
    publish.add_argument(
        "--not-validated",
        metavar="REASON",
        help="why the covering checks that did not run could not run; pass it "
        "alone when none ran, or next to --validated for a partial report",
    )
    publish.add_argument(
        "--rewrote",
        action="append",
        metavar="COMMAND",
        help="a covering check that rewrote files; those rewrites must already be "
        "in the commits this pushes",
    )
    publish.add_argument(
        "--validation-commit",
        action="append",
        metavar="SHA",
        help="a commit that carries only what a covering check rewrote; repeat for "
        "each one, and keep every path inside the planned batch paths",
    )
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
