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
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable


STATE_VERSION = 1
DEFAULT_MAX_ITERATIONS = 5
DEFAULT_PIPELINE_MAX_ITERATIONS = 2
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
STACK_ENTRIES_PAGE = 100
STACK_CONFLICT_EXIT = 3
ESCALATION_KINDS = (
    "contradiction",
    "max_iterations",
    "no_progress",
    "unsafe_push",
    "unknown_mergeability",
    "ad_hoc_base",
    "stack_external_dependents",
    "validation",
    "other",
)
STAGE_OUTCOMES = ("cleared", "skipped", "no_progress", "escalated", "carried")
RECORDED_ENDINGS = ("mergeable", "published", "escalated", "aborted")


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


def is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    """Report whether ``ancestor`` is already contained in ``descendant``.

    ``git merge-base --is-ancestor`` answers this directly and treats an equal
    pair as an ancestor, which is what a caller asking "is this base already in
    this head" wants.
    """
    return (
        git_try(repo_root, "merge-base", "--is-ancestor", ancestor, descendant).returncode
        == 0
    )


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


def base_ref_tip(repo_name: str, base_branch: str) -> str:
    """Return the live tip commit of a pull request's base branch.

    GitHub's ``baseRefOid`` freezes at the moment the pull request was created
    or last synced and does not follow the base branch as it moves, so reading
    it compares the head against a commit the base branch has since left behind.
    The branch ref always names the current tip, so this reads that instead.

    A base branch that has been deleted or is otherwise unreadable is a hard
    error. Falling back to the frozen ``baseRefOid`` would silently restore the
    staleness this exists to remove, and nothing downstream would see it happen.
    """
    result = run(
        ["gh", "api", f"repos/{repo_name}/git/ref/heads/{base_branch}"],
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise WorkflowError(
            f"could not read the tip of base branch {base_branch!r} in {repo_name}; "
            f"it may have been deleted: {detail}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise WorkflowError(
            f"reading the tip of base branch {base_branch!r} in {repo_name} "
            f"returned invalid JSON: {error}"
        ) from error
    obj = payload.get("object") if isinstance(payload, dict) else None
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if not isinstance(sha, str) or not sha:
        raise WorkflowError(
            f"the tip of base branch {base_branch!r} in {repo_name} has no commit SHA"
        )
    return sha


def parse_stack(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn a GraphQL ``PullRequestStack`` into an ordered member snapshot.

    Every member must be readable. Cascading only the visible subset would rewrite
    branches around an unknown layer, so an unreadable entry is a hard error.
    """
    trunk = raw.get("baseRefName")
    if not isinstance(trunk, str) or not trunk:
        raise WorkflowError("the native stack has no trunk branch")
    entries = raw.get("entries")
    nodes = entries.get("nodes") if isinstance(entries, dict) else None
    members: list[dict[str, Any]] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            raise WorkflowError("the native stack has an unreadable member")
        member = node.get("pullRequest")
        if not isinstance(member, dict):
            raise WorkflowError("the native stack has an unreadable member")
        number = member.get("number")
        head_branch = member.get("headRefName")
        base_branch = member.get("baseRefName")
        head_sha = member.get("headRefOid")
        base_sha = member.get("baseRefOid")
        timeline = member.get("timelineItems")
        events = timeline.get("nodes") if isinstance(timeline, dict) else None
        page_info = timeline.get("pageInfo") if isinstance(timeline, dict) else None
        if isinstance(page_info, dict) and page_info.get("hasNextPage") is True:
            raise WorkflowError(
                f"native stack member #{number} has incomplete branch history"
            )
        retargeted_from = None
        force_pushed = False
        for event in events or []:
            if (
                isinstance(event, dict)
                and event.get("__typename") == "HeadRefForcePushedEvent"
            ):
                force_pushed = True
            if (
                isinstance(event, dict)
                and event.get("newBase") == base_branch
                and isinstance(event.get("oldBase"), str)
                and event["oldBase"]
            ):
                retargeted_from = event["oldBase"]
        if (
            not isinstance(number, int)
            or not isinstance(head_branch, str)
            or not head_branch
            or not isinstance(base_branch, str)
            or not base_branch
            or not isinstance(head_sha, str)
            or not head_sha
            or not isinstance(base_sha, str)
            or not base_sha
        ):
            raise WorkflowError(
                f"native stack member {number!r} is missing a required field"
            )
        members.append(
            {
                "position": node.get("position"),
                "number": number,
                "head_branch": head_branch,
                "base_branch": base_branch,
                "mergeable": member.get("mergeable"),
                "head_sha": head_sha,
                "base_sha": base_sha,
                "retargeted_from": retargeted_from,
                "force_pushed": force_pushed,
            }
        )
    members.sort(
        key=lambda item: (item["position"] is None, item["position"], item["number"])
    )
    size = raw.get("size")
    if not isinstance(size, int) or size != len(members):
        raise WorkflowError(
            f"the native stack reports {size!r} members but exposes {len(members)}"
        )
    return {
        "id": raw.get("id"),
        "number": raw.get("number"),
        "size": size,
        "trunk": trunk,
        "members": members,
    }


def merged_predecessor(
    pr: dict[str, Any], member: dict[str, Any]
) -> dict[str, Any] | None:
    """Resolve the merged PR that caused GitHub to retarget a stack member."""
    old_base = member.get("retargeted_from")
    if not isinstance(old_base, str) or not old_base:
        return None
    repo_name = f"{pr['upstream_owner']}/{pr['upstream_repo']}"
    payload = gh_json(
        [
            "api",
            "--paginate",
            "--method",
            "GET",
            f"repos/{repo_name}/pulls",
            "-f",
            "state=closed",
            "-f",
            f"head={pr['upstream_owner']}:{old_base}",
        ]
    )
    if not isinstance(payload, list):
        raise WorkflowError(
            f"could not read the merged predecessor branch {old_base!r}"
        )
    matches = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        head = item.get("head")
        if (
            item.get("merged_at")
            and item.get("merge_commit_sha") == member["base_sha"]
            and isinstance(head, dict)
            and head.get("ref") == old_base
            and isinstance(head.get("sha"), str)
            and head["sha"]
        ):
            matches.append(
                {
                    "number": item.get("number"),
                    "head_branch": old_base,
                    "head_sha": head["sha"],
                    "merge_sha": item["merge_commit_sha"],
                }
            )
    if len(matches) > 1:
        raise WorkflowError(
            f"multiple merged pull requests match historical base {member['base_sha']} "
            f"for {old_base!r}"
        )
    return matches[0] if matches else None


def stack_membership(pr: dict[str, Any]) -> dict[str, Any]:
    """Read the repository default branch and whether this PR is a native stack.

    ``pullRequest.stack`` is the detection mechanism: non-null means a native
    GitHub stack, and ad-hoc base targeting returns null. The default branch is
    read here too so no downstream decision has to assume it is named ``main``.
    ``entries`` is paginated and returns the whole ``stack`` field as null unless
    a ``first:`` bound is supplied, so one is always passed.
    """
    query = (
        "query($owner: String!, $name: String!, $number: Int!, $first: Int!) {"
        "  repository(owner: $owner, name: $name) {"
        "    defaultBranchRef { name }"
        "    pullRequest(number: $number) {"
        "      stack {"
        "        id number size baseRefName"
        "        entries(first: $first) {"
        "          nodes {"
        "            position"
        "            pullRequest {"
        "              number headRefName baseRefName mergeable headRefOid baseRefOid"
        "              timelineItems(first: $first, itemTypes: ["
        "                AUTOMATIC_BASE_CHANGE_SUCCEEDED_EVENT,"
        "                HEAD_REF_FORCE_PUSHED_EVENT"
        "              ]) {"
        "                pageInfo { hasNextPage }"
        "                nodes {"
        "                  __typename"
        "                  ... on AutomaticBaseChangeSucceededEvent {"
        "                    oldBase newBase createdAt"
        "                  }"
        "                  ... on HeadRefForcePushedEvent {"
        "                    createdAt beforeCommit { oid } afterCommit { oid }"
        "                  }"
        "                }"
        "              }"
        "            }"
        "          }"
        "        }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    payload = graphql(
        query,
        {
            "owner": pr["upstream_owner"],
            "name": pr["upstream_repo"],
            "number": pr["number"],
            "first": STACK_ENTRIES_PAGE,
        },
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    repository = data.get("repository") if isinstance(data, dict) else None
    if not isinstance(repository, dict):
        raise WorkflowError("the stack query returned no repository")
    default_ref = repository.get("defaultBranchRef")
    default_branch = (
        default_ref.get("name") if isinstance(default_ref, dict) else None
    )
    if not isinstance(default_branch, str) or not default_branch:
        raise WorkflowError(
            f"repository {pr['upstream_owner']}/{pr['upstream_repo']} has no "
            "default branch"
        )
    pull = repository.get("pullRequest")
    if not isinstance(pull, dict):
        raise WorkflowError("the stack query returned no pull request")
    raw_stack = pull.get("stack")
    stack = parse_stack(raw_stack) if isinstance(raw_stack, dict) else None
    if stack is not None:
        for index, member in enumerate(stack["members"]):
            member["merged_predecessor"] = (
                merged_predecessor(pr, member) if index == 0 else None
            )
    return {"default_branch": default_branch, "stack": stack}


def merge_tree_conflicts(repo_root: Path, left: str, right: str) -> list[str]:
    """Return the files a real three-way merge of two commits would conflict in.

    ``git merge-tree --write-tree`` performs the merge in memory and exits 0 when
    it is clean and 1 when it conflicts; any other exit is a genuine git error,
    such as an unknown revision, and is surfaced rather than read as "clean".
    With ``--name-only`` the first line is the resulting tree object and the
    conflicted paths follow until the first blank line, after which git prints
    informational messages that are not file names.
    """
    result = git_try(
        repo_root, "merge-tree", "--write-tree", "--name-only", left, right
    )
    if result.returncode == 0:
        return []
    if result.returncode != 1:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise WorkflowError(
            f"could not test-merge {left} into {right}: {detail}"
        )
    conflicts: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        if not line.strip():
            break
        conflicts.append(line)
    return sorted(conflicts)


def ad_hoc_escalation(
    repo_root: Path,
    remote: str,
    pr: dict[str, Any],
    default_branch: str,
    default_sha: str,
) -> dict[str, Any]:
    """Explain a conflict on a pull request that targets a non-default base.

    Comparing only against the declared base reproduces the bare contradiction
    this exists to replace: an ad-hoc PR's declared base is usually already an
    ancestor of the head, so that merge is clean and names no file. Both merges
    are run and both reported, so the escalation names the branch and file that
    actually conflict instead of guessing at a stacked PR or stale state.
    """
    fetch_reference(repo_root, remote, pr["base_branch"], pr["base_sha"])
    fetch_reference(repo_root, remote, default_branch, default_sha)
    base_conflicts = merge_tree_conflicts(repo_root, pr["base_sha"], pr["head_sha"])
    default_conflicts = merge_tree_conflicts(
        repo_root, default_sha, pr["head_sha"]
    )
    if base_conflicts:
        reason = (
            f"the head {pr['head_sha']} conflicts with its declared base branch "
            f"{pr['base_branch']} in {', '.join(base_conflicts)}"
        )
        recommended_action = (
            "a person must resolve the conflict with the declared base branch this "
            "names"
        )
    elif default_conflicts:
        reason = (
            f"the head {pr['head_sha']} is clean against its declared base branch "
            f"{pr['base_branch']} but conflicts with the repository default branch "
            f"{default_branch} in {', '.join(default_conflicts)}; this pull request "
            f"targets a non-default base and is not part of a native stack, so "
            f"GitHub measures mergeability against {default_branch}"
        )
        recommended_action = (
            f"a person must resolve the conflict with {default_branch} this names, "
            "or retarget the pull request"
        )
    else:
        reason = (
            f"the head {pr['head_sha']} is clean against both its declared base "
            f"branch {pr['base_branch']} and the repository default branch "
            f"{default_branch}, yet GitHub reports it as conflicting; neither merge "
            f"reproduces GitHub's answer"
        )
        recommended_action = (
            "a person must work out why GitHub reports a conflict that neither merge "
            "reproduces"
        )
    return {
        "reason": reason,
        "recommended_action": recommended_action,
        "base_branch": pr["base_branch"],
        "base_conflicts": base_conflicts,
        "default_branch": default_branch,
        "default_conflicts": default_conflicts,
    }


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


def default_propagation_state_path(
    target: dict[str, Any], stack_number: int, fixed_number: int
) -> Path:
    name = (
        f"{target['owner']}--{target['repo']}--stack-{stack_number}"
        f"--after-{fixed_number}.json"
    )
    return Path.home() / ".copilot" / "run" / "conflict-fix-loop" / "propagation" / name


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
        raise WorkflowError(
            "cannot resolve the current pull request from detached HEAD, which "
            "names no branch to look up; pass the pull request explicitly"
        )
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
        "headRefOid,headRepositoryOwner,headRepository,baseRefName,commits"
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
    base_branch = metadata.get("baseRefName")
    if not isinstance(base_branch, str) or not base_branch:
        raise WorkflowError("resolved PR metadata has no base branch")
    base_sha = base_ref_tip(resolved["repo_name"], base_branch)
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
        "base_branch": base_branch,
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


def mergeability_settled(
    metadata: dict[str, Any], expected_head: str | None = None
) -> bool:
    """Report whether a mergeability read is worth acting on.

    A read is worth acting on once GitHub has finished computing the value and, when
    an expected head SHA is given, once the answer describes that commit.

    This narrows the stale window rather than closing it. No GitHub field states the
    commit a mergeable value was computed against, so an answer that describes the
    expected head can still carry a value computed just before the push landed. What
    it does rule out is the larger case, where the pull request has not registered the
    push at all and the answer is plainly about the previous head.
    """
    if metadata.get("mergeable") == "UNKNOWN":
        return False
    if expected_head is None:
        return True
    return metadata.get("head_sha") == expected_head


def live_mergeability(
    target: dict[str, Any],
    *,
    delays: Iterable[float] = MERGEABILITY_RETRY_DELAYS,
    expected_head: str | None = None,
) -> dict[str, Any]:
    """Read mergeability live, waiting while GitHub is still computing it.

    GitHub computes the value lazily, so the first read of a freshly pushed head is
    routinely UNKNOWN. Reading it again is what triggers and then observes the
    computation.

    A read taken right after a push can also still describe the previous head, and
    that stale answer carries a settled mergeable value rather than UNKNOWN. Pass the
    head SHA the answer has to describe so the wait covers that case too.
    """
    metadata = metadata_for(target)
    for delay in delays:
        if mergeability_settled(metadata, expected_head):
            return metadata
        time.sleep(delay)
        metadata = metadata_for(target)
    return metadata


def classify_mergeability(
    metadata: dict[str, Any], *, expected_head: str | None = None
) -> str:
    """Name the mergeability an answer reports, for the head it describes.

    An answer about any other head is reported as unknown rather than believed, which
    fails safe: the caller escalates instead of trusting a value it cannot place.
    """
    if expected_head is not None and metadata.get("head_sha") != expected_head:
        return "unknown"
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


def external_stack_dependents(
    pr: dict[str, Any], stack: dict[str, Any]
) -> list[dict[str, Any]]:
    """Open pull requests based on a branch the cascade moves but outside the stack.

    A cascade rewrites every member's head branch. An open pull request based on
    one of those branches, but not itself a member, has its history orphaned when
    that branch is force-pushed. The user approved rewriting the stack's own
    members, and that grant does not reach an arbitrary dependent, so one refuses
    the cascade instead of silently orphaning it.

    Only the members' head branches are checked. The trunk is a member's base but
    never a member's head, so the cascade does not rewrite it, and open pull
    requests targeting the trunk are not dependents in this sense.
    """
    upstream = f"{pr['upstream_owner']}/{pr['upstream_repo']}"
    member_numbers = {member["number"] for member in stack["members"]}
    member_branches = {member["head_branch"] for member in stack["members"]}
    found: dict[int, dict[str, Any]] = {}
    for branch in sorted(member_branches):
        for item in list_open_pulls(upstream, {"base": branch}):
            summary = summarize_pull(item)
            if summary["number"] in member_numbers:
                continue
            found[summary["number"]] = {
                "number": summary["number"],
                "url": summary["url"],
                "head_branch": summary["head_branch"],
                "base_branch": branch,
            }
    return [found[key] for key in sorted(found)]


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


def line_endings_in(data: bytes | None) -> set[str]:
    """Name every line ending a file actually contains."""
    if not data:
        return set()
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    present = set()
    if crlf:
        present.add("crlf")
    if lf:
        present.add("lf")
    return present


def line_ending_style(data: bytes | None) -> str:
    """Name the line ending a file uses: lf, crlf, mixed, or none."""
    present = line_endings_in(data)
    if len(present) == 2:
        return "mixed"
    return present.pop() if present else "none"


def introduced_line_ending(
    resolved: bytes | None, sides: Iterable[bytes | None]
) -> str | None:
    """Report a line ending the resolution introduced that neither side contained.

    An editor on Windows can rewrite a whole file as it saves it. Git normalization
    can then hide that in a diff, so the resolution looks small while every line
    changed. Comparing against both sides catches it before anything is staged.
    """
    existing: set[str] = set()
    for side in sides:
        existing |= line_endings_in(side)
    if not existing:
        return None
    introduced = line_endings_in(resolved) - existing
    if not introduced:
        return None
    return introduced.pop()


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


def collect_stack_conflicts(
    repo_root: Path, stack: dict[str, Any]
) -> list[dict[str, Any]]:
    """Report conflicts against the current stack layer's frozen boundaries."""
    current = stack.get("current_index")
    plan = stack.get("plan") or []
    if not isinstance(current, int) or current < 0 or current >= len(plan):
        raise WorkflowError("the stack cascade has no conflicted member to inspect")
    member = plan[current]
    old_base = member.get("old_base")
    head_sha = member.get("head_sha")
    new_base_ref = member.get("new_base_ref")
    if not all(
        isinstance(value, str) and value
        for value in (old_base, head_sha, new_base_ref)
    ):
        raise WorkflowError("the conflicted stack member has incomplete history")

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
                    repo_root, f"{old_base}..{head_sha}", path
                ),
                "base_commits": commits_touching(
                    repo_root, f"{old_base}..{new_base_ref}", path
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
        path = Path(location.stdout.strip())
        if not path.is_absolute():
            path = Path(repo_root) / path
        if location.returncode == 0 and path.exists():
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


def attempt_repo_root(state: dict[str, Any], attempt: dict[str, Any]) -> Path:
    """The checkout an attempt operates in.

    A single-branch attempt works in the session worktree recorded as
    ``repo_root``. A stack cascade works in its own scratch clone, because it has
    to claim and move every branch in the stack and git refuses to check a branch
    out in two worktrees of one repository. Reading the root from the attempt
    keeps ``resolved`` and the rest of the staging path identical for both.
    """
    if attempt.get("strategy") == "stack":
        workspace = (attempt.get("stack") or {}).get("workspace")
        if not workspace:
            raise WorkflowError(
                "this stack attempt has no cascade workspace; run stack-rebase first"
            )
        return Path(workspace)
    return Path(state["repo_root"])


def find_conflicts(
    attempt: dict[str, Any], paths: Iterable[str]
) -> list[dict[str, Any]]:
    by_path = {conflict["path"]: conflict for conflict in attempt.get("conflicts") or []}
    missing = [path for path in paths if path not in by_path]
    if missing:
        raise WorkflowError(f"paths are not conflicted in this attempt: {missing}")
    return [by_path[path] for path in paths]


def replayed_commit_paths(repo_root: Path) -> set[str]:
    result = git_try(
        repo_root,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "--root",
        "-z",
        "REBASE_HEAD",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise WorkflowError(f"could not read the commit being replayed: {detail}")
    return {path for path in result.stdout.split("\0") if path}


def normalize_companion_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or ".." in normalized.split("/")
    ):
        raise WorkflowError(f"companion path must be relative to the repository: {path}")
    return normalized


def literal_pathspec(path: str) -> str:
    return f":(literal){path}"


def path_has_unstaged_changes(repo_root: Path, path: str) -> bool:
    result = git_try(repo_root, "diff", "--quiet", "--", literal_pathspec(path))
    if result.returncode not in {0, 1}:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise WorkflowError(f"could not inspect {path}: {detail}")
    return result.returncode == 1


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
            "companion_resolutions": attempt.get("companion_resolutions") or [],
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


def attached_to_other_branch(repo_root: Path, head_branch: str) -> str | None:
    """Name the branch this worktree holds when it is not the pull request's own.

    A detached head returns nothing and is welcome. The resolution reaches the
    head branch through the push refspec, so the branch a worktree happens to
    hold decides nothing about where the work lands. Another branch is another
    line of work, and committing a resolution onto it would be wrong.
    """
    branch = git(repo_root, "branch", "--show-current")
    if branch and branch != head_branch:
        return branch
    return None


def checkout_pr_branch(
    repo_root: Path, target: dict[str, Any], metadata: dict[str, Any]
) -> bool:
    """Put this worktree on the pull request's head commit, detaching to get there.

    Resolving a conflict commits onto the head branch through the push refspec,
    not through the branch name this worktree carries, so a detached head serves
    the whole loop. It also serves the one arrangement that claiming the branch
    cannot: git refuses to check a branch out in two worktrees of one repository,
    and the session worktree that opened the pull request is usually still
    holding it, so attaching would fail exactly when a conflict needs resolving.
    The branch is kept only when this worktree already holds it.

    Returns whether the worktree stayed attached to the head branch.
    """
    current_branch = git(repo_root, "branch", "--show-current")
    on_pr_branch = current_branch == metadata["head_branch"]
    command = ["gh", "pr", "checkout", target["pr_url"]]
    if not on_pr_branch:
        command.append("--detach")
    run(command, cwd=repo_root)
    stray = attached_to_other_branch(repo_root, metadata["head_branch"])
    if stray is not None:
        raise WorkflowError(
            f"branch mismatch: local {stray!r}, PR head {metadata['head_branch']!r}"
        )
    local_head = git(repo_root, "rev-parse", "HEAD")
    if local_head != metadata["head_sha"]:
        raise WorkflowError(
            f"HEAD mismatch: local {local_head}, PR head {metadata['head_sha']}; "
            "this loop resolves the authoritative remote branch, so publish or "
            "reconcile local work before preflight"
        )
    return on_pr_branch


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


def planned_stack_attempt(
    metadata: dict[str, Any], stack: dict[str, Any], iteration: int
) -> dict[str, Any]:
    """Build the planned attempt for a native-stack cascade.

    The member head SHAs are captured now as the pre-cascade baseline; publish
    compares the rebased tips against these to prove exactly what moved and that
    nothing landed anywhere unexpected.
    """
    members = [
        {
            "number": member["number"],
            "head_branch": member["head_branch"],
            "base_branch": member["base_branch"],
            "mergeable": member.get("mergeable"),
            "head_sha": member["head_sha"],
            "base_sha": member["base_sha"],
            "merged_predecessor": member.get("merged_predecessor"),
        }
        for member in stack["members"]
    ]
    return {
        "id": f"pr-{metadata['number']}-iteration-{iteration}",
        "status": "planned",
        "iteration": iteration,
        "strategy": "stack",
        "strategy_reason": (
            "the pull request is part of a native GitHub stack; the conflict is "
            "resolved by cascading a rebase through the trunk"
        ),
        "strategy_warnings": [],
        "head_sha": metadata["head_sha"],
        "base_sha": metadata["base_sha"],
        "merge_base": None,
        "mergeable": metadata.get("mergeable"),
        "merge_state_status": metadata.get("merge_state_status"),
        "started_at": utc_now(),
        "conflicts": [],
        "conflict_signature": None,
        "published_head_sha": None,
        "mergeable_at_head_sha": None,
        "stack": {
            "number": stack["number"],
            "size": stack["size"],
            "trunk": stack["trunk"],
            "invoked_number": metadata["number"],
            "members": members,
            "workspace": None,
            "members_after": None,
        },
    }


def stack_member_target(pr: dict[str, Any], number: int) -> dict[str, Any]:
    target = parse_target(pr["pr_url"])
    return {
        **target,
        "number": number,
        "pr_url": (
            f"https://github.com/{target['owner']}/{target['repo']}/pull/{number}"
        ),
    }


def stack_is_mergeable(
    pr: dict[str, Any], stack: dict[str, Any], invoked: dict[str, Any]
) -> bool:
    for member in stack["members"]:
        metadata = (
            invoked
            if member["number"] == invoked["number"]
            else live_mergeability(
                stack_member_target(pr, member["number"]),
                expected_head=member["head_sha"],
            )
        )
        if (
            classify_mergeability(metadata, expected_head=member["head_sha"])
            != "mergeable"
        ):
            return False
    return True


def command_preflight(args: argparse.Namespace) -> None:
    require_tools()
    repo_root = resolve_repo_root(args.repo_root)
    target = resolve_target(args.target, repo_root)
    state_path = cli_path(args.state) if args.state else default_state_path(target)
    state = load_state(state_path) if state_path.is_file() else None
    if state is not None:
        cleanup_replaced_stack_attempt(state)

    require_clean_worktree(repo_root)
    require_no_integration_in_progress(repo_root)

    metadata = live_mergeability(target)
    require_open_pull_request(metadata)
    checkout_pr_branch(repo_root, target, metadata)

    relations = stack_relations(metadata)
    merge_methods = repository_merge_methods(metadata["repo_name"])
    push_blockers = push_safety_blockers(metadata, relations)
    detection = stack_membership(metadata)
    default_branch = detection["default_branch"]
    stack = detection["stack"]
    # Established from metadata before anything touches disk: a cascade the user
    # did not approve is refused here, not after a workspace is cloned.
    external_dependents = (
        external_stack_dependents(metadata, stack) if stack is not None else []
    )

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
    state["default_branch"] = default_branch
    state["stack"] = stack

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
    # is what `archive_attempt` dedupes history on and a duplicate is dropped
    # rather than recorded. Any budget that rewrote that count instead of taking a
    # baseline against it would restart the numbering and lose an entry.
    iteration = state["iterations"] + 1
    mergeability = classify_mergeability(metadata)
    whole_stack = bool(getattr(args, "whole_stack", False))
    whole_stack_mergeable = (
        whole_stack
        and stack is not None
        and stack_is_mergeable(metadata, stack, metadata)
    )

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
    elif exhausted:
        result = "max_iterations_reached"
    elif whole_stack_mergeable:
        result = "stack_mergeable"
    elif mergeability == "mergeable" and not (whole_stack and stack is not None):
        result = "mergeable"
    elif stack is not None and external_dependents:
        # The cascade would force-push branches that open pull requests outside
        # the stack are based on. The user approved rewriting the stack's own
        # members, not these, so name them and refuse rather than orphan them.
        result = "stack_external_dependents"
    elif stack is not None:
        # A native GitHub stack resolves by cascading a rebase through the trunk,
        # a whole-stack operation the single-branch strategies cannot express, so
        # it is routed to its own path rather than through choose_strategy.
        result = "stack_rebase"
    elif mergeability == "unknown":
        result = "unknown_mergeability"
    elif metadata["base_branch"] != default_branch:
        # The declared base is neither the default branch nor a native stack
        # trunk, so GitHub measures mergeability against a different branch than
        # the one this loop would merge in. Naming the real conflict beats
        # rebasing onto the declared base and reporting a false clearance.
        result = "ad_hoc_base"
    elif strategy_error is not None:
        result = "no_safe_strategy"
    else:
        result = "ready"

    if result in {"mergeable", "stack_mergeable", "ready"}:
        attempt = {
            "id": f"pr-{metadata['number']}-iteration-{iteration}",
            "status": (
                "mergeable"
                if result in {"mergeable", "stack_mergeable"}
                else "planned"
            ),
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
            "mergeable_at_head_sha": (
                metadata["head_sha"]
                if result in {"mergeable", "stack_mergeable"}
                else None
            ),
        }
        state["attempt"] = attempt
    elif result == "stack_rebase":
        state["attempt"] = planned_stack_attempt(metadata, stack, iteration)
    else:
        state["attempt"] = None

    ad_hoc: dict[str, Any] | None = None
    if result == "ad_hoc_base":
        remote = find_remote(repo_root, metadata["repo_name"], push=False)
        default_sha = base_ref_tip(metadata["repo_name"], default_branch)
        ad_hoc = ad_hoc_escalation(
            repo_root, remote, metadata, default_branch, default_sha
        )

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
    elif result == "ad_hoc_base":
        record_escalation(
            state,
            kind="ad_hoc_base",
            reason=ad_hoc["reason"],
            recommended_action=ad_hoc["recommended_action"],
            iteration=iteration,
        )
    elif result == "stack_external_dependents":
        listed = "; ".join(
            f"#{item['number']} (targets {item['base_branch']})"
            for item in external_dependents
        )
        record_escalation(
            state,
            kind="stack_external_dependents",
            reason=(
                "the cascade would rewrite branches that open pull requests "
                f"outside the stack are based on: {listed}"
            ),
            recommended_action=(
                "a person must retarget or close these dependent pull requests "
                "before the stack can be cascaded"
            ),
            iteration=iteration,
        )
    elif result in {"mergeable", "stack_mergeable", "ready", "stack_rebase"}:
        state["escalation"] = None

    save_state(state_path, state)
    if result == "stack_mergeable":
        record_stack_member_clearances(state, stack["members"], metadata["number"])

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
        "default_branch": default_branch,
        "stack": stack,
        "external_dependents": external_dependents,
        "ad_hoc": ad_hoc,
        "strategy": strategy_choice,
        "strategy_error": strategy_error,
        "escalation": state.get("escalation"),
        "history": state["history"],
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
            "default_branch": default_branch,
            "stack": None
            if stack is None
            else {
                "number": stack["number"],
                "trunk": stack["trunk"],
                "size": stack["size"],
                "members": [member["number"] for member in stack["members"]],
            },
            "ad_hoc": ad_hoc,
            "external_dependents": external_dependents,
            "escalation": state.get("escalation"),
            "iteration": iteration,
            "max_iterations": max_iterations,
            "completed_iterations": completed_iterations,
            "absolute_cap": absolute_cap,
            "budget_exhausted": exhausted,
            "pipeline_run": None if scope is None else scope["run"],
            "pipeline_iteration": None if scope is None else scope["iteration"],
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

    stray = attached_to_other_branch(repo_root, pr["head_branch"])
    if stray is not None:
        raise WorkflowError(
            f"branch mismatch: local {stray!r}, PR head {pr['head_branch']!r}"
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

    if is_ancestor(repo_root, pr["base_sha"], attempt["head_sha"]):
        # The base tip is already contained in the head, so there is genuinely
        # nothing to integrate. Deciding this from the merge afterwards would
        # infer it from the merge changing nothing; asking git directly lets the
        # escalation state the fact and name the two commits it compared. GitHub
        # can report CONFLICTING against a base tip that is already an ancestor,
        # and that stale flag is exactly what leaves this loop with no work.
        attempt["status"] = "escalated"
        escalation = record_escalation(
            state,
            kind="contradiction",
            reason=(
                f"the base branch tip {pr['base_sha']} is already an ancestor of the "
                f"head {attempt['head_sha']}, so there is nothing to integrate, yet "
                f"GitHub reports {pr['head_branch']} as conflicting with "
                f"{pr['base_branch']}"
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
    repo_root = attempt_repo_root(state, attempt)
    conflicts = find_conflicts(attempt, args.paths)
    companion_paths = list(
        dict.fromkeys(
            normalize_companion_path(path)
            for path in (getattr(args, "companion_paths", None) or [])
        )
    )
    conflict_paths = {
        conflict["path"] for conflict in (attempt.get("conflicts") or [])
    }
    overlap = [path for path in companion_paths if path in conflict_paths]
    if overlap:
        raise WorkflowError(
            f"companion paths are already recorded as conflicted paths: {overlap}"
        )
    if companion_paths and attempt.get("strategy") not in {"rebase", "stack"}:
        raise WorkflowError(
            "companion paths are allowed only while a rebase is replaying a commit"
        )
    if companion_paths:
        replayed_paths = replayed_commit_paths(repo_root)
        unrelated = [path for path in companion_paths if path not in replayed_paths]
        if unrelated:
            raise WorkflowError(
                "companion paths are not touched by the commit currently being "
                f"replayed: {unrelated}"
            )
    rationale = (
        load_text_input(args.rationale_file, "rationale")
        if args.rationale_file
        else args.rationale
    )

    validated_conflicts = []
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
        introduced = None
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
            introduced = introduced_line_ending(
                target.read_bytes(), (blobs["head"], blobs["base"])
            )
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
        if introduced and not args.accept_line_endings:
            raise WorkflowError(
                f"{path} now uses {introduced.upper()} line endings, which neither side "
                "of the conflict used; an editor rewrote the whole file, so every line "
                "would change and the real resolution would be invisible in the diff; "
                "restore the original line endings, or pass --accept-line-endings with "
                "the reason the file has to change style"
            )
        validated_conflicts.append(
            {
                "conflict": conflict,
                "path": path,
                "one_side": one_side,
                "deleted": deleted,
            }
        )

    validated_companions = []
    for path in companion_paths:
        target = repo_root / path
        deleted = not target.exists()
        if deleted and not args.accept_deletion:
            raise WorkflowError(
                f"{path} no longer exists in the worktree; a companion deletion needs "
                "--accept-deletion together with the reason both sides allow it"
            )
        if not deleted:
            text = read_worktree_text(target)
            if text is not None:
                markers = parse_conflict_markers(text)
                if markers["regions"] or markers["problems"]:
                    raise WorkflowError(f"{path} still holds conflict markers")
        if not path_has_unstaged_changes(repo_root, path):
            raise WorkflowError(f"companion path has no unstaged resolution change: {path}")
        validated_companions.append({"path": path, "deleted": deleted})

    stage_paths = [
        entry["path"] for entry in validated_conflicts + validated_companions
    ]
    add = git_try(
        repo_root,
        "add",
        "--all",
        "--",
        *(literal_pathspec(path) for path in stage_paths),
    )
    if add.returncode != 0:
        detail = add.stderr.strip() or add.stdout.strip() or "no output"
        raise WorkflowError(
            f"could not stage {', '.join(stage_paths)}: {detail}"
        )

    recorded = []
    resolved_at = utc_now()
    for entry in validated_conflicts:
        conflict = entry["conflict"]
        conflict["status"] = "resolved"
        conflict["rationale"] = rationale
        conflict["one_side"] = entry["one_side"]
        conflict["deleted"] = entry["deleted"]
        conflict["resolved_at"] = resolved_at
        recorded.append(
            {
                "path": entry["path"],
                "one_side": entry["one_side"],
                "deleted": entry["deleted"],
            }
        )

    companion_resolutions = list(attempt.get("companion_resolutions") or [])
    companions = []
    for entry in validated_companions:
        path = entry["path"]
        companion_resolutions[:] = [
            existing
            for existing in companion_resolutions
            if existing.get("path") != path
        ]
        companion_resolutions.append(
            {
                "path": path,
                "rationale": rationale,
                "deleted": entry["deleted"],
                "resolved_at": resolved_at,
            }
        )
        companions.append({"path": path, "deleted": entry["deleted"]})
    if companion_resolutions:
        attempt["companion_resolutions"] = companion_resolutions

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
            "companions": companions,
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

    stray = attached_to_other_branch(repo_root, pr["head_branch"])
    if stray is not None:
        raise WorkflowError(
            f"refusing to push from branch {stray!r}, which is not the PR head "
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

    final = live_mergeability(target, expected_head=local_head)
    mergeability = classify_mergeability(final, expected_head=local_head)
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


def force_rmtree(path: Path) -> None:
    """Remove a directory tree, clearing the read-only bit git sets on objects."""

    def clear_readonly(function, target, _info):
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, ignore_errors=False, onerror=clear_readonly)


def create_stack_workspace(pr: dict[str, Any], reference: Path | None = None) -> Path:
    """Clone the upstream repository into a throwaway directory for a cascade.

    A cascade must check out and move every branch in the stack, which the App's
    worktrees forbid because git refuses to check one branch out in two worktrees
    of a repository. A separate clone has independent refs, so it can claim every
    branch, and it holds none the App's worktrees hold. The clone is safe to
    discard at any point because the helper rebases locally and pushes nothing,
    so nothing on the remote moves until an explicit publish.

    ``gh repo clone`` forwards everything after ``--`` to ``git clone`` while
    keeping ``gh``'s credential setup for the later git push. When ``reference``
    names an on-disk object store, it is forwarded as
    ``--reference-if-able`` so the clone borrows those objects instead of
    downloading the whole repository, which for a large upstream is minutes and
    gigabytes per cascade. ``--reference-if-able`` degrades to a full clone on its
    own if the reference turns out to be unusable. A clone that borrows objects
    must be dissociated before it is preserved past the cascade; see
    ``dissociate_workspace``.
    """
    workspace = Path(tempfile.mkdtemp(prefix="conflict-fix-loop-stack."))
    upstream = f"{pr['upstream_owner']}/{pr['upstream_repo']}"
    git_flags = ["--no-single-branch"]
    if reference is not None:
        git_flags.extend(["--reference-if-able", str(reference)])
    clone = run(
        ["gh", "repo", "clone", upstream, str(workspace), "--", *git_flags],
        check=False,
    )
    if clone.returncode != 0:
        detail = clone.stderr.strip() or clone.stdout.strip() or "no output"
        force_rmtree(workspace)
        raise WorkflowError(
            f"could not clone {upstream} for the stack cascade: {detail}"
        )
    return workspace


def local_object_source(repo_root: Path) -> Path | None:
    """The common object store a cascade clone can borrow, or None when there is none.

    Almost every checkout this pipeline runs in is a linked worktree whose own
    ``.git`` is a file and whose objects live in the main repository.
    ``--git-common-dir`` resolves to that shared store; a worktree path passed to
    ``--reference`` would not give the object reuse we want. The objects directory
    must exist to borrow from, and when the path does not resolve the caller falls
    back to a full clone.
    """
    result = git_try(repo_root, "rev-parse", "--git-common-dir")
    if result.returncode != 0:
        return None
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = (Path(repo_root) / common).resolve()
    if not (common / "objects").is_dir():
        return None
    return common


def dissociate_workspace(workspace: Path) -> str | None:
    """Make a preserved cascade clone own the objects it borrows through a reference.

    A ``--reference`` clone borrows objects through
    ``.git/objects/info/alternates`` rather than owning them. That is fine for a
    scratch clone deleted at the end of a cascade, but a workspace preserved after
    a rejected or unverifiable publish can outlive the source it borrows from --
    worktrees vanish from disk in this environment, and even a surviving source
    can prune the borrowed objects, which its garbage collection does without
    knowing about an outside borrower. Either way the preserved workspace becomes
    corrupt at exactly the moment someone needs it for diagnosis. Repack to copy
    the objects in, drop the alternates file, and prove HEAD still resolves.

    Returns None once the workspace stands on its own, or a message naming the
    still-borrowed source when the workspace could not be dissociated.
    """
    alternates = Path(workspace) / ".git" / "objects" / "info" / "alternates"
    if not alternates.exists():
        return None
    borrowed = alternates.read_text(encoding="utf-8").strip() or "its object source"
    repack = git_try(workspace, "repack", "-a", "-d")
    if repack.returncode != 0:
        detail = repack.stderr.strip() or repack.stdout.strip() or "no output"
        return (
            "could not repack the preserved cascade workspace, which still borrows "
            f"objects from {borrowed}: {detail}"
        )
    try:
        alternates.unlink()
    except OSError as error:
        return (
            "could not drop the alternates file from the preserved cascade "
            f"workspace, which still borrows objects from {borrowed}: {error}"
        )
    resolved = git_try(workspace, "rev-parse", "HEAD")
    if resolved.returncode != 0 or alternates.exists():
        return (
            f"the preserved cascade workspace may still depend on {borrowed}: HEAD "
            "did not resolve after dissociating"
        )
    return None


def remove_stack_workspace(attempt: dict[str, Any]) -> None:
    """Delete a cascade's scratch clone and forget its path."""
    stack = attempt.get("stack") or {}
    workspace = stack.get("workspace")
    if workspace and Path(workspace).exists():
        force_rmtree(Path(workspace))
    if stack:
        stack["workspace"] = None


def cleanup_replaced_stack_attempt(state: dict[str, Any]) -> None:
    """Dispose of a preserved failed-publish clone before a new preflight."""
    attempt = state.get("attempt") or {}
    if attempt.get("strategy") != "stack":
        return
    stack = attempt.get("stack") or {}
    workspace = stack.get("workspace")
    status = attempt.get("status")
    if status == "published_refs":
        raise WorkflowError(
            "the stack refs are already published; re-run stack-publish before "
            "starting a new preflight"
        )
    if status != "resolved":
        raise WorkflowError(
            f"the previous stack cascade is still {status}; run stack-abort before "
            "starting a new preflight"
        )
    if workspace and Path(workspace).exists():
        remove_stack_workspace(attempt)


def validate_stack_snapshot(stack: dict[str, Any]) -> None:
    """Require a complete, linear stack before any local branch moves."""
    members = stack.get("members") or []
    if not members:
        raise WorkflowError("the native stack has no members")
    if stack.get("size") != len(members):
        raise WorkflowError(
            f"the native stack reports {stack.get('size')!r} members but the "
            f"cascade received {len(members)}"
        )
    seen_numbers: set[int] = set()
    seen_branches: set[str] = set()
    expected_base = stack.get("trunk")
    for member in members:
        number = member.get("number")
        branch = member.get("head_branch")
        base = member.get("base_branch")
        head = member.get("head_sha")
        historical_base = member.get("base_sha")
        if (
            not isinstance(number, int)
            or not isinstance(branch, str)
            or not branch
            or not isinstance(base, str)
            or not base
            or not isinstance(head, str)
            or not head
            or not isinstance(historical_base, str)
            or not historical_base
        ):
            raise WorkflowError("the native stack snapshot has an incomplete member")
        if number in seen_numbers or branch in seen_branches:
            raise WorkflowError(
                f"the native stack repeats pull request #{number} or branch {branch!r}"
            )
        if base != expected_base:
            raise WorkflowError(
                f"the native stack is not linear at #{number}: {branch!r} targets "
                f"{base!r}, expected {expected_base!r}"
            )
        seen_numbers.add(number)
        seen_branches.add(branch)
        expected_base = branch
    invoked = stack.get("invoked_number")
    if invoked not in seen_numbers:
        raise WorkflowError(
            f"the invoked pull request #{invoked} is missing from the native stack"
        )


def stack_base_mismatches(stack: dict[str, Any]) -> list[dict[str, Any]]:
    """Name members whose direct PR base disagrees with native stack order."""
    mismatches = []
    expected_base = stack.get("trunk")
    for member in stack.get("members") or []:
        if member.get("base_branch") != expected_base:
            mismatches.append(
                {
                    "number": member.get("number"),
                    "head_branch": member.get("head_branch"),
                    "base_branch": member.get("base_branch"),
                    "expected_base": expected_base,
                }
            )
        expected_base = member.get("head_branch")
    return mismatches


def linear_stack_segments(stack: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """Split members into maximal vertex-disjoint direct-base chains."""
    members = stack.get("members") or []
    by_head = {member["head_branch"]: member for member in members}
    checked: set[int] = set()
    for member in members:
        path: set[int] = set()
        current = member
        while current is not None and current["number"] not in checked:
            if current["number"] in path:
                raise WorkflowError(
                    "the malformed native stack contains a direct-base cycle"
                )
            path.add(current["number"])
            current = by_head.get(current["base_branch"])
        checked.update(path)

    children = {member["head_branch"]: [] for member in members}
    for member in members:
        parent = by_head.get(member["base_branch"])
        if parent is not None:
            children[parent["head_branch"]].append(member)

    starts = [
        member
        for member in members
        if member["base_branch"] not in by_head
        or len(children[member["base_branch"]]) != 1
    ]
    segments: list[list[dict[str, Any]]] = []
    visited: set[int] = set()
    for start in starts:
        segment = []
        current = start
        while current["number"] not in visited:
            segment.append(current)
            visited.add(current["number"])
            descendants = children[current["head_branch"]]
            if len(descendants) != 1:
                break
            current = descendants[0]
        segments.append(segment)
    if len(visited) != len(members):
        raise WorkflowError("the malformed native stack could not be split into chains")
    return segments


def member_stack(pr: dict[str, Any], number: int) -> dict[str, Any] | None:
    """Read native stack membership for another member of the same repository."""
    return stack_membership({**pr, "number": number}).get("stack")


def effective_stack_group(
    stack: dict[str, Any] | None, number: int
) -> tuple[int, ...] | None:
    """Treat GitHub's retained one-member stack wrapper as unstacked."""
    if stack is None:
        return None
    numbers = tuple(member["number"] for member in stack["members"])
    return None if numbers == (number,) else numbers


def unstack_native_stack(pr: dict[str, Any], number: int) -> None:
    """Remove every unlocked pull request from one malformed native stack."""
    repo_name = f"{pr['upstream_owner']}/{pr['upstream_repo']}"
    result = run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            "-H",
            "X-GitHub-Api-Version: 2026-03-10",
            f"repos/{repo_name}/stacks/{number}/unstack",
        ],
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise WorkflowError(
            f"could not dissolve malformed native stack {number}: {detail}"
        )


def create_native_stack(pr: dict[str, Any], numbers: list[int]) -> None:
    """Create one native stack from a verified direct-base chain."""
    repo_name = f"{pr['upstream_owner']}/{pr['upstream_repo']}"
    result = run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            "-H",
            "X-GitHub-Api-Version: 2026-03-10",
            f"repos/{repo_name}/stacks",
            "--input",
            "-",
        ],
        input_text=json.dumps({"pull_requests": numbers}),
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise WorkflowError(
            f"could not create repaired native stack {numbers}: {detail}"
        )


def repair_native_stack_topology(
    pr: dict[str, Any], stack: dict[str, Any]
) -> list[list[int]]:
    """Replace one malformed stack with the direct-base chains it contains."""
    segments = linear_stack_segments(stack)
    desired = {
        member["number"]: (
            tuple(item["number"] for item in segment) if len(segment) > 1 else None
        )
        for segment in segments
        for member in segment
    }
    original = tuple(member["number"] for member in stack["members"])
    observed = {
        number: effective_stack_group(member_stack(pr, number), number)
        for number in original
    }
    if all(group == original for group in observed.values()):
        unstack_native_stack(pr, stack["number"])
    elif any(
        group is not None and group != desired[number]
        for number, group in observed.items()
    ):
        raise WorkflowError(
            "native stack membership changed while its malformed topology was being "
            "repaired"
        )

    observed = {
        number: effective_stack_group(member_stack(pr, number), number)
        for number in original
    }
    for segment in segments:
        numbers = [member["number"] for member in segment]
        if len(numbers) < 2:
            continue
        groups = {observed[number] for number in numbers}
        expected = tuple(numbers)
        if groups == {expected}:
            continue
        if groups != {None}:
            raise WorkflowError(
                f"native stack segment {numbers} is only partly repaired"
            )
        create_native_stack(pr, numbers)

    by_number = {member["number"]: member for member in stack["members"]}
    for segment in segments:
        numbers = [member["number"] for member in segment]
        current = member_stack(pr, numbers[0])
        if len(numbers) == 1:
            if effective_stack_group(current, numbers[0]) is not None:
                raise WorkflowError(
                    f"pull request #{numbers[0]} remained in a native stack after repair"
                )
            if current is not None:
                member = current["members"][0]
                frozen = by_number[numbers[0]]
                if (
                    current["trunk"] != frozen["base_branch"]
                    or member["head_branch"] != frozen["head_branch"]
                    or member["base_branch"] != frozen["base_branch"]
                    or member["head_sha"] != frozen["head_sha"]
                ):
                    raise WorkflowError(
                        f"pull request #{numbers[0]} changed while its singleton stack "
                        "wrapper was being verified"
                    )
            continue
        if current is None:
            raise WorkflowError(f"repaired native stack {numbers} is missing")
        current_numbers = [member["number"] for member in current["members"]]
        if current_numbers != numbers:
            raise WorkflowError(
                f"repaired native stack has members {current_numbers}, expected {numbers}"
            )
        if current["trunk"] != segment[0]["base_branch"]:
            raise WorkflowError(
                f"repaired native stack {numbers} targets {current['trunk']!r}, expected "
                f"{segment[0]['base_branch']!r}"
            )
        for member in current["members"]:
            frozen = by_number[member["number"]]
            if (
                member["head_branch"] != frozen["head_branch"]
                or member["base_branch"] != frozen["base_branch"]
                or member["head_sha"] != frozen["head_sha"]
            ):
                raise WorkflowError(
                    f"pull request #{member['number']} changed while its native stack "
                    "topology was being repaired"
                )
    return [[member["number"] for member in segment] for segment in segments]


def fetch_merged_predecessor(workspace: Path, predecessor: dict[str, Any]) -> None:
    """Fetch and verify a merged PR's frozen original head through its pull ref."""
    number = predecessor.get("number")
    expected = predecessor.get("head_sha")
    if not isinstance(number, int) or not isinstance(expected, str) or not expected:
        raise WorkflowError("the merged stack predecessor is incomplete")
    fetched = git_try(
        workspace, "fetch", "--no-tags", "origin", f"refs/pull/{number}/head"
    )
    if fetched.returncode != 0:
        detail = fetched.stderr.strip() or fetched.stdout.strip() or "no output"
        raise WorkflowError(
            f"could not fetch merged predecessor pull request #{number}: {detail}"
        )
    actual = git(workspace, "rev-parse", "FETCH_HEAD")
    if actual != expected:
        raise WorkflowError(
            f"merged predecessor pull request #{number} now resolves to {actual}, "
            f"expected {expected}"
        )


def recover_rewritten_parent_boundary(
    workspace: Path,
    *,
    parent_sha: str,
    child_sha: str,
    merge_base: str,
    historical_base: str,
    parent_branch: str,
    child_branch: str,
) -> str:
    """Find the old parent tip when its commits were replayed onto new history."""
    if not is_ancestor(workspace, historical_base, parent_sha):
        raise WorkflowError(
            f"the declared parent {parent_branch!r} no longer descends from "
            f"the historical base {historical_base} of {child_branch!r}; "
            "the parent may have been rewritten"
        )

    history = []
    expected_parent = merge_base
    for line in git(
        workspace,
        "rev-list",
        "--reverse",
        "--parents",
        f"{merge_base}..{child_sha}",
    ).splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[1] != expected_parent:
            raise WorkflowError(
                f"cannot recover the old parent boundary for {child_branch!r}: "
                "its divergent history is not linear"
            )
        history.append(parts[0])
        expected_parent = parts[0]

    cherry = []
    for line in git(
        workspace, "cherry", parent_sha, child_sha, merge_base
    ).splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[0] not in {"+", "-"}:
            raise WorkflowError(
                f"cannot recover the old parent boundary for {child_branch!r}: "
                "git cherry returned an unreadable result"
            )
        cherry.append((parts[0], parts[1]))
    if [sha for _sign, sha in cherry] != history:
        raise WorkflowError(
            f"cannot recover the old parent boundary for {child_branch!r}: "
            "patch comparison did not cover its linear divergent history"
        )

    boundary = None
    saw_child_commit = False
    for sign, sha in cherry:
        if sign == "-":
            if saw_child_commit:
                raise WorkflowError(
                    f"cannot recover the old parent boundary for {child_branch!r}: "
                    "parent-equivalent and child-only commits are interleaved"
                )
            boundary = sha
        else:
            saw_child_commit = True
    if boundary is None or not saw_child_commit:
        raise WorkflowError(
            f"cannot recover the old parent boundary for {child_branch!r}: "
            "the divergent history does not have a parent-equivalent prefix "
            "followed by child-only commits"
        )
    return boundary


def prepare_stack_cascade(
    workspace: Path, stack: dict[str, Any]
) -> list[dict[str, Any]]:
    """Freeze branch refs and recover one safe historical boundary per layer."""
    validate_stack_snapshot(stack)
    trunk = stack["trunk"]
    trunk_ref = f"refs/remotes/origin/{trunk}"
    try:
        trunk_sha = git(workspace, "rev-parse", trunk_ref)
    except WorkflowError as error:
        raise WorkflowError(
            f"the cascade clone has no remote-tracking ref for trunk {trunk!r}"
        ) from error

    members = stack["members"]
    for index, member in enumerate(members):
        branch = member["head_branch"]
        valid = git_try(workspace, "check-ref-format", "--branch", branch)
        if valid.returncode != 0:
            raise WorkflowError(f"native stack branch {branch!r} is not a valid ref")
        try:
            remote_tip = git(
                workspace, "rev-parse", f"refs/remotes/origin/{branch}"
            )
        except WorkflowError as error:
            raise WorkflowError(
                f"native stack branch {branch!r} is missing from the cascade clone"
            ) from error
        if remote_tip != member["head_sha"]:
            raise WorkflowError(
                f"native stack branch {branch!r} moved before the cascade started: "
                f"expected {member['head_sha']}, found {remote_tip}"
            )
        predecessor = member.get("merged_predecessor") if index == 0 else None
        if predecessor is not None:
            fetch_merged_predecessor(workspace, predecessor)

    plan = []
    for index, member in enumerate(members):
        if index == 0:
            parent_branch = trunk
            parent_sha = trunk_sha
            new_base = trunk_ref
            new_base_ref = trunk_ref
        else:
            parent = members[index - 1]
            parent_branch = parent["head_branch"]
            parent_sha = parent["head_sha"]
            new_base = parent_branch
            new_base_ref = f"refs/heads/{parent_branch}"
        child_sha = member["head_sha"]
        if is_ancestor(workspace, parent_sha, child_sha):
            old_base = parent_sha
        elif index == 0 and member.get("merged_predecessor") is not None:
            predecessor = member["merged_predecessor"]
            old_base = predecessor["head_sha"]
            if predecessor.get("merge_sha") != member["base_sha"]:
                raise WorkflowError(
                    f"merged predecessor pull request #{predecessor['number']} does not "
                    f"match historical base {member['base_sha']}"
                )
            if not is_ancestor(workspace, old_base, child_sha):
                raise WorkflowError(
                    f"the original head {old_base} of merged predecessor pull request "
                    f"#{predecessor['number']} is not an ancestor of "
                    f"{member['head_branch']!r}"
                )
        else:
            historical_base = member["base_sha"]
            try:
                merge_bases = [
                    line
                    for line in git(
                        workspace, "merge-base", "--all", parent_sha, child_sha
                    ).splitlines()
                    if line
                ]
            except WorkflowError as error:
                raise WorkflowError(
                    f"no safe lineage connects {member['head_branch']!r} to its "
                    f"declared parent {parent_branch!r}"
                ) from error
            if not merge_bases:
                raise WorkflowError(
                    f"no safe lineage connects {member['head_branch']!r} to its "
                    f"declared parent {parent_branch!r}"
                )
            if len(merge_bases) != 1:
                raise WorkflowError(
                    f"the lineage between {member['head_branch']!r} and "
                    f"{parent_branch!r} is ambiguous: multiple merge bases exist"
                )
            old_base = merge_bases[0]
            if index == 0:
                # A PR's baseRefOid is a base-branch observation, not its fork point.
                # The bottom layer's merge base is the exact range GitHub reviews.
                old_base = merge_bases[0]
            elif old_base != historical_base and members[index - 1].get(
                "force_pushed"
            ):
                old_base = recover_rewritten_parent_boundary(
                    workspace,
                    parent_sha=parent_sha,
                    child_sha=child_sha,
                    merge_base=old_base,
                    historical_base=historical_base,
                    parent_branch=parent_branch,
                    child_branch=member["head_branch"],
                )
            elif old_base == historical_base and not (
                is_ancestor(workspace, historical_base, parent_sha)
                and is_ancestor(workspace, historical_base, child_sha)
            ):
                raise WorkflowError(
                    f"the historical base {historical_base} is not an ancestor of both "
                    f"{parent_branch!r} and {member['head_branch']!r}"
                )
        plan.append(
            {
                "index": index,
                "number": member["number"],
                "branch": member["head_branch"],
                "branch_ref": f"refs/heads/{member['head_branch']}",
                "head_sha": member["head_sha"],
                "new_base": new_base,
                "new_base_ref": new_base_ref,
                "old_base": old_base,
            }
        )

    for member in members:
        update = git_try(
            workspace,
            "update-ref",
            f"refs/heads/{member['head_branch']}",
            member["head_sha"],
        )
        if update.returncode != 0:
            detail = update.stderr.strip() or update.stdout.strip() or "no output"
            raise WorkflowError(
                f"could not create local branch {member['head_branch']!r}: {detail}"
            )
    stack["trunk_sha"] = trunk_sha
    stack["plan"] = plan
    stack["current_index"] = None
    return plan


def run_stack_member_rebase(
    workspace: Path, member: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"}
    return run(
        [
            "git",
            "-C",
            str(workspace),
            "rebase",
            "--onto",
            member["new_base_ref"],
            member["old_base"],
            member["branch_ref"],
        ],
        check=False,
        env=environment,
    )


def record_rebased_member(workspace: Path, member: dict[str, Any]) -> None:
    """Move the planned local branch to the detached rebase result."""
    rebased = git(workspace, "rev-parse", "HEAD")
    current = git(workspace, "rev-parse", member["branch_ref"])
    if current == rebased:
        return
    if current != member["head_sha"]:
        raise WorkflowError(
            f"local branch {member['branch']!r} moved during its rebase: expected "
            f"{member['head_sha']}, found {current}"
        )
    update = git_try(
        workspace,
        "update-ref",
        member["branch_ref"],
        rebased,
        member["head_sha"],
    )
    if update.returncode != 0:
        detail = update.stderr.strip() or update.stdout.strip() or "no output"
        raise WorkflowError(
            f"could not record rebased branch {member['branch']!r}: {detail}"
        )


def run_stack_cascade(
    workspace: Path, stack: dict[str, Any], start_index: int = 0
) -> subprocess.CompletedProcess[str]:
    """Rebase each planned layer and stop at the first conflict or hard error."""
    output = []
    plan = stack.get("plan") or []
    for member in plan[start_index:]:
        if is_ancestor(workspace, member["new_base_ref"], member["branch_ref"]):
            continue
        process = run_stack_member_rebase(workspace, member)
        output.extend(part for part in (process.stdout, process.stderr) if part)
        if process.returncode == 0:
            try:
                record_rebased_member(workspace, member)
            except WorkflowError as error:
                return subprocess.CompletedProcess(
                    process.args,
                    1,
                    stdout="".join(output),
                    stderr=str(error),
                )
            continue
        if rebase_in_progress(workspace) and unmerged_entries(workspace):
            stack["current_index"] = member["index"]
            return subprocess.CompletedProcess(
                process.args,
                STACK_CONFLICT_EXIT,
                stdout="".join(output),
                stderr="",
            )
        return subprocess.CompletedProcess(
            process.args,
            process.returncode,
            stdout="".join(output),
            stderr="",
        )
    stack["current_index"] = None
    return subprocess.CompletedProcess(
        ["stack-cascade"], 0, stdout="".join(output), stderr=""
    )


def continue_stack_cascade(
    workspace: Path, stack: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    """Continue the conflicted member, then resume the remaining cascade."""
    current = stack.get("current_index")
    if not isinstance(current, int):
        raise WorkflowError("the stack cascade has no conflicted member to continue")
    environment = {**os.environ, "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"}
    continued = run(
        ["git", "-C", str(workspace), "rebase", "--continue"],
        cwd=workspace,
        check=False,
        env=environment,
    )
    if continued.returncode != 0:
        output = continued.stdout + continued.stderr
        if (
            "no changes" in output.lower()
            and rebase_in_progress(workspace)
            and not unmerged_entries(workspace)
        ):
            skipped = run(
                ["git", "-C", str(workspace), "rebase", "--skip"],
                check=False,
                env=environment,
            )
            if skipped.returncode != 0:
                if rebase_in_progress(workspace) and unmerged_entries(workspace):
                    return subprocess.CompletedProcess(
                        skipped.args,
                        STACK_CONFLICT_EXIT,
                        stdout=output + skipped.stdout,
                        stderr=skipped.stderr,
                    )
                return skipped
            record_rebased_member(workspace, stack["plan"][current])
            remainder = run_stack_cascade(workspace, stack, current + 1)
            return subprocess.CompletedProcess(
                remainder.args,
                remainder.returncode,
                stdout=output + skipped.stdout + remainder.stdout,
                stderr=skipped.stderr + remainder.stderr,
            )
        if rebase_in_progress(workspace) and unmerged_entries(workspace):
            return subprocess.CompletedProcess(
                continued.args,
                STACK_CONFLICT_EXIT,
                stdout=continued.stdout,
                stderr=continued.stderr,
            )
        return continued
    record_rebased_member(workspace, stack["plan"][current])
    remainder = run_stack_cascade(workspace, stack, current + 1)
    return subprocess.CompletedProcess(
        remainder.args,
        remainder.returncode,
        stdout=continued.stdout + remainder.stdout,
        stderr=continued.stderr + remainder.stderr,
    )


def capture_member_tips(
    workspace: Path, stack: dict[str, Any]
) -> list[dict[str, Any]]:
    """Read the local tip of every stack member after a clean cascade.

    These are the commits publish must land on the remote, and the only commits
    it may land: a member that ends up on anything else is the cascade's own
    equivalent of the single-branch "nothing else moved" assertion, inverted to
    "each member moved to exactly this".
    """
    tips = []
    for member in stack["members"]:
        rev = git_try(workspace, "rev-parse", f"refs/heads/{member['head_branch']}")
        if rev.returncode != 0:
            raise WorkflowError(
                f"rebased branch {member['head_branch']!r} is missing from the "
                "cascade workspace"
            )
        tips.append(
            {
                "number": member["number"],
                "head_branch": member["head_branch"],
                "head_sha": rev.stdout.strip(),
            }
        )
    return tips


def validate_rebased_stack(
    workspace: Path, stack: dict[str, Any]
) -> list[dict[str, Any]]:
    """Prove every rebased member contains its intended local parent."""
    tips = capture_member_tips(workspace, stack)
    parent = f"refs/remotes/origin/{stack['trunk']}"
    for member in tips:
        if not is_ancestor(workspace, parent, member["head_sha"]):
            raise WorkflowError(
                f"rebased branch {member['head_branch']!r} does not contain its "
                f"expected parent {parent!r}"
            )
        parent = f"refs/heads/{member['head_branch']}"
    return tips


def finish_stack_rebase(
    state_path: Path,
    state: dict[str, Any],
    attempt: dict[str, Any],
    workspace: Path,
    process: subprocess.CompletedProcess[str],
    verb: str,
) -> None:
    """Interpret a helper-owned cascade (or continuation) and record it.

    Exit 0 means the whole cascade is clean; exit 3 means it stopped on a
    conflict a person or the resolution machinery must clear before continuing;
    every other documented code is a setup or state failure named by its cause
    rather than mistaken for a conflict.
    """
    code = process.returncode
    output = (process.stdout + process.stderr).strip()
    stack = attempt["stack"]

    if code == 0:
        try:
            attempt["stack"]["members_after"] = validate_rebased_stack(
                workspace, stack
            )
        except WorkflowError:
            remove_stack_workspace(attempt)
            save_state(state_path, state)
            raise
        attempt["status"] = "resolved"
        attempt["command_output"] = output
        save_state(state_path, state)
        emit(
            {
                "result": "resolved",
                "state": str(state_path),
                "attempt": attempt_summary(attempt),
                "members_after": attempt["stack"]["members_after"],
                "next": "stack-publish",
            }
        )
        return

    if code == STACK_CONFLICT_EXIT:
        conflicts = collect_stack_conflicts(workspace, stack)
        if not conflicts:
            if rebase_in_progress(workspace):
                git_try(workspace, "rebase", "--abort")
            remove_stack_workspace(attempt)
            save_state(state_path, state)
            raise WorkflowError(
                f"the stack {verb} stopped without any unmerged paths: "
                f"{output or 'no output'}"
            )
        attempt["conflicts"] = conflicts
        attempt["conflict_signature"] = conflict_signature(
            conflict["path"] for conflict in conflicts
        )
        attempt["status"] = "conflicted"
        attempt["command_output"] = output
        repeats = detect_no_progress(state["history"], attempt["conflict_signature"])
        result = "no_progress" if repeats >= NO_PROGRESS_LIMIT else "conflicted"
        if result == "no_progress":
            attempt["status"] = "escalated"
            record_escalation(
                state,
                kind="no_progress",
                reason=(
                    f"the last {repeats} finished attempts ended on this same set of "
                    f"conflicted files: "
                    f"{', '.join(conflict['path'] for conflict in conflicts)}"
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
            {"escalation": state.get("escalation"), "next": "resolved"},
        )
        return

    # Any other exit is a setup or state failure, not a conflict. The workspace
    # is removed so a half-cascaded local stack is never mistaken for publishable.
    if rebase_in_progress(workspace):
        git_try(workspace, "rebase", "--abort")
    remove_stack_workspace(attempt)
    save_state(state_path, state)
    raise WorkflowError(
        f"the stack {verb} failed (exit code {code}): {output or 'no output'}"
    )


def command_stack_rebase(args: argparse.Namespace) -> None:
    require_tools()
    state_path = cli_path(args.state)
    state = load_state(state_path)
    attempt = active_attempt(state)
    if attempt.get("strategy") != "stack":
        raise WorkflowError(
            "this attempt is not a native-stack cascade; only preflight on a native "
            "stack starts one"
        )
    if attempt["status"] != "planned":
        raise WorkflowError(
            f"this stack attempt is already {attempt['status']}; run preflight to "
            "start the next one"
        )
    stack = attempt["stack"]
    mismatches = stack_base_mismatches(stack)
    if mismatches:
        stack["topology_mismatches"] = mismatches
        stack["repair_segments"] = [
            [member["number"] for member in segment]
            for segment in linear_stack_segments(stack)
        ]
        save_state(state_path, state)
        segments = repair_native_stack_topology(state["pr"], stack)
        attempt["status"] = "aborted"
        attempt["stack_repair"] = {
            "mismatches": mismatches,
            "segments": segments,
        }
        state["last_stack_repair"] = attempt["stack_repair"]
        archive_attempt(state)
        state["attempt"] = None
        save_state(state_path, state)
        emit(
            {
                "result": "stack_repaired",
                "state": str(state_path),
                "mismatches": mismatches,
                "segments": segments,
                "next": "preflight",
            }
        )
        return
    reference = local_object_source(Path(state["repo_root"]))
    workspace = create_stack_workspace(state["pr"], reference=reference)
    stack["workspace"] = str(workspace)
    # Persist the workspace path before the rebase runs so a later abort can find
    # and remove it even if this process is interrupted mid-cascade.
    save_state(state_path, state)

    try:
        prepare_stack_cascade(workspace, stack)
    except WorkflowError:
        remove_stack_workspace(attempt)
        save_state(state_path, state)
        raise
    save_state(state_path, state)

    process = run_stack_cascade(workspace, stack)
    finish_stack_rebase(state_path, state, attempt, workspace, process, "rebase")


def command_stack_continue(args: argparse.Namespace) -> None:
    require_tools()
    state_path = cli_path(args.state)
    state = load_state(state_path)
    attempt = active_attempt(state)
    if attempt.get("strategy") != "stack":
        raise WorkflowError("this attempt is not a native-stack cascade")
    workspace = attempt_repo_root(state, attempt)
    if attempt["status"] != "conflicted":
        raise WorkflowError(
            f"a stack cascade can only be continued from a conflict, not from "
            f"{attempt['status']}"
        )
    unresolved = [
        conflict["path"]
        for conflict in attempt.get("conflicts") or []
        if conflict.get("status") != "resolved"
    ]
    if unresolved:
        raise WorkflowError(f"these conflicted files are not resolved yet: {unresolved}")
    still_unmerged = [entry["path"] for entry in unmerged_entries(workspace)]
    if still_unmerged:
        raise WorkflowError(
            f"git still reports these paths as unmerged: {still_unmerged}"
        )
    process = continue_stack_cascade(workspace, attempt["stack"])
    finish_stack_rebase(state_path, state, attempt, workspace, process, "rebase --continue")


def command_stack_abort(args: argparse.Namespace) -> None:
    state_path = cli_path(args.state)
    state = load_state(state_path)
    attempt = state.get("attempt")
    undone = None
    if attempt is not None and attempt.get("status") == "published_refs":
        raise WorkflowError(
            "the stack refs are already published; re-run stack-publish to finish "
            "recording the result"
        )
    if attempt is not None and attempt.get("strategy") == "stack":
        workspace = (attempt.get("stack") or {}).get("workspace")
        if workspace and Path(workspace).exists():
            if rebase_in_progress(Path(workspace)):
                git_try(Path(workspace), "rebase", "--abort")
                undone = "stack-rebase"
            remove_stack_workspace(attempt)
    if attempt is not None and attempt.get("status") not in {"published"}:
        attempt["status"] = "aborted"
        archive_attempt(state)
        state["attempt"] = None
    save_state(state_path, state)
    emit(
        {
            "result": "aborted",
            "state": str(state_path),
            "undone": undone,
        }
    )


def atomic_stack_push(
    workspace: Path, stack: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    """Publish only stack members, atomically, with exact pre-cascade leases."""
    intended = stack.get("members_after") or []
    baseline = {
        member["head_branch"]: member["head_sha"] for member in stack["members"]
    }
    command = ["git", "-C", str(workspace), "push", "--atomic"]
    for member in intended:
        branch = member["head_branch"]
        command.append(
            f"--force-with-lease=refs/heads/{branch}:{baseline[branch]}"
        )
    command.append("origin")
    command.extend(
        f"{member['head_sha']}:refs/heads/{member['head_branch']}"
        for member in intended
    )
    return run(command, check=False)


def stack_snapshot_key(stack: dict[str, Any]) -> tuple[Any, ...]:
    """The live facts whose change invalidates a planned cascade."""
    return (
        stack.get("number"),
        stack.get("size"),
        stack.get("trunk"),
        tuple(
            (
                member.get("number"),
                member.get("head_branch"),
                member.get("base_branch"),
                member.get("head_sha"),
            )
            for member in stack.get("members") or []
        ),
    )


def stack_snapshot_fingerprint(stack: dict[str, Any]) -> str:
    encoded = json.dumps(stack_snapshot_key(stack), separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def propagated_stack_fingerprint(
    stack: dict[str, Any], intended: list[dict[str, Any]]
) -> str:
    heads = {member["number"]: member["head_sha"] for member in intended}
    updated = {
        **stack,
        "members": [
            {**member, "head_sha": heads.get(member["number"], member["head_sha"])}
            for member in stack["members"]
        ],
    }
    return stack_snapshot_fingerprint(updated)


def require_current_stack_snapshot(pr: dict[str, Any], stack: dict[str, Any]) -> None:
    """Refuse publication when stack membership or dependents changed."""
    current = stack_membership(pr).get("stack")
    if current is None or stack_snapshot_key(current) != stack_snapshot_key(stack):
        raise WorkflowError(
            "the native stack changed during the cascade; no branch was published"
        )
    dependents = external_stack_dependents(pr, current)
    if dependents:
        listed = "; ".join(
            f"#{item['number']} (targets {item['base_branch']})"
            for item in dependents
        )
        raise WorkflowError(
            "new pull requests outside the native stack now depend on branches "
            f"the cascade would rewrite: {listed}; no branch was published"
        )


def propagation_stack(
    stack: dict[str, Any], fixed_number: int, expected_head: str
) -> dict[str, Any]:
    """Select only descendants above a fixed stack member."""
    validated = {**stack, "invoked_number": fixed_number}
    validate_stack_snapshot(validated)
    members = stack["members"]
    fixed_index = next(
        (index for index, member in enumerate(members) if member["number"] == fixed_number),
        None,
    )
    if fixed_index is None:
        raise WorkflowError(
            f"fixed pull request #{fixed_number} is not a member of native stack "
            f"{stack.get('number')}"
        )
    fixed = members[fixed_index]
    if fixed["head_sha"] != expected_head:
        raise WorkflowError(
            f"fixed pull request #{fixed_number} moved from {expected_head} to "
            f"{fixed['head_sha']}"
        )
    descendants = [dict(member) for member in members[fixed_index + 1 :]]
    return {
        "number": stack["number"],
        "size": len(descendants),
        "trunk": fixed["head_branch"],
        "invoked_number": descendants[0]["number"] if descendants else None,
        "members": descendants,
        "source_snapshot": stack_snapshot_fingerprint(stack),
        "fixed_number": fixed_number,
        "fixed_head_sha": expected_head,
    }


def propagation_landed(
    pr: dict[str, Any], intended: list[dict[str, Any]], *, wait: bool = False
) -> bool:
    return bool(intended) and all(
        (
            wait_for_remote_head(
                pr["upstream_owner"],
                pr["upstream_repo"],
                member["head_branch"],
                member["head_sha"],
            )
            if wait
            else remote_head(
                pr["upstream_owner"],
                pr["upstream_repo"],
                member["head_branch"],
            )
        )
        == member["head_sha"]
        for member in intended
    )


def command_descendant_propagate(args: argparse.Namespace) -> None:
    """Rebase and atomically publish only members above a named fixed PR."""
    require_tools()
    repo_root = resolve_repo_root(args.repo_root)
    target_value = args.target
    if target_value is None and args.repo and args.pull_request is not None:
        target_value = f"{args.repo}#{args.pull_request}"
    if target_value is None:
        raise WorkflowError(
            "descendant-propagate needs a target or both --repo and --pull-request"
        )
    target = parse_target(target_value)
    fixed_pr = args.fixed_pr if args.fixed_pr is not None else args.pull_request
    expected_head = args.expected_head or args.head_sha
    if fixed_pr is None or not expected_head:
        raise WorkflowError(
            "descendant-propagate needs --fixed-pr and --expected-head, or "
            "--pull-request and --head-sha"
        )
    pr = metadata_for(target)
    require_open_pull_request(pr)
    detection = stack_membership(pr)
    stack = detection.get("stack")
    if stack is None:
        raise WorkflowError(f"{target['pr_url']} is not in a native GitHub stack")
    if args.stack_number is not None and stack.get("number") != args.stack_number:
        raise WorkflowError(
            f"pull request #{target['number']} is in native stack {stack.get('number')}, "
            f"not {args.stack_number}"
        )
    partial = propagation_stack(stack, fixed_pr, expected_head)
    if not partial["members"]:
        emit(
            {
                "result": "no_descendants",
                "stack_number": stack["number"],
                "fixed_pr": fixed_pr,
                "fixed_head_sha": expected_head,
                "members_published": [],
            }
        )
        return
    external = external_stack_dependents(pr, partial)
    if external:
        listed = "; ".join(
            f"#{item['number']} (targets {item['base_branch']})" for item in external
        )
        raise WorkflowError(
            "pull requests outside the native stack depend on branches the propagation "
            f"would rewrite: {listed}"
        )

    state_path = (
        cli_path(args.state)
        if args.state
        else default_propagation_state_path(target, stack["number"], fixed_pr)
    )
    prior = load_state(state_path) if state_path.is_file() else None
    resume_resolved = False
    if prior is not None and prior.get("operation") == "descendant_propagation":
        intended = prior.get("members_after") or []
        if (
            prior.get("expected_post_fingerprint")
            == stack_snapshot_fingerprint(stack)
            and propagation_landed(pr, intended)
        ):
            workspace = prior.get("workspace")
            if isinstance(workspace, str) and Path(workspace).exists():
                force_rmtree(Path(workspace))
            prior["status"] = "published"
            prior["workspace"] = None
            save_state(state_path, prior)
            emit(
                {
                    "result": "published",
                    "recovered": True,
                    "state": str(state_path),
                    "stack_number": stack["number"],
                    "fixed_pr": fixed_pr,
                    "fixed_head_sha": expected_head,
                    "members_published": intended,
                }
            )
            return
        prior_workspace = prior.get("workspace")
        if (
            prior.get("status") == "resolved"
            and prior.get("stack_number") == stack["number"]
            and prior.get("fixed_pr") == fixed_pr
            and prior.get("fixed_head_sha") == expected_head
            and isinstance(prior_workspace, str)
            and Path(prior_workspace).exists()
        ):
            resume_resolved = True
        if (
            not resume_resolved
            and prior.get("status") in {"planned", "resolved", "published_refs"}
            and isinstance(prior_workspace, str)
            and Path(prior_workspace).exists()
        ):
            raise WorkflowError(
                "an earlier descendant propagation still owns a preserved workspace "
                f"at {prior_workspace}; recover or remove that run before starting another"
            )

    if resume_resolved:
        state = prior
        workspace = Path(state["workspace"])
        intended = state.get("members_after") or []
        partial["members_after"] = intended
        if not intended:
            raise WorkflowError(
                "the resolved descendant propagation has no recorded member tips"
            )
    else:
        state = {
            "version": STATE_VERSION,
            "created_at": utc_now(),
            "operation": "descendant_propagation",
            "status": "planned",
            "repo_root": str(repo_root),
            "pr": pr,
            "stack_number": stack["number"],
            "fixed_pr": fixed_pr,
            "fixed_head_sha": expected_head,
            "source_snapshot": stack_snapshot_fingerprint(stack),
            "members_before": partial["members"],
            "members_after": None,
            "expected_post_fingerprint": None,
            "workspace": None,
        }
        save_state(state_path, state)
        workspace = create_stack_workspace(
            pr, reference=local_object_source(repo_root)
        )
        state["workspace"] = str(workspace)
        save_state(state_path, state)
    try:
        if not resume_resolved:
            prepare_stack_cascade(workspace, partial)
            process = run_stack_cascade(workspace, partial)
            if process.returncode == STACK_CONFLICT_EXIT:
                state["status"] = "conflicted"
                state["detail"] = (process.stdout + process.stderr).strip()
                remove_stack_workspace({"stack": state})
                save_state(state_path, state)
                emit(
                    {
                        "result": "conflicted",
                        "state": str(state_path),
                        "stack_number": stack["number"],
                        "fixed_pr": fixed_pr,
                        "members_published": [],
                    }
                )
                return
            if process.returncode != 0:
                detail = (process.stdout + process.stderr).strip() or "no output"
                raise WorkflowError(
                    f"descendant propagation failed before publication "
                    f"(exit code {process.returncode}): {detail}"
                )
            intended = validate_rebased_stack(workspace, partial)
            partial["members_after"] = intended
            state["members_after"] = intended
            state["expected_post_fingerprint"] = propagated_stack_fingerprint(
                stack, intended
            )
            state["status"] = "resolved"
            save_state(state_path, state)
        else:
            captured = validate_rebased_stack(workspace, partial)
            if captured != intended:
                raise WorkflowError(
                    "the preserved descendant propagation workspace changed after "
                    "resolution; start a new whole-stack pass"
                )

        current_pr = metadata_for(target)
        require_open_pull_request(current_pr)
        current = stack_membership(current_pr).get("stack")
        if current is None or stack_snapshot_key(current) != stack_snapshot_key(stack):
            raise WorkflowError(
                "the native stack changed during descendant propagation; no branch "
                "was published"
            )
        current_partial = propagation_stack(current, fixed_pr, expected_head)
        current_external = external_stack_dependents(current_pr, current_partial)
        if current_external:
            listed = "; ".join(
                f"#{item['number']} (targets {item['base_branch']})"
                for item in current_external
            )
            raise WorkflowError(
                "pull requests outside the native stack began depending on branches "
                f"the propagation would rewrite: {listed}"
            )
        push = atomic_stack_push(workspace, partial)
        if push.returncode != 0 and not propagation_landed(pr, intended):
            detail = push.stderr.strip() or push.stdout.strip() or "no output"
            raise WorkflowError(f"atomic descendant propagation failed: {detail}")
        if not propagation_landed(pr, intended, wait=True):
            raise WorkflowError(
                "the atomic descendant propagation did not land every intended ref"
            )
        state["status"] = "published_refs"
        save_state(state_path, state)
        force_rmtree(workspace)
        state["workspace"] = None
        state["status"] = "published"
        save_state(state_path, state)
        emit(
            {
                "result": "published",
                "recovered": False,
                "state": str(state_path),
                "stack_number": stack["number"],
                "fixed_pr": fixed_pr,
                "fixed_head_sha": expected_head,
                "members_published": intended,
            }
        )
    except BaseException:
        if state.get("status") not in {"resolved", "published_refs"}:
            if workspace.exists():
                force_rmtree(workspace)
            state["workspace"] = None
            save_state(state_path, state)
        elif workspace.exists():
            dissociate_workspace(workspace)
            save_state(state_path, state)
        raise


def command_stack_publish(args: argparse.Namespace) -> None:
    require_tools()
    state_path = cli_path(args.state)
    state = load_state(state_path)
    attempt = active_attempt(state)
    pr = state["pr"]
    if attempt.get("strategy") != "stack":
        raise WorkflowError("this attempt is not a native-stack cascade")
    if attempt["status"] not in {"resolved", "published_refs"}:
        raise WorkflowError(
            f"only a resolved stack cascade can be published or finalized; this one is "
            f"{attempt['status']}"
        )
    stack = attempt["stack"]
    intended = stack.get("members_after")
    if not intended:
        raise WorkflowError(
            "no rebased member tips were recorded; run stack-rebase to completion "
            "before publishing"
        )

    if attempt["status"] == "resolved":
        workspace = attempt_repo_root(state, attempt)
        captured = validate_rebased_stack(workspace, stack)
        if captured != intended:
            raise WorkflowError(
                "a stack branch moved in the cascade workspace after validation; "
                "run stack-rebase again"
            )
        require_current_stack_snapshot(pr, stack)
        trunk_remote = remote_head(
            pr["upstream_owner"], pr["upstream_repo"], stack["trunk"]
        )
        if trunk_remote != stack.get("trunk_sha"):
            raise WorkflowError(
                f"the stack trunk {stack['trunk']!r} moved during the cascade: "
                f"expected {stack.get('trunk_sha')}, found {trunk_remote}; no branch "
                "was published"
            )

        push = atomic_stack_push(workspace, stack)
        push_detail = push.stderr.strip() or push.stdout.strip() or "no output"
        mismatched = []
        for member in intended:
            remote = wait_for_remote_head(
                pr["upstream_owner"],
                pr["upstream_repo"],
                member["head_branch"],
                member["head_sha"],
            )
            if remote == member["head_sha"]:
                continue
            mismatched.append(
                {
                    "number": member["number"],
                    "head_branch": member["head_branch"],
                    "expected": member["head_sha"],
                    "actual": remote,
                }
            )
        trunk_after = remote_head(
            pr["upstream_owner"], pr["upstream_repo"], stack["trunk"]
        )
        if mismatched:
            # The atomic push either moves every member or none. Preserve a
            # self-contained workspace for inspection and require a new preflight.
            borrow = dissociate_workspace(Path(workspace))
            mismatch_desc = "; ".join(
                f"#{item['number']} {item['head_branch']} is at "
                f"{item['actual'] or 'a missing branch'} not {item['expected']}"
                for item in mismatched
            )
            message = (
                "the atomic stack publish did not land the complete intended stack. "
                f"Remote verification: {mismatch_desc}. The cascade workspace is "
                f"preserved at {workspace}. Run preflight again before another "
                f"publish. Git reported: {push_detail}"
            )
            if borrow is not None:
                message += f" ({borrow})"
            save_state(state_path, state)
            raise WorkflowError(message)

        invoked = next(
            (
                member
                for member in intended
                if member["number"] == stack["invoked_number"]
            ),
            None,
        )
        notes = []
        if push.returncode != 0:
            notes.append(f"git reported after all refs landed: {push_detail}")
        if trunk_after != stack.get("trunk_sha"):
            notes.append(
                f"the trunk moved from {stack.get('trunk_sha')} to {trunk_after} "
                "during publication"
            )
        attempt["status"] = "published_refs"
        attempt["stack_push_detail"] = "; ".join(notes) if notes else None
        attempt["published_head_sha"] = None if invoked is None else invoked["head_sha"]
        attempt["stack"]["members_published"] = intended
        attempt["stack"]["trunk_after_publish"] = trunk_after
        save_state(state_path, state)

    remove_stack_workspace(attempt)
    save_state(state_path, state)

    target = parse_target(pr["pr_url"])
    expected_invoked = attempt["published_head_sha"]
    final = live_mergeability(target, expected_head=expected_invoked)
    mergeability = classify_mergeability(final, expected_head=expected_invoked)
    record_stack_member_clearances(state, intended, stack["invoked_number"])
    attempt["status"] = "published"
    attempt["mergeable"] = final.get("mergeable")
    attempt["merge_state_status"] = final.get("merge_state_status")
    attempt["mergeable_at_head_sha"] = (
        attempt["published_head_sha"] if mergeability == "mergeable" else None
    )
    state["pr"] = final
    state["iterations"] = int(state.get("iterations", 0)) + 1
    archive_attempt(state)
    save_state(state_path, state)
    emit(
        {
            "result": "published",
            "state": str(state_path),
            "members_published": intended,
            "invoked_head_sha": attempt["published_head_sha"],
            "mergeability": mergeability,
            "mergeable_at_head_sha": attempt["mergeable_at_head_sha"],
            "iterations": state["iterations"],
            **(
                {"push_detail": attempt["stack_push_detail"]}
                if attempt.get("stack_push_detail")
                else {}
            ),
        }
    )


def record_stack_member_clearances(
    stack_state: dict[str, Any],
    members: list[dict[str, Any]],
    invoked_number: int,
) -> None:
    """Persist ordinary conflict-stage clearance for each published stack member."""
    for member in members:
        if member["number"] == invoked_number:
            continue
        target = stack_member_target(stack_state["pr"], member["number"])
        metadata = live_mergeability(target, expected_head=member["head_sha"])
        mergeability = classify_mergeability(
            metadata, expected_head=member["head_sha"]
        )
        path = default_state_path(target)
        prior = load_state(path) if path.is_file() else None
        projected = prior or {
            "version": STATE_VERSION,
            "created_at": utc_now(),
            "iterations": 0,
            "history": [],
        }
        archive_attempt(projected)
        projected["pr"] = metadata
        projected["escalation"] = None
        projected["iterations"] = int(projected.get("iterations", 0)) + 1
        projected["attempt"] = {
            "iteration": projected["iterations"],
            "status": "mergeable" if mergeability == "mergeable" else "escalated",
            "base_sha": metadata.get("base_sha"),
            "mergeable_at_head_sha": (
                member["head_sha"] if mergeability == "mergeable" else None
            ),
            "published_head_sha": member["head_sha"],
            "strategy": "stack",
            "stack_source_pr": invoked_number,
        }
        archive_attempt(projected)
        save_state(path, projected)


def cleared_head_sha(state: dict[str, Any] | None) -> str | None:
    """The commit a run cleared the pull request at, or None when no run cleared one.

    A clearance is only worth reporting alongside the commit it was read at, so this
    is the single fact behind both. A reader that cannot tie the clearance to a head
    cannot check it against the head being recorded, and an unattached clearance
    defeats that check without looking like it did.
    """
    if not state or state.get("escalation"):
        return None
    attempt = state.get("attempt") or {}
    marker = attempt.get("mergeable_at_head_sha")
    if not marker:
        return None
    status = attempt.get("status")
    if status == "mergeable":
        return marker
    if status == "published" and marker == attempt.get("published_head_sha"):
        return marker
    return None


def stage_outcome(state: dict[str, Any] | None) -> str | None:
    """Name how a run ended, in the vocabulary an orchestrator reads.

    This says how the run ended. It is never a claim that the pull request merges.
    Whether this stage is green is decided from GitHub's live mergeability, and a
    disagreement between the two is this field being wrong rather than the live
    answer being wrong.

    Only an ending some command actually recorded gets a word. Nothing here is
    inferred from the shape of the state, because `preflight` writes the state before
    any work happens, so a run killed at any point leaves a state byte-identical to
    one still going. Reading a mid-flight state as a failed run is not detecting a
    crash; the information to tell those apart was never written.

    That distinction decides who wins a disagreement. A caller prefers this word over
    its own reading, on the grounds that the run watched itself. That holds for a
    record and inverts for a guess: a guess made from a state file would outrank the
    live agent that actually watched the run, so a guess must be absence instead.

    An ending that was recorded but is not one of the recognized ones still reports
    escalated. That is evidence of an ending nobody can describe, which is worth a
    person's attention, and not the same as having no evidence at all.

    A run that spent its own iteration cap reports carried. The cap bounds one pass
    of the orchestrator, and the orchestrator gives the stage the rest of its
    budget on the next pass rather than ending the run there.

    With no state at all there is likewise no run to describe. A stage that was never
    launched and one that finished and cleaned up after itself both look like this,
    and neither of them made no progress.
    """
    if not state:
        return None
    escalation = state.get("escalation")
    if escalation:
        kind = escalation.get("kind")
        if kind == "no_progress":
            return "no_progress"
        if kind == "max_iterations":
            return "carried"
        return "escalated"
    status = (state.get("attempt") or {}).get("status")
    if status not in RECORDED_ENDINGS:
        return None
    return "cleared" if cleared_head_sha(state) else "escalated"


def with_stage_outcome(payload: dict[str, Any], state: dict[str, Any] | None) -> dict[str, Any]:
    """Add the run's outcome to a payload, and only when a command recorded one.

    A cleared run carries the commit it cleared at in the same payload, so a reader
    never has one without the other.
    """
    outcome = stage_outcome(state)
    if outcome is None:
        return payload
    payload["stage_outcome"] = outcome
    if outcome == "cleared":
        payload["mergeable_at_head_sha"] = cleared_head_sha(state)
    return payload


def command_status(args: argparse.Namespace) -> None:
    if args.current:
        require_tools()
        repo_root = resolve_repo_root(args.repo_root)
        target = current_pr_target(repo_root)
        path = default_state_path(target)
        if not path.is_file():
            emit(
                with_stage_outcome(
                    {
                        "result": "no_state",
                        "state": str(path),
                        "pr": {"number": target["number"], "url": target["pr_url"]},
                        "attempt": None,
                        "escalation": None,
                        "history": [],
                    },
                    None,
                )
            )
            return
    else:
        path = cli_path(args.state)
    state = load_state(path)
    pr = state["pr"]
    attempt = state.get("attempt")
    history = state.get("history") or []
    payload = with_stage_outcome(
        {
            "result": "ready",
            "state": str(path),
            "pr": pr,
            "attempt": attempt,
            "relations": state.get("relations"),
            "merge_methods": state.get("merge_methods"),
            "escalation": state.get("escalation"),
            "history": history,
            "iterations": int(state.get("iterations", 0)),
            "last_helper_activity": last_helper_activity(state),
        },
        state,
    )
    status_path = status_path_for(path)
    write_result_file(status_path, payload, "status")
    emit(
        with_stage_outcome(
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
                "last_helper_activity": last_helper_activity(state),
            },
            state,
        )
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
        help=(
            "PR URL or owner/repo#number; omit only from a worktree attached to "
            "the PR's branch"
        ),
    )
    preflight.add_argument("--repo-root")
    preflight.add_argument("--state")
    preflight.add_argument("--strategy", choices=list(STRATEGIES), default="auto")
    preflight.add_argument(
        "--whole-stack",
        action="store_true",
        help=(
            "inspect every native-stack member even when the invoked pull request "
            "is already mergeable"
        ),
    )
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
    resolved.add_argument(
        "--companion-paths",
        nargs="+",
        help=(
            "non-conflicted files that the commit being replayed also touches and "
            "that must change to preserve both sides"
        ),
    )
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
        "--accept-line-endings",
        action="store_true",
        help="allow a resolution that changes the file's line endings",
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

    stack_rebase = subparsers.add_parser(
        "stack-rebase",
        help="cascade a rebase through the trunk for a native GitHub stack",
    )
    stack_rebase.add_argument("--state", required=True)
    stack_rebase.set_defaults(function=command_stack_rebase)

    stack_continue = subparsers.add_parser(
        "stack-continue",
        help="continue a cascading rebase after resolving its conflicted files",
    )
    stack_continue.add_argument("--state", required=True)
    stack_continue.set_defaults(function=command_stack_continue)

    stack_abort = subparsers.add_parser(
        "stack-abort",
        help="abort a cascading rebase and remove its scratch clone",
    )
    stack_abort.add_argument("--state", required=True)
    stack_abort.set_defaults(function=command_stack_abort)

    stack_publish = subparsers.add_parser(
        "stack-publish",
        help="push every stack member and verify each landed on its rebased tip",
    )
    stack_publish.add_argument("--state", required=True)
    stack_publish.set_defaults(function=command_stack_publish)

    propagate = subparsers.add_parser(
        "descendant-propagate",
        help="atomically rebase and publish only descendants above a fixed stack PR",
    )
    propagate.add_argument(
        "target",
        nargs="?",
        help=(
            "PR URL or owner/repo#number; omit only from a worktree attached to "
            "the PR's branch"
        ),
    )
    propagate.add_argument("--repo")
    propagate.add_argument("--pull-request", type=int)
    propagate.add_argument("--head-sha")
    propagate.add_argument("--stack-number", type=int)
    propagate.add_argument("--fixed-pr", type=int)
    propagate.add_argument("--expected-head")
    propagate.add_argument("--repo-root")
    propagate.add_argument("--state")
    propagate.set_defaults(function=command_descendant_propagate)

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
