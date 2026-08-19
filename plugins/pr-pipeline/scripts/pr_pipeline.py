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
CHECK_SETTLE_GRACE_SECONDS = 180
CHECK_COVERAGE_DEADLINE_SECONDS = 1800
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
#
# ``model`` names the model this stage runs best on. A stage that leaves it None
# runs on DEFAULT_STAGE_MODEL. It is a starting point rather than a rule: a
# ``--stage-model`` override at preflight beats it, and the ``requires_family``
# gate still checks whatever model the stage ends up with.
STAGES: tuple[dict[str, Any], ...] = (
    {
        "stage": STAGE_CONFLICT,
        "plugin": STAGE_CONFLICT,
        "agent": f"{STAGE_CONFLICT}:{STAGE_CONFLICT}",
        "module": "conflict_fix_loop",
        "evidence": "github",
        "requires_family": None,
        "model": None,
        "summary": "resolve merge conflicts with the base branch",
    },
    {
        "stage": STAGE_SELF_REVIEW,
        "plugin": STAGE_SELF_REVIEW,
        "agent": f"{STAGE_SELF_REVIEW}:{STAGE_SELF_REVIEW}",
        "module": "self_review_loop",
        "evidence": "helper",
        "requires_family": CLAUDE_FAMILY,
        "model": None,
        "summary": "review the diff and commit the verified fixes",
    },
    {
        "stage": STAGE_COPILOT_REVIEW,
        "plugin": STAGE_COPILOT_REVIEW,
        "agent": f"{STAGE_COPILOT_REVIEW}:{STAGE_COPILOT_REVIEW}",
        "module": "copilot_review_loop",
        "evidence": "helper",
        "requires_family": None,
        "model": None,
        "summary": "address the Copilot review comments",
    },
    {
        "stage": STAGE_CI,
        "plugin": STAGE_CI,
        "agent": f"{STAGE_CI}:{STAGE_CI}",
        "module": "ci_fix_loop",
        "evidence": "github",
        "requires_family": None,
        "model": None,
        "summary": "fix the failing checks this pull request caused",
    },
    {
        "stage": STAGE_DESCRIPTION,
        "plugin": STAGE_DESCRIPTION,
        "agent": f"{STAGE_DESCRIPTION}:{STAGE_DESCRIPTION}",
        "module": "pr_description",
        "evidence": "helper",
        "requires_family": None,
        "model": None,
        "summary": "validate or replace the title and description",
    },
)
STAGE_NAMES = tuple(entry["stage"] for entry in STAGES)
STAGE_BY_NAME = {entry["stage"]: entry for entry in STAGES}
STAGE_INDEX = {entry["stage"]: index for index, entry in enumerate(STAGES)}
HELPER_EVIDENCE_STAGES = tuple(
    entry["stage"] for entry in STAGES if entry["evidence"] == "helper"
)

STAGE_OUTCOMES = ("cleared", "skipped", "no_progress", "escalated")
CLEARING_OUTCOMES = ("cleared", "skipped")

ESCALATION_ACTIONS = {
    "checks_never_registered": (
        "Check whether the repository skips these checks on a draft pull "
        "request. If it does, take the pull request out of draft yourself, or "
        "start the pipeline again once the checks can run."
    ),
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


def parse_utc(value: Any) -> dt.datetime | None:
    """Read one of this helper's own timestamps back, or give up quietly."""

    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def elapsed_seconds(start: Any, end: Any) -> float | None:
    """Seconds between two recorded timestamps, or ``None`` if either is unreadable."""

    first = parse_utc(start)
    second = parse_utc(end)
    if first is None or second is None:
        return None
    return max((second - first).total_seconds(), 0.0)


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


def stage_installed(entry: dict[str, Any]) -> bool:
    """Report whether a stage's plugin is installed.

    Every stage needs this, including the two whose greenness comes from GitHub.
    Being installed and being green are separate facts: a passing check rollup
    says nothing about whether the agent that fixes checks can be launched. An
    unresolvable plugin-qualified agent name falls back to the default agent
    without an error, so a stage that is not installed has to stop the pipeline
    rather than launch a general-purpose agent against a real pull request.
    """

    return stage_script_path(entry).is_file()


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


def corroborate_mergeability(mergeable: Any) -> dict[str, Any]:
    """Judge whether GitHub's mergeability answer has settled.

    GitHub computes mergeability in the background, and the result lags the head
    the pull request already reports. One response can therefore carry a fresh
    ``headRefOid`` beside a settled ``MERGEABLE`` or ``CONFLICTING`` computed
    against the head it replaced. ``UNKNOWN`` covers only the interval while
    GitHub recomputes, so waiting for ``UNKNOWN`` to clear does not rule that
    out, and nothing here can. The only thing that narrows the window is the
    caller refusing the first read after the head moved.

    ``mergeStateStatus`` is deliberately not consulted. It was once required to
    agree with ``mergeable`` before a mergeable answer could be trusted, on the
    theory that a self-contradicting response is one to throw away. Measurement
    killed that theory: across 81 open draft pull requests the two fields agreed
    every time, with ``CONFLICTING`` always paired with ``DIRTY`` and ``UNKNOWN``
    always paired with ``UNKNOWN``. They are two views of one asynchronous
    computation and they go stale together, so requiring agreement cannot catch
    the stale answer the guard existed to catch. A check that can never fire is
    worse than no check, because the next reader counts it as a defense that has
    been holding all along. The field is still recorded; it decides nothing.

    So the residual window is open, and this says so rather than implying
    otherwise. No GitHub field states which commit a mergeability answer was
    computed at, so no caller can prove that a response *about* a head carries
    an answer computed *at* it.
    """

    mergeable_value = str(mergeable or "").strip().upper()
    fields = {"mergeable": mergeable_value or None}

    if mergeable_value == "CONFLICTING":
        return {**fields, "state": "conflicting", "settled": True, "reason": "settled"}

    if mergeable_value == "MERGEABLE":
        return {**fields, "state": "mergeable", "settled": True, "reason": "settled"}

    return {
        **fields,
        "state": "unsettled",
        "settled": False,
        "reason": (
            "mergeable_unknown"
            if mergeable_value in ("", "UNKNOWN")
            else "unrecognized"
        ),
    }


def observe_pull_request(
    target: dict[str, Any], *, known_head_sha: str | None = None
) -> dict[str, Any]:
    """Read every live GitHub fact the stage decisions depend on.

    Two of these facts are not simply true when GitHub returns them, and this
    reads more than once for both.

    Mergeability is computed in the background and lags the head the pull
    request already reports, so a response can carry a fresh ``headRefOid``
    beside an answer computed against the head it replaced. A response taken
    right after the head moved is therefore refused and asked again after a
    delay. That is the whole of the defense: the two mergeability fields GitHub
    returns are two views of one computation and go stale together, so no
    agreement between them can stand in for freshness.

    The check rollup is not stale in that way. Each check run belongs to a
    commit, so the rollup genuinely describes this head. It can still be
    *incomplete*: right after a push, GitHub may have registered only the
    quickest workflows, and a rollup with two passing entries and nothing else
    yet looks exactly like a finished green one. The first read after a push is
    refused for that reason too, and coverage is judged separately against the
    contexts the base branch declares as required.

    This narrows both windows; it closes neither. No GitHub field states which
    commit a mergeability answer was computed at, so no caller can prove that a
    response *about* a head holds an answer computed *at* it, and no field
    states how many checks a commit will eventually run. What the pipeline
    promises is only the safe direction: an answer it cannot corroborate leaves
    the stage not green, which costs one stage run that reads a real answer and
    stops.
    """

    fields = (
        "number,title,url,state,isDraft,mergeable,mergeStateStatus,headRefName,"
        "headRefOid,headRepositoryOwner,headRepository,baseRefName,baseRefOid,"
        "statusCheckRollup"
    )
    payload: dict[str, Any] = {}
    mergeability: dict[str, Any] = {}
    previous_head = known_head_sha
    head_moved = False
    moved = False
    attempts = 0
    for delay in (*MERGEABLE_RETRY_DELAYS, None):
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
        attempts += 1
        observed_head = payload.get("headRefOid")
        moved = bool(previous_head) and observed_head != previous_head
        head_moved = head_moved or moved
        previous_head = observed_head
        mergeability = corroborate_mergeability(payload.get("mergeable"))
        if delay is None:
            break
        if moved:
            # The head changed under the pipeline. This response's mergeability
            # may predate the commit it arrived with, and its rollup may hold
            # only the checks that registered first. Ask again rather than
            # accept either on the first read after a push.
            time.sleep(delay)
            continue
        if mergeability["settled"]:
            break
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
    base_sha = payload.get("baseRefOid")
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
        "base_sha": base_sha,
        "mergeable": payload.get("mergeable"),
        "merge_state_status": payload.get("mergeStateStatus"),
        "mergeability": mergeability,
        "checks": summarize_checks(payload.get("statusCheckRollup")),
        "reads": {
            "attempts": attempts,
            "head_moved": head_moved,
            "head_moved_on_last_read": moved,
        },
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

    The names are kept because a rollup can be complete in shape and incomplete
    in coverage. Right after a push GitHub may have registered only the quickest
    workflows, and a rollup holding two passing entries with nothing failing and
    nothing pending is indistinguishable here from a finished green one. Judging
    that needs to know which checks the branch *declares*, which comes from the
    repository's rulesets rather than from this response.
    ``judge_check_coverage`` applies it.
    """

    nodes = rollup if isinstance(rollup, list) else []
    counts: dict[str, int] = {}
    failing: list[dict[str, str]] = []
    pending: list[dict[str, str]] = []
    names: set[str] = set()
    for node in nodes:
        conclusion = check_conclusion(node)
        counts[conclusion] = counts.get(conclusion, 0) + 1
        name = ""
        if isinstance(node, dict):
            name = str(node.get("name") or node.get("context") or "")
        if name:
            names.add(name)
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
        "names": sorted(names),
        "coverage": {
            "state": "unsatisfied",
            "source": "none",
            "reason": "not_judged",
            "missing": [],
            "declared": [],
        },
        "action_required": [
            entry for entry in failing if entry["state"] == "ACTION_REQUIRED"
        ],
    }


def required_contexts(target: dict[str, Any], base_branch: Any) -> dict[str, Any]:
    """Read which status checks the base branch *declares* as required.

    Absence of a check name only means "has not arrived yet" for a check the
    repository said would run. An inferred expectation cannot carry that
    meaning: neither the base commit's checks nor the pull request's previous
    head tell you what this head is supposed to produce, so a name missing from
    either is indistinguishable from a name that was never coming.

    The ruleset endpoint is the one that answers the declared question. It reads
    with plain read access, and it returns the active rules from every ruleset
    that applies to the branch, so the required contexts are the union across
    every ``required_status_checks`` rule rather than the first one found.

    The classic branch-protection endpoint is deliberately not used. A
    repository governed by rulesets rather than by classic protection answers it
    with ``404``, which is indistinguishable from an unprotected branch, so it
    fails quietly and wrongly on exactly the repositories this pipeline runs
    against most.

    Nothing declared is a normal answer rather than a fault. A private
    repository on a free plan answers ``403``, a branch with no rules answers
    ``404`` or an empty list, and a branch with rules may declare no required
    checks at all. Every one of those leaves the answer unavailable, and
    coverage falls back to waiting for the head to settle.
    """

    if not isinstance(base_branch, str) or not base_branch:
        return {"available": False, "reason": "no_base_branch", "contexts": []}
    try:
        response = gh_json(
            [
                "api",
                f"repos/{target['repo_name']}/rules/branches/{base_branch}",
            ]
        )
    except WorkflowError as error:
        detail = str(error)
        reason = "not_available_here" if "403" in detail else "lookup_failed"
        if "404" in detail:
            reason = "no_rules"
        return {
            "available": False,
            "reason": reason,
            "detail": detail,
            "contexts": [],
        }

    contexts: set[str] = set()
    for rule in response if isinstance(response, list) else []:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        parameters = rule.get("parameters")
        if not isinstance(parameters, dict):
            continue
        for check in parameters.get("required_status_checks") or []:
            if not isinstance(check, dict):
                continue
            context = check.get("context")
            if isinstance(context, str) and context:
                contexts.add(context)
    if not contexts:
        return {"available": False, "reason": "none_declared", "contexts": []}
    return {"available": True, "reason": "declared", "contexts": sorted(contexts)}


def judge_check_coverage(
    names: set[str],
    required: Any,
    *,
    head_age_seconds: float | None,
    grace_seconds: int = CHECK_SETTLE_GRACE_SECONDS,
    deadline_seconds: int = CHECK_COVERAGE_DEADLINE_SECONDS,
) -> dict[str, Any]:
    """Judge whether the head's rollup is complete enough to be believed.

    A rollup can be complete in shape and incomplete in coverage. Right after a
    push GitHub may have registered only the quickest workflows, and a rollup
    holding two passing entries with nothing failing and nothing pending looks
    exactly like a finished green one.

    Where the base branch declares required contexts, coverage is answered
    exactly: every declared context must appear in the rollup. A declared
    context that is missing has not registered yet, and its absence is
    meaningful because the repository said it would be there.

    Where nothing is declared, coverage degrades to a question about time: the
    head must have been under observation for ``grace_seconds`` before a passing
    rollup is believed. Time always passes, so this fallback cannot hold a stage
    forever.

    Comparison against an inferred set is not used in either path, and the
    absence of a check nobody declared never holds the pipeline. A check that is
    *present* still counts wherever it came from, so a failing check outside the
    declared set routes to the check stage as it always did.

    The declared path is bounded too. A repository that skips its checks on
    draft pull requests would otherwise wait for a context that is never coming,
    and this pipeline works exclusively on drafts. After
    ``deadline_seconds`` the missing contexts stop being a wait and become an
    escalation that names them.
    """

    declared: set[str] = set()
    if isinstance(required, dict) and required.get("available"):
        declared = {name for name in required.get("contexts") or [] if name}

    if declared:
        missing = sorted(declared - names)
        if not missing:
            return {
                "state": "satisfied",
                "source": "declared",
                "reason": "required_contexts_present",
                "missing": [],
                "declared": sorted(declared),
            }
        age = None if head_age_seconds is None else float(head_age_seconds)
        if age is not None and age >= deadline_seconds:
            return {
                "state": "overdue",
                "source": "declared",
                "reason": "required_contexts_never_registered",
                "missing": missing,
                "declared": sorted(declared),
                "head_age_seconds": age,
                "deadline_seconds": deadline_seconds,
            }
        return {
            "state": "unsatisfied",
            "source": "declared",
            "reason": "required_contexts_missing",
            "missing": missing,
            "declared": sorted(declared),
            "head_age_seconds": age,
            "deadline_seconds": deadline_seconds,
        }

    reason = "none_declared"
    if isinstance(required, dict) and required.get("reason"):
        reason = str(required["reason"])
    if head_age_seconds is None:
        return {
            "state": "satisfied",
            "source": "age",
            "reason": "age_not_measurable",
            "missing": [],
            "declared": [],
            "required_reason": reason,
        }
    age = float(head_age_seconds)
    state = "satisfied" if age >= grace_seconds else "unsatisfied"
    return {
        "state": state,
        "source": "age",
        "reason": "head_settled" if state == "satisfied" else "head_too_new",
        "missing": [],
        "declared": [],
        "required_reason": reason,
        "head_age_seconds": age,
        "grace_seconds": grace_seconds,
    }


def apply_check_coverage(
    state: dict[str, Any], observation: dict[str, Any], required: Any
) -> dict[str, Any]:
    """Judge the rollup's coverage and fold the answer into the observation.

    ``checks_watch`` records when this head first came under observation. That
    timestamp is what the fallback grace and the declared deadline are both
    measured from, so a head that has only just arrived is never mistaken for
    one whose checks have finished registering.
    """

    head_sha = observation["head_sha"]
    checks = observation.setdefault("checks", {})
    names = {name for name in checks.get("names") or [] if name}
    now = utc_now()

    watch = state.get("checks_watch")
    if not isinstance(watch, dict) or watch.get("head_sha") != head_sha:
        watch = {"head_sha": head_sha, "first_seen_at": now}
    state["checks_watch"] = watch

    coverage = judge_check_coverage(
        names,
        required,
        head_age_seconds=elapsed_seconds(watch.get("first_seen_at"), now),
    )
    checks["coverage"] = coverage
    if checks.get("state") == "success" and coverage["state"] != "satisfied":
        checks["state"] = "pending"
    return coverage


def cached_required_contexts(
    state: dict[str, Any], target: dict[str, Any], base_branch: Any
) -> dict[str, Any]:
    """Read the declared contexts once per base branch and reuse the answer.

    Which checks a branch requires is configuration. It does not change while a
    pipeline runs, so it must not become a call per poll.
    """

    cached = state.get("required_contexts")
    if isinstance(cached, dict) and cached.get("base_branch") == base_branch:
        return cached
    answer = required_contexts(target, base_branch)
    answer = {**answer, "base_branch": base_branch, "read_at": utc_now()}
    state["required_contexts"] = answer
    return answer


def run_stage_status(
    entry: dict[str, Any], target: dict[str, Any]
) -> dict[str, Any]:
    """Run one stage helper's ``status`` and return its envelope.

    Every stage has a helper, whatever kind of evidence makes the stage green.
    Locating and running that helper is therefore separate from reading
    greenness: the conflict and check stages answer to GitHub for greenness and
    still have a helper that can say how their own run ended.
    """

    script = stage_script_path(entry)
    state_path = stage_state_path(entry["plugin"], target)
    result: dict[str, Any] = {
        "installed": script.is_file(),
        "script": str(script),
        "state": str(state_path),
        "payload": None,
    }
    if not result["installed"]:
        return {**result, "ok": False, "reason": "helper_missing"}
    if not state_path.is_file():
        return {**result, "ok": False, "reason": "no_state"}
    process = run(
        [sys.executable, str(script), "status", "--state", str(state_path)],
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "no output"
        return {**result, "ok": False, "reason": "status_failed", "detail": detail}
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        return {
            **result,
            "ok": False,
            "reason": "invalid_status_json",
            "detail": str(error),
        }
    return {**result, "ok": True, "payload": payload}


def read_stage_marker(entry: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """Read one stage's clean-at-head record from its own helper.

    The pipeline never reads a stage's prose report. A stage whose result is a
    judgment leaves the only durable record of that judgment in its own state
    file, so the helper that owns the file is the only thing that may interpret
    it.

    Every marker carries ``installed``, including the markers of the stages whose
    greenness GitHub already states. Those stages need no helper lookup, but they
    still need their plugin present before the pipeline may launch them.
    """

    script = stage_script_path(entry)
    installed = script.is_file()

    if entry["evidence"] != "helper":
        return {
            "source": "github",
            "available": True,
            "installed": installed,
            "script": str(script),
            "clean_at_head_sha": None,
        }

    status = run_stage_status(entry, target)
    if not status.get("ok") and status.get("reason") == "no_state":
        return {
            "source": "helper",
            "available": True,
            "installed": True,
            "reason": "no_state",
            "state": status["state"],
            "clean_at_head_sha": None,
        }
    if not status.get("ok"):
        return {
            "source": "helper",
            "available": False,
            "installed": status["installed"],
            "reason": status["reason"],
            "script": status["script"],
            "state": status["state"],
            "detail": status.get("detail"),
            "clean_at_head_sha": None,
        }
    payload = status["payload"]
    return {
        "source": "helper",
        "available": True,
        "installed": True,
        "state": status["state"],
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


def extract_stage_outcome(payload: Any) -> str | None:
    """Pull a stage's own name for how its run ended out of its status envelope.

    A stage that reports ``stage_outcome`` speaks the pipeline's vocabulary
    directly, which removes the last place a model's reading of prose decided
    anything. The field counts only on a ready status: an envelope that says a
    stage has no state cannot describe a run, and a stage that cleaned up after
    clearing must not be read as having done nothing.

    This says how a run ended. It never says whether a stage is green. Greenness
    stays where it was: live GitHub for the conflict and check stages, the
    clean-at-head marker for the other three.
    """

    if not isinstance(payload, dict) or payload.get("result") != "ready":
        return None
    outcome = payload.get("stage_outcome")
    if isinstance(outcome, str) and outcome.strip() in STAGE_OUTCOMES:
        return outcome.strip()
    return None


def read_stage_outcome(
    entry: dict[str, Any], target: dict[str, Any]
) -> dict[str, Any]:
    """Ask one stage's helper how its run ended.

    Only some stages report this. A stage that does not is not a failure: the
    caller falls back to reading the stage's report, which is what the pipeline
    did for every stage before any of them could answer mechanically.
    """

    status = run_stage_status(entry, target)
    common = {
        "stage": entry["stage"],
        "installed": status["installed"],
        "script": status["script"],
        "state": status["state"],
        "evidence": entry["evidence"],
        "clean_at_head_sha": (
            extract_clean_at_head_sha(entry["stage"], status["payload"])
            if status.get("ok")
            else None
        ),
    }
    if not status.get("ok"):
        return {
            **common,
            "available": False,
            "outcome": None,
            "reason": status["reason"],
            "detail": status.get("detail"),
        }
    outcome = extract_stage_outcome(status["payload"])
    if outcome is None:
        return {
            **common,
            "available": False,
            "outcome": None,
            "reason": "not_reported",
            "status_result": (
                status["payload"].get("result")
                if isinstance(status["payload"], dict)
                else None
            ),
        }
    return {**common, "available": True, "outcome": outcome, "source": "stage_status"}


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

    fresh = observation.get("reads") or {}
    stale_read = bool(fresh.get("head_moved_on_last_read"))

    if entry["stage"] == STAGE_CONFLICT:
        mergeability = observation.get("mergeability")
        if not isinstance(mergeability, dict):
            mergeability = {
                "state": "unsettled",
                "settled": False,
                "reason": "not_observed",
                "mergeable": observation.get("mergeable"),
            }
        green = mergeability.get("settled") and mergeability.get("state") == "mergeable"
        reason = mergeability.get("reason")
        if green and stale_read:
            green = False
            reason = "head_moved"
        return {
            "green": bool(green),
            "evidence": "github",
            "mergeable": mergeability.get("mergeable"),
            # Recorded for the history, and read by nothing. It is a second view
            # of the same computation ``mergeable`` comes from, so it goes stale
            # in step with it and can corroborate nothing.
            "merge_state_status": observation.get("merge_state_status"),
            "mergeability": mergeability.get("state"),
            "settled": bool(mergeability.get("settled")),
            "reason": None if green else reason,
            "recorded_at_head_sha": recorded,
        }

    if entry["stage"] == STAGE_CI:
        checks = observation.get("checks") or {}
        coverage = checks.get("coverage") or {}
        green = checks.get("state") == "success" and coverage.get("state") == "satisfied"
        reason = None if green else checks.get("state")
        if not green and coverage.get("state") != "satisfied":
            reason = coverage.get("reason") or "coverage_unsatisfied"
        if green and stale_read:
            green = False
            reason = "head_moved"
        return {
            "green": bool(green),
            "evidence": "github",
            "checks": checks.get("state"),
            "coverage": coverage.get("state"),
            "coverage_source": coverage.get("source"),
            "missing_contexts": coverage.get("missing") or [],
            "reason": reason,
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
    marker = markers.get(stage) or {}

    # Installation is checked for every stage, whatever its evidence kind. A
    # stage green from GitHub never gets this far, so the check costs nothing
    # when the plugin is absent but unneeded.
    if marker.get("installed") is False:
        return {
            "result": "escalate",
            "stage": stage,
            "reason": "helper_missing",
            "detail": (
                f"the {next_entry['plugin']} plugin is not installed, so the "
                f"pipeline cannot launch {next_entry['agent']}"
            ),
            "next_action": ESCALATION_ACTIONS["helper_missing"],
            "head_sha": head_sha,
            "recorded": False,
        }

    # A declared context that never registers is not a wait, it is a fault. The
    # pipeline works exclusively on drafts, so a repository that skips its
    # checks on a draft would otherwise wait for something that is not coming.
    if verdict.get("coverage") == "overdue":
        missing = verdict.get("missing_contexts") or []
        have = "they have" if len(missing) > 1 else "it has"
        return {
            "result": "escalate",
            "stage": stage,
            "reason": "checks_never_registered",
            "detail": (
                f"the base branch requires {', '.join(missing)} but {have} "
                f"not registered on {head_sha}"
            ),
            "next_action": ESCALATION_ACTIONS["checks_never_registered"],
            "missing_contexts": missing,
            "head_sha": head_sha,
            "recorded": False,
        }

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


def stage_default_model(entry: dict[str, Any]) -> str:
    model = entry.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return DEFAULT_STAGE_MODEL


def default_stage_models() -> dict[str, str]:
    return {entry["stage"]: stage_default_model(entry) for entry in STAGES}


def stage_models(state: dict[str, Any]) -> dict[str, str]:
    configured = state.get("stage_models")
    models = default_stage_models()
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
        "stage_models": default_stage_models(),
        "cleared": {},
        "no_progress": {},
        "running": None,
        "history": [],
        "escalation": None,
        "completed": None,
        "observed_head_sha": observation.get("head_sha"),
    }


def collect_observation(
    target: dict[str, Any],
    *,
    with_markers: bool = True,
    known_head_sha: str | None = None,
) -> dict[str, Any]:
    observation = observe_pull_request(target, known_head_sha=known_head_sha)
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

    existing = load_state(path) if path.is_file() else None
    observation = collect_observation(
        target,
        known_head_sha=(existing or {}).get("observed_head_sha"),
    )
    if observation.get("state") != "OPEN":
        raise WorkflowError(
            f"pull request {target['pr_url']} is {observation.get('state')}; "
            "the pipeline only drives an open pull request"
        )

    if existing is not None:
        state = existing
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
    state["observed_head_sha"] = observation["head_sha"]
    apply_check_coverage(
        state,
        observation,
        cached_required_contexts(state, target, observation["pr"].get("base_branch")),
    )

    for assignment in args.stage_model or []:
        stage, separator, model = assignment.partition("=")
        if not separator or stage not in STAGE_BY_NAME or not model.strip():
            raise WorkflowError(
                f"--stage-model expects <stage>=<model> for a known stage: {assignment}"
            )
        state.setdefault("stage_models", {})[stage] = model.strip()

    save_state(path, state)
    gate = gate_stage_models(stage_models(state), can_pin=not args.no_pin)
    # Reported rather than fatal. A stage that is green from GitHub never
    # launches, so a missing plugin only stops the run once that stage is the
    # one to run, and ``next`` escalates there.
    missing = [
        entry["stage"] for entry in STAGES if not stage_installed(entry)
    ]
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
            "missing_plugins": missing,
        }
    )


def command_next(args: argparse.Namespace) -> None:
    require_tools()
    path = cli_path(args.state)
    state = load_state(path)
    target = build_target(
        state["pr"]["owner"], state["pr"]["repo"], state["pr"]["number"]
    )
    observation = collect_observation(
        target, known_head_sha=state.get("observed_head_sha")
    )
    state["observed_head_sha"] = observation["head_sha"]
    apply_check_coverage(
        state,
        observation,
        cached_required_contexts(state, target, observation["pr"].get("base_branch")),
    )
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
                "mergeability": observation.get("mergeability"),
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
                "mergeability": observation.get("mergeability"),
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


def resolve_finish_outcome(
    entry: dict[str, Any],
    target: dict[str, Any],
    requested: str,
    *,
    head_sha: str | None = None,
) -> dict[str, Any]:
    """Settle how a stage ended, preferring the stage's own answer.

    Both answers are evidence, and neither is a view of the run itself. The
    stage's word is read from a state file. The caller's word is the agent's
    reading of its own run. The stage's is preferred because the stage contract
    guarantees it is a record of an ending a command wrote, rather than a word
    inferred from the shape of that state, so it cannot be misread out of prose.
    The caller's answer is kept in the history either way, which is what makes a
    disagreement visible instead of silent.

    That precedence has one limit, and it is the reason a head is passed in. A
    stage whose greenness is a judgment records that judgment in a state file
    that outlives the run which wrote it, and its ``status`` reports ``cleared``
    from the presence of that record. A run that dies before it replaces an
    older record therefore answers ``cleared`` about a commit it never looked
    at. The word alone is not evidence about this run: a clearance is accepted
    only when the stage's own head-pinned marker names the head being recorded.
    When it does not, the run reached no clearance, and the disagreement is kept
    rather than quietly rewritten, because a stage answering from a record it
    did not write is worth seeing afterwards.

    A stage whose truth lives on GitHub is untouched by this. Its clearance is
    never read from the pipeline's record, so a stale marker cannot speak for
    it.

    A pipeline problem that is not the stage's fault, such as a launch that never
    produced a run, belongs in ``escalate`` rather than here. ``finish`` says how
    the stage ended.
    """

    reading = read_stage_outcome(entry, target)
    if not reading.get("available"):
        return {
            "outcome": requested,
            "requested_outcome": requested,
            "outcome_source": "reported",
            "outcome_reason": reading.get("reason"),
            "clean_at_head_sha": reading.get("clean_at_head_sha"),
        }
    outcome = reading["outcome"]
    marker = sha_or_none(reading.get("clean_at_head_sha"))
    if entry["evidence"] == "helper" and outcome == "cleared" and marker != head_sha:
        return {
            "outcome": "no_progress",
            "requested_outcome": requested,
            "outcome_source": "stage_status",
            "outcome_reason": "clean_marker_head_mismatch",
            "stage_outcome": outcome,
            "clean_at_head_sha": marker,
        }
    return {
        "outcome": outcome,
        "requested_outcome": requested,
        "outcome_source": "stage_status",
        "outcome_reason": None,
        "clean_at_head_sha": marker,
    }


def command_outcome(args: argparse.Namespace) -> None:
    """Report how the stage that just ran ended, in the pipeline's vocabulary."""

    state = load_state(cli_path(args.state))
    if args.stage not in STAGE_BY_NAME:
        raise WorkflowError(f"unknown stage: {args.stage}")
    entry = STAGE_BY_NAME[args.stage]
    target = build_target(
        state["pr"]["owner"], state["pr"]["repo"], state["pr"]["number"]
    )
    reading = read_stage_outcome(entry, target)
    payload = {
        **reading,
        "stage": args.stage,
        "outcome": reading.get("outcome"),
    }
    if reading.get("available"):
        emit({**payload, "result": "ready", "authoritative": True})
        return
    emit(
        {
            **payload,
            "result": "not_reported",
            "authoritative": False,
            "reason": reading.get("reason", "not_reported"),
            "next_action": (
                "This stage does not report its own outcome, so work it out from "
                "the stage's report and pass it to finish."
            ),
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
    target = build_target(
        state["pr"]["owner"], state["pr"]["repo"], state["pr"]["number"]
    )
    resolution = resolve_finish_outcome(
        STAGE_BY_NAME[stage], target, args.outcome, head_sha=head_sha
    )
    outcome = resolution["outcome"]
    entry = {
        "stage": stage,
        "outcome": outcome,
        "requested_outcome": resolution["requested_outcome"],
        "outcome_source": resolution["outcome_source"],
        "outcome_reason": resolution.get("outcome_reason"),
        "stage_outcome": resolution.get("stage_outcome"),
        "clean_at_head_sha": resolution.get("clean_at_head_sha"),
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
    # A stage repeating an answer it already gave at this head has told the
    # pipeline nothing new. Relaunching a stage that has run out of its own road
    # returns the same result immediately every time, so a repeat must not read
    # as fresh evidence and must not reset the no-progress streak that is the
    # only brake on relaunching the same stage forever.
    repeat = any(
        isinstance(past, dict)
        and past.get("stage") == stage
        and past.get("head_sha") == head_sha
        and past.get("outcome") == outcome
        for past in state.get("history") or []
    )
    entry["repeat"] = repeat
    state.setdefault("history", []).append(entry)
    state["running"] = None

    streaks = state.setdefault("no_progress", {})
    stalled = outcome == "no_progress" or repeat
    if stalled:
        previous = streaks.get(stage)
        count = int(previous.get("count") or 0) + 1 if isinstance(previous, dict) else 1
        streaks[stage] = {"count": count, "head_sha": head_sha, "at": utc_now()}
    else:
        streaks.pop(stage, None)

    escalation = None
    if outcome in CLEARING_OUTCOMES and head_sha:
        state.setdefault("cleared", {})[stage] = head_sha
    if outcome == "escalated":
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
    elif stalled:
        count = int((streaks.get(stage) or {}).get("count") or 0)
        if count >= NO_PROGRESS_LIMIT:
            detail = f"{stage} ran {count} times in a row without changing anything"
            if repeat:
                detail = (
                    f"{stage} repeated its {outcome} answer at {head_sha} without "
                    "the pipeline being able to act on it"
                )
            escalation = record_escalation(
                state,
                {
                    "stage": stage,
                    "reason": "no_progress",
                    "detail": detail,
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
            "outcome": outcome,
            "requested_outcome": resolution["requested_outcome"],
            "outcome_source": resolution["outcome_source"],
            "outcome_reason": resolution.get("outcome_reason"),
            "entry": entry,
            "cleared": state.get("cleared") or {},
            "no_progress": state.get("no_progress") or {},
            "escalation": escalation,
            "keep_session": outcome != "cleared",
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
        models = default_stage_models()
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
    entry = STAGE_BY_NAME[args.stage]
    if not stage_installed(entry):
        emit(
            {
                "result": "not_installed",
                "stage": args.stage,
                "plugin": entry["plugin"],
                "agent": entry["agent"],
                "script": str(stage_script_path(entry)),
                "detail": (
                    f"the {entry['plugin']} plugin is not installed, so the "
                    f"pipeline cannot launch {entry['agent']}"
                ),
                "next_action": ESCALATION_ACTIONS["helper_missing"],
            }
        )
        return
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

    outcome = subparsers.add_parser(
        "outcome", help="ask a stage's own helper how its run ended"
    )
    outcome.add_argument("--state", required=True)
    outcome.add_argument("--stage", required=True, choices=list(STAGE_NAMES))
    outcome.set_defaults(function=command_outcome)

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
