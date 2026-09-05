#!/usr/bin/env python3
"""Deterministic mechanics for the CI Fix Loop custom agent."""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
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
import uuid


STATE_VERSION = 1
STACK_STATE_KIND = "native_stack"
STACK_ENTRIES_PAGE = 100
DEFAULT_MAX_ITERATIONS = 5
DEFAULT_PIPELINE_MAX_ITERATIONS = 2
DEFAULT_POLL_INTERVAL = 60
DEFAULT_POLL_TIMEOUT = 300
DEFAULT_NOT_STARTED_GRACE = 900
MAX_RERUNS_PER_CHECK = 1
PR_HEAD_LAG_RETRY_DELAY = 1
REMOTE_REF_LAG_RETRY_DELAYS = (1, 2, 4)
PROPAGATION_CONTAINMENT_RETRY_DELAYS = (1, 2, 4)
EMPTY_RERUN_COMMIT_MESSAGE = "ci: rerun checks"
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
RERUN_PERMISSION_PATTERNS = (
    re.compile(r"\bresource not accessible by integration\b", re.IGNORECASE),
    re.compile(r"\bpermission denied\b", re.IGNORECASE),
    re.compile(r"\binsufficient permissions?\b", re.IGNORECASE),
    re.compile(
        r"\bmust have\b.*\b(?:access|permission|rights?)\b", re.IGNORECASE
    ),
    re.compile(
        r"\b(?:write|push|admin)\s+(?:access|permission|rights?)\s+(?:is|are)\s+required\b",
        re.IGNORECASE,
    ),
)

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
STACK_CLEAR_OUTCOMES = {"cleared", "skipped"}

STACK_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!, $first: Int!) {"
    "  repository(owner: $owner, name: $name) {"
    "    pullRequest(number: $number) {"
    "      stack {"
    "        id number size baseRefName"
    "        entries(first: $first) {"
    "          nodes {"
    "            position"
    "            pullRequest {"
    "              number title headRefName baseRefName headRefOid isDraft state"
    "            }"
    "          }"
    "        }"
    "      }"
    "    }"
    "  }"
    "}"
)

# A path this matches holds tests. The loop refuses to make a check pass by
# stopping one of them from running, so it needs to recognize one by name.
TEST_PATH_MARKERS = (
    "/test/",
    "/tests/",
    "/spec/",
    "/specs/",
    "/testing/",
    "/__tests__/",
    "/testdata/",
)
TEST_FILE_PATTERNS = (
    "test_*.py",
    "*_test.py",
    "*_test.go",
    "*_test.rb",
    "*_spec.rb",
    "*_test.cc",
    "*_test.cpp",
    "*_unittest.cc",
    "*test.java",
    "*tests.java",
    "*testcase.java",
    "*test.kt",
    "*tests.kt",
    "*test.cs",
    "*tests.cs",
    "*test.scala",
    "*test.groovy",
    "*spec.groovy",
    "*.test.js",
    "*.test.jsx",
    "*.test.ts",
    "*.test.tsx",
    "*.spec.js",
    "*.spec.jsx",
    "*.spec.ts",
    "*.spec.tsx",
)

# Adding one of these to a test that was running turns a failure green by not
# running it. Each pattern matches only the annotation, never a mention of it in
# prose, so a line that explains a skip does not read as one.
SUPPRESSION_PATTERNS = (
    (re.compile(r"@pytest\.mark\.(?:skip|skipif|xfail)\b"), "@pytest.mark.skip"),
    (
        re.compile(r"@unittest\.(?:skip|skipIf|skipUnless|expectedFailure)\b"),
        "@unittest.skip",
    ),
    (re.compile(r"\bpytest\.skip\s*\("), "pytest.skip()"),
    (re.compile(r"\bself\.skipTest\s*\("), "self.skipTest()"),
    (re.compile(r"@Disabled\b"), "@Disabled"),
    (re.compile(r"@Ignore\b"), "@Ignore"),
    (
        re.compile(r"@Test\s*\([^)]*enabled\s*=\s*false", re.IGNORECASE),
        "@Test(enabled = false)",
    ),
    (re.compile(r"\bx(?:it|describe|test|context)\s*\("), "xit()"),
    (re.compile(r"\b(?:it|describe|test|context|suite)\.skip\s*\("), ".skip()"),
    (re.compile(r"\b(?:it|describe|test)\.todo\s*\("), ".todo()"),
    (re.compile(r"\bt\.Skip(?:Now|f)?\s*\("), "t.Skip()"),
    (re.compile(r"#\[ignore\b"), "#[ignore]"),
    (re.compile(r"\[Ignore\b"), "[Ignore]"),
    (re.compile(r"\bSkip\s*=\s*[\"']"), 'Skip = "..."'),
)
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


class RerunPermissionDenied(WorkflowError):
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


def base_ref_tip(repo_name: str, base_branch: str) -> str:
    """Return the live tip commit of a pull request's base branch.

    GitHub's ``baseRefOid`` freezes at the moment the pull request was created
    or last synced and does not follow the base branch as it moves, so reading
    it names a commit the base branch has since left behind. The base commit is
    the baseline the check attribution compares against, so a stale one blames
    the pull request for a failure the newer base introduced and excuses one the
    newer base fixed. The branch ref always names the current tip, so this reads
    that instead.

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


def default_stack_state_path(
    target: dict[str, Any], stack_number: int, run_id: str
) -> Path:
    name = (
        f"{target['owner']}--{target['repo']}--stack-{stack_number}--{run_id}.json"
    )
    return Path.home() / ".copilot" / "run" / "ci-fix-loop" / "stacks" / name


def stack_member_state_path(stack_state_path: Path, member_number: int) -> Path:
    return stack_state_path.with_name(
        f"{stack_state_path.stem}--pr-{member_number}.json"
    )


def stack_propagation_state_path(
    stack_state_path: Path, fixed_number: int, expected_head: str
) -> Path:
    return stack_state_path.with_name(
        f"{stack_state_path.stem}--propagate-pr-{fixed_number}-{expected_head}.json"
    )


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
        raise WorkflowError(
            "cannot resolve the current pull request from detached HEAD: pass "
            "the pull request explicitly as a URL or owner/repo#number"
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
        "number,title,url,state,isDraft,headRefName,headRefOid,headRepositoryOwner,"
        "headRepository,baseRefName,commits"
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
    base_branch = metadata.get("baseRefName")
    if not isinstance(base_branch, str) or not base_branch:
        raise WorkflowError("resolved PR metadata has no base branch")
    base_sha = base_ref_tip(upstream_repo_name, base_branch)
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
        "base_branch": base_branch,
        "base_sha": base_sha,
        "is_fork": head_repo_name.lower() != upstream_repo_name.lower(),
        "is_draft": bool(metadata.get("isDraft")),
        "commits": commits,
    }


def parse_native_stack(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise WorkflowError("the native stack is not an object")
    trunk = raw.get("baseRefName")
    if not isinstance(trunk, str) or not trunk:
        raise WorkflowError("the native stack has no trunk branch")
    entries = raw.get("entries")
    nodes = entries.get("nodes") if isinstance(entries, dict) else None
    if not isinstance(nodes, list):
        raise WorkflowError("the native stack has no readable member list")
    members: list[dict[str, Any]] = []
    for node in nodes:
        member = node.get("pullRequest") if isinstance(node, dict) else None
        if not isinstance(member, dict):
            raise WorkflowError("the native stack has an unreadable member")
        number = member.get("number")
        title = member.get("title")
        head_branch = member.get("headRefName")
        base_branch = member.get("baseRefName")
        head_sha = member.get("headRefOid")
        if (
            not isinstance(number, int)
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(head_branch, str)
            or not head_branch
            or not isinstance(base_branch, str)
            or not base_branch
            or not isinstance(head_sha, str)
            or not head_sha
        ):
            raise WorkflowError(
                f"native stack member {number!r} is missing a required field"
            )
        members.append(
            {
                "position": node.get("position"),
                "number": number,
                "title": title.strip(),
                "head_branch": head_branch,
                "base_branch": base_branch,
                "head_sha": head_sha,
                "is_draft": bool(member.get("isDraft")),
                "state": member.get("state"),
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
    number = raw.get("number")
    if not isinstance(number, int):
        raise WorkflowError("the native stack has no number")
    return {
        "id": raw.get("id"),
        "number": number,
        "size": size,
        "trunk": trunk,
        "members": members,
    }


def read_native_stack(target: dict[str, Any]) -> dict[str, Any] | None:
    payload = graphql(
        STACK_QUERY,
        {
            "owner": target["owner"],
            "name": target["repo"],
            "number": target["number"],
            "first": STACK_ENTRIES_PAGE,
        },
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    repository = data.get("repository") if isinstance(data, dict) else None
    if not isinstance(repository, dict):
        raise WorkflowError("the stack query returned no repository")
    pull = repository.get("pullRequest")
    if not isinstance(pull, dict):
        raise WorkflowError("the stack query returned no pull request")
    return parse_native_stack(pull.get("stack"))


def stack_topology_fingerprint(stack: dict[str, Any]) -> str:
    material = json.dumps(
        {
            "id": stack.get("id"),
            "number": stack.get("number"),
            "trunk": stack.get("trunk"),
            "members": [
                [member["number"], member["head_branch"], member["base_branch"]]
                for member in stack["members"]
            ],
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def commit_contains(repository: str, ancestor: str, descendant: str) -> bool:
    payload = gh_json(
        ["api", f"repos/{repository}/compare/{ancestor}...{descendant}"]
    )
    return isinstance(payload, dict) and payload.get("status") in {
        "ahead",
        "identical",
    }


def copilot_home() -> Path:
    value = os.environ.get("COPILOT_HOME", "").strip()
    return cli_path(value) if value else Path.home() / ".copilot"


def conflict_resolver_script() -> Path:
    return (
        copilot_home()
        / "installed-plugins"
        / "trask-plugins"
        / "pr-conflict-resolver"
        / "scripts"
        / "pr_conflict_resolver.py"
    )


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


def is_aggregate_check(check: dict[str, Any]) -> bool:
    """Recognize checks whose result summarizes other status checks."""
    name = re.sub(r"[^a-z0-9]+", " ", str(check.get("name") or "").lower()).strip()
    description = re.sub(
        r"[^a-z0-9]+", " ", str(check.get("description") or "").lower()
    ).strip()
    return (
        name in {"required status check", "required status checks", "all checks"}
        or name.startswith("required status checks ")
        or "aggregate status check" in name
        or "required status checks" in description
    )


def failure_sets(
    checks: list[dict[str, Any]], failed: Iterable[str]
) -> tuple[list[str], list[str]]:
    by_key = {check["key"]: check for check in checks}
    concrete = sorted(key for key in failed if not is_aggregate_check(by_key[key]))
    aggregate = sorted(key for key in failed if is_aggregate_check(by_key[key]))
    return (concrete or aggregate), (aggregate if concrete else [])


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
    pending = sorted(grouped["running"] + grouped["not_started"])
    not_started = grouped["not_started"]
    overdue = sorted(
        key
        for key in not_started
        if not_started_seconds(tracking, key, now) >= not_started_grace
    )
    failed, aggregate = failure_sets(checks, grouped["failed"])

    if failed:
        pending_detail = (
            f"; {len(pending)} other check(s) are still pending" if pending else ""
        )
        aggregate_detail = (
            f"; ignored aggregate failures backed by those jobs: "
            f"{describe_checks(checks, aggregate)}"
            if aggregate
            else ""
        )
        return {
            "decision": "failures",
            "reason": "checks_failed",
            "checks": failed,
            "pending_checks": pending,
            "overdue_checks": overdue,
            "aggregate_checks": aggregate,
            "detail": (
                f"these concrete checks failed: {describe_checks(checks, failed)}"
                f"{pending_detail}{aggregate_detail}"
            ),
        }

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

    if pending:
        if deadline_expired:
            return {
                "decision": "waiting",
                "reason": "still_running",
                "checks": pending,
                "pending_checks": pending,
                "detail": (
                    "these checks are still running after this polling slice: "
                    f"{describe_checks(checks, pending)}"
                ),
            }
        return {
            "decision": "waiting",
            "reason": "checks_running",
            "checks": pending,
            "pending_checks": pending,
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


def rerun_evidence_is_stale(
    check: dict[str, Any], entry: Any, head_sha: str
) -> bool:
    """Say whether a failure was already on record when its re-run was requested.

    A re-run does not change the rollup until GitHub re-queues the job, so the
    failure sitting there just after the request is the old one. Crediting it
    would report a flake as having failed twice on the strength of a single run.
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("head_sha") != head_sha:
        return False
    requested_at = entry.get("requested_at")
    if not requested_at:
        return False
    completed_at = check.get("completed_at")
    if not completed_at:
        # Without a completion time there is nothing to prove the failure is
        # newer than the request, so wait rather than credit it.
        return True
    return parse_timestamp(completed_at) <= parse_timestamp(requested_at)


def apply_rerun_watermark(
    checks: list[dict[str, Any]], reruns: Any, head_sha: str
) -> list[dict[str, Any]]:
    """Hold back a failure this loop has already asked GitHub to run again."""
    if not isinstance(reruns, dict) or not reruns:
        return checks
    applied = []
    for check in checks:
        entry = reruns.get(check["key"])
        if check["class"] == "failed" and rerun_evidence_is_stale(
            check, entry, head_sha
        ):
            check = {**check, "class": "running", "awaiting_rerun": True}
        applied.append(check)
    return applied


def baseline_conclusions(pr: dict[str, Any], base_sha: str) -> dict[str, str]:
    """Read how the same checks concluded on the base branch commit.

    This is the evidence that stops the loop from editing the pull request to
    paper over a breakage the base branch already has.

    The result answers how a named check behaved on the base commit, and it
    answers nothing about which checks the head ought to run. The base commit
    and the pull request head are reached by different triggers, so a `push`
    workflow leaves a name here that never runs on the head, and a
    `pull_request` workflow runs on the head with no counterpart here. Neither
    name set contains the other. Read only the names present on both sides, and
    treat a name missing from this result as an absence of evidence rather than
    as a check that has yet to register.
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
    pending = list(decision.get("pending_checks") or [])
    already_handled = handled_checks(state)

    fixable = sorted(
        key
        for key in failing
        if (attributions.get(key) or {}).get("verdict") == "pr_caused"
        and key not in already_handled
    )
    if fixable:
        return {
            "action": "fix",
            "reason": "pr_caused_failures",
            "checks": fixable,
            "pending_checks": pending,
            "detail": "this pull request plausibly caused these failures",
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
            "pending_checks": pending,
            "detail": "re-run each suspected flake exactly once",
        }

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
            "pending_checks": pending,
            "detail": (
                "the base branch evidence does not settle these failures, so each one "
                "needs a verdict before this loop may touch it"
            ),
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
            "pending_checks": pending,
            "detail": (
                "these checks failed again after their one automatic re-run, so they "
                "are not flakes"
            ),
        }

    pre_existing = sorted(
        key for key in failing if attributions[key]["verdict"] == "pre_existing"
    )
    if pre_existing:
        overdue = list(decision.get("overdue_checks") or [])
        if overdue:
            return {
                "action": "escalate",
                "reason": "checks_never_started",
                "checks": overdue,
                "pending_checks": pending,
                "ignored_checks": pre_existing,
                "detail": (
                    "the known failures are pre-existing, but these checks did not "
                    f"start within the grace period: {', '.join(overdue)}"
                ),
            }
        if pending:
            return {
                "action": "waiting",
                "reason": "checks_running",
                "checks": pending,
                "pending_checks": pending,
                "ignored_checks": pre_existing,
                "detail": (
                    "the known failures are pre-existing; waiting for the remaining "
                    f"{len(pending)} check(s)"
                ),
            }
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


def is_test_path(path: Any) -> bool:
    """Say whether a repository path holds tests, by name alone."""
    if not isinstance(path, str) or not path:
        return False
    normalized = path.replace("\\", "/").lower().lstrip("/")
    if any(marker in f"/{normalized}" for marker in TEST_PATH_MARKERS):
        return True
    name = normalized.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, pattern) for pattern in TEST_FILE_PATTERNS)


def suppression_markers(line: Any) -> list[str]:
    """Name every way one line stops a test from running."""
    if not isinstance(line, str):
        return []
    return [label for pattern, label in SUPPRESSION_PATTERNS if pattern.search(line)]


def commit_suppressions(repo_root: Path, commit: str) -> list[dict[str, Any]]:
    """Find every way one commit makes a test stop running.

    This reads the commit rather than the worktree, so it sees what would reach
    the pull request. It reports only the unambiguous forms: a deleted test file,
    and a skip or disable annotation added to a test file. Judging whether a
    surviving test still asserts what it used to is deliberately not attempted.
    """
    findings: list[dict[str, Any]] = []
    for line in git(
        repo_root, "show", "--format=", "--name-status", "--no-renames", commit
    ).splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        if fields[0].startswith("D") and is_test_path(fields[-1]):
            findings.append(
                {"kind": "deleted_test_file", "path": fields[-1], "marker": None}
            )
    current: str | None = None
    for line in git(
        repo_root, "show", "--format=", "--unified=0", "--no-renames", commit
    ).splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current = target[2:] if target.startswith("b/") else target
            continue
        if line.startswith("---") or line.startswith("+++"):
            continue
        if not line.startswith("+") or current in (None, "/dev/null"):
            continue
        if not is_test_path(current):
            continue
        for marker in suppression_markers(line[1:]):
            findings.append(
                {
                    "kind": "added_suppression",
                    "path": current,
                    "marker": marker,
                    "line": line[1:].strip(),
                }
            )
    return findings


def refuse_test_suppression(repo_root: Path, commits: Iterable[str]) -> None:
    """Refuse a commit that turns a check green by stopping a test from running.

    Making the checks pass never legitimately includes removing a feature's
    coverage, so this stage treats deleting a test file, or disabling a test that
    was running, as something it cannot do rather than something it should weigh.
    A refusal in code leaves no room for a rationale to talk its way past it.
    """
    findings: list[dict[str, Any]] = []
    for commit in commits:
        for finding in commit_suppressions(repo_root, commit):
            findings.append({"commit": commit, **finding})
    if not findings:
        return
    raise WorkflowError(
        "this commit makes a check pass by stopping a test from running, which "
        "this loop never does: "
        f"{json.dumps(findings, sort_keys=True)}. Fix what the test caught, or "
        "record the batch with --rationale and escalate it as unfixable_failure."
    )


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


def invocation_scope(
    state: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any] | None:
    """Scope a standalone budget to one explicit user invocation."""
    if getattr(args, "new_invocation", False):
        spent = int(state.get("iterations", 0))
        return {
            "run": uuid.uuid4().hex,
            "iteration": None,
            "baseline": spent,
            "run_baseline": spent,
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
        "iteration": None,
        "baseline": whole_number(recorded.get("baseline"), spent),
        "run_baseline": whole_number(recorded.get("run_baseline"), spent),
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

    Scoped counters keep overlapping pipeline and standalone runs independent.
    A state written before those counters existed falls back to its durable-count
    baselines. Without a scope, both values remain the lifetime count.
    """
    spent = int(state.get("iterations", 0))
    if scope is None:
        return spent, spent
    charge_key = scope.get("_charge_key")
    run_charge_key = scope.get("_run_charge_key")
    charges = state.get("budget_charges")
    if (
        isinstance(charge_key, str)
        and isinstance(run_charge_key, str)
        and isinstance(charges, dict)
    ):
        return (
            whole_number(charges.get(charge_key), 0),
            whole_number(charges.get(run_charge_key), 0),
        )
    return (
        max(0, spent - whole_number(scope.get("baseline"), spent)),
        max(0, spent - whole_number(scope.get("run_baseline"), spent)),
    )


def budget_charge_keys(kind: str, scope: dict[str, Any]) -> tuple[str, str]:
    run = scope["run"]
    iteration = scope.get("iteration")
    return (
        json.dumps([kind, run, iteration], separators=(",", ":")),
        json.dumps([kind, run], separators=(",", ":")),
    )


def stored_budget_scope(state: dict[str, Any]) -> str:
    kind = state.get("budget_scope")
    if kind in {"pipeline", "invocation", "lifetime"}:
        return kind
    if isinstance(state.get("pipeline_budget"), dict):
        return "pipeline"
    if isinstance(state.get("invocation_budget"), dict):
        return "invocation"
    return "lifetime"


def migrate_budget_counters(state: dict[str, Any]) -> None:
    """Materialize counters and charged-head records from baseline-based state."""
    spent = int(state.get("iterations", 0))
    charges = state.setdefault("budget_charges", {})
    for kind, field in (
        ("pipeline", "pipeline_budget"),
        ("invocation", "invocation_budget"),
    ):
        scope = state.get(field)
        if not isinstance(scope, dict) or not isinstance(scope.get("run"), str):
            continue
        charge_key, run_charge_key = budget_charge_keys(kind, scope)
        charges.setdefault(
            charge_key,
            max(0, spent - whole_number(scope.get("baseline"), spent)),
        )
        charges.setdefault(
            run_charge_key,
            max(0, spent - whole_number(scope.get("run_baseline"), spent)),
        )

    charged_head = state.get("charged_head_sha")
    if charged_head:
        entry = {
            "head_sha": charged_head,
            "iteration": (state.get("run") or {}).get("iteration"),
        }
        charged_heads = state.setdefault("budget_charged_heads", {})
        charged_heads.setdefault("lifetime", entry)
        for kind, field in (
            ("pipeline", "pipeline_budget"),
            ("invocation", "invocation_budget"),
        ):
            scope = state.get(field)
            if isinstance(scope, dict) and isinstance(scope.get("run"), str):
                charge_key, _ = budget_charge_keys(kind, scope)
                charged_heads.setdefault(charge_key, entry)
        state.pop("charged_head_sha", None)


def scoped_budget(
    state: dict[str, Any],
    kind: str,
    scope: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Attach persistent charge counters to one active budget."""
    if scope is None:
        return None
    previous_iteration_spent, previous_run_spent = budget_spent(state, scope)
    charge_key, run_charge_key = budget_charge_keys(kind, scope)
    charges = state.setdefault("budget_charges", {})
    if charge_key not in charges:
        charges[charge_key] = previous_iteration_spent
    if run_charge_key not in charges:
        charges[run_charge_key] = previous_run_spent
    return {
        **scope,
        "_charge_key": charge_key,
        "_run_charge_key": run_charge_key,
    }


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


def budget_advanced(recorded: Any, scope: dict[str, Any] | None) -> bool:
    """Whether this scope is a different outer position from the recorded one.

    A new run, or a later iteration of the same run, both move the budget on.
    Anything that leaves the budget where it was, including no outer loop at all,
    reads as no advance.
    """
    if scope is None:
        return False
    previous = recorded if isinstance(recorded, dict) else {}
    if previous.get("run") != scope.get("run"):
        return True
    seen = pipeline_iteration_value(previous.get("iteration"))
    current = pipeline_iteration_value(scope.get("iteration"))
    return current is not None and (seen is None or current > seen)


def charge_iteration(state: dict[str, Any], run_state: dict[str, Any]) -> bool:
    """Spend an iteration on the current run, once, when it has real work to do.

    A launch that reads the checks and finds nothing to fix costs nothing. Only a
    run that reaches attribution, a re-run, or a fix spends one, so relaunching the
    loop at a head whose checks already passed can never exhaust the cap.

    The budget bounds fix attempts, and a fix attempt is exactly what moves the
    head, so an unchanged head is charged once however many times the loop is
    relaunched on it. Re-deriving the same analysis, or re-running a flaky job and
    reading the checks again, therefore costs nothing beyond the attempt that
    reached that head. A run carrying no head is charged, because a dedupe with no
    head to key on would be a guess.

    The lifetime count is durable and only ever rises. Scoped counters enforce
    each active budget without letting another run's work consume it.
    """
    if run_state.get("charged"):
        return False
    head_sha = run_state.get("head_sha")
    charge_key = run_state.get("budget_charge_key")
    head_key = run_state.get("budget_head_key")
    if isinstance(head_key, str):
        charged_heads = state.setdefault("budget_charged_heads", {})
        charged = charged_heads.get(head_key)
        charged_head = charged.get("head_sha") if isinstance(charged, dict) else charged
        if head_sha and charged_head == head_sha:
            return False
    elif head_sha and state.get("charged_head_sha") == head_sha:
        return False
    run_state["charged"] = True
    state["iterations"] = int(state.get("iterations", 0)) + 1
    if isinstance(charge_key, str):
        charges = state.setdefault("budget_charges", {})
        charges[charge_key] = whole_number(charges.get(charge_key), 0) + 1
        run_charge_key = run_state.get("budget_run_charge_key")
        if isinstance(run_charge_key, str) and run_charge_key != charge_key:
            charges[run_charge_key] = whole_number(charges.get(run_charge_key), 0) + 1
    if isinstance(head_key, str) and head_sha:
        charged_heads[head_key] = {
            "head_sha": head_sha,
            "iteration": run_state.get("iteration"),
        }
    elif not isinstance(charge_key, str) and head_sha:
        state["charged_head_sha"] = head_sha
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
    process = run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{pr['upstream_owner']}/{pr['upstream_repo']}/actions/runs/"
            f"{run_id}/rerun-failed-jobs",
        ],
        check=False,
    )
    if process.returncode == 0:
        return
    detail = process.stderr.strip() or process.stdout.strip() or "no output"
    if any(pattern.search(detail) for pattern in RERUN_PERMISSION_PATTERNS):
        raise RerunPermissionDenied(detail)
    raise WorkflowError(f"GitHub could not re-run workflow {run_id}: {detail}")


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
    state_origin = "reused" if state is not None else "fresh"

    dirty = git(repo_root, "status", "--porcelain=v1")
    if dirty:
        raise WorkflowError(f"worktree is not clean:\n{dirty}")

    metadata = metadata_for(target)
    stack_guard = (
        verify_stack_member_guard(
            cli_path(args.stack_state), target, metadata["head_sha"]
        )
        if getattr(args, "stack_state", None)
        else None
    )
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
    state["iterations"] = int(state.get("iterations", 0))
    migrate_budget_counters(state)
    archive_run(state)
    previous_run = state.get("run") or {}
    previous_head = previous_run.get("head_sha")
    if previous_head and previous_head != metadata["head_sha"]:
        # A new head invalidates every re-run this loop spent on the old one.
        state["reruns"] = {}
    max_iterations = getattr(args, "max_iterations", DEFAULT_MAX_ITERATIONS)
    pipeline = pipeline_scope(state, args)
    invocation = invocation_scope(state, args)
    if pipeline is not None and invocation is not None:
        raise WorkflowError(
            "standalone invocation arguments cannot be combined with pipeline arguments"
        )
    scope = pipeline or invocation
    budget_scope = (
        "pipeline"
        if pipeline is not None
        else "invocation"
        if invocation is not None
        else "lifetime"
    )
    recorded_scope = (
        state.get("pipeline_budget")
        if budget_scope == "pipeline"
        else state.get("invocation_budget")
        if budget_scope == "invocation"
        else None
    )
    budget_just_advanced = budget_advanced(recorded_scope, scope)
    if budget_scope == "pipeline":
        state["pipeline_budget"] = scope
    elif budget_scope == "invocation":
        state["invocation_budget"] = scope
    state["budget_scope"] = budget_scope
    scope = scoped_budget(state, budget_scope, scope)
    absolute_cap = absolute_iteration_cap(
        pipeline, max_iterations, getattr(args, "pipeline_max_iterations", None)
    )
    completed_iterations, run_spent = budget_spent(state, scope)
    # Numbered from the durable count rather than from the budget, because this id
    # is what `archive_run` dedupes history on and a duplicate is dropped rather
    # than recorded. Any budget that rewrote that count instead of taking a
    # baseline against it would restart the numbering and lose an entry.
    #
    # An unchanged head that was already charged keeps its number too, for the
    # same reason. It is the same attempt read a second time, so it re-derives the
    # verdicts already archived under that id and they are correctly dropped;
    # advancing the number would let the label outrun the budget and make a third
    # read collide with the second's entries instead.
    budget_head_key = scope["_charge_key"] if scope is not None else "lifetime"
    charged = (state.get("budget_charged_heads") or {}).get(
        budget_head_key,
        state.get("charged_head_sha") if scope is None else None,
    )
    charged_head = charged.get("head_sha") if isinstance(charged, dict) else charged
    already_charged = bool(charged_head and charged_head == metadata["head_sha"])
    charged_iteration = (
        whole_number(charged.get("iteration"), state["iterations"])
        if isinstance(charged, dict)
        else state["iterations"]
    )
    iteration = charged_iteration if already_charged else state["iterations"] + 1
    exhausted = exhausted_budget(state, scope, max_iterations, absolute_cap)
    blocked_budget = exhausted if not already_charged else None
    result = "max_iterations_reached" if blocked_budget else "ready"
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
                "stack_guard": stack_guard,
                "charged": False,
                "budget_scope": budget_scope,
                "budget_head_key": budget_head_key,
                "budget_charge_key": None if scope is None else scope["_charge_key"],
                "budget_run_charge_key": (
                    None if scope is None else scope["_run_charge_key"]
                ),
            },
        }
    )
    # A relaunch reads the checks again from GitHub, which is the only thing that
    # states whether they pass and the only thing that may retract it. Drop the
    # outcome the previous run recorded so nothing reports a stale clearance.
    state["outcome"] = None
    state["clean_at_head_sha"] = None
    if result == "max_iterations_reached":
        detail = (
            f"this pull request already spent {run_spent} iteration(s) "
            f"in this pipeline run, which is its ceiling of {absolute_cap}"
            if exhausted == "absolute"
            else (
                f"this {budget_scope} budget already spent {completed_iterations} "
                f"iteration(s), which is its cap of {max_iterations}"
            )
        )
        state["escalation"] = {
            "reason": "max_iterations_reached",
            "detail": detail,
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
        "completed_iterations": completed_iterations,
        "absolute_cap": absolute_cap,
        "budget_exhausted": blocked_budget,
        "budget_origin": "fresh" if budget_just_advanced else "reused",
        "budget_scope": budget_scope,
        "state_origin": state_origin,
        "invocation_run": None if invocation is None else invocation["run"],
        "pipeline_run": None if pipeline is None else pipeline["run"],
        "pipeline_iteration": None if pipeline is None else pipeline["iteration"],
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
            "completed_iterations": completed_iterations,
            "budget_origin": "fresh" if budget_just_advanced else "reused",
            "budget_scope": budget_scope,
            "state_origin": state_origin,
            "invocation_run": None if invocation is None else invocation["run"],
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

    checks = apply_rerun_watermark(checks, state.get("reruns"), pinned)
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
        actionable = set(decision["checks"])
        attributions = attribute_failures(
            [check for check in checks if check["key"] in actionable],
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


def record_terminal_outcome(
    state: dict[str, Any], run_state: dict[str, Any], outcome: str
) -> str | None:
    if outcome not in ("green", "no_checks"):
        raise WorkflowError(f"cannot record nonterminal checks outcome {outcome!r}")
    pinned = run_state["head_sha"]
    run_state["outcome"] = outcome
    run_state["clean_at_head_sha"] = pinned
    state["clean_at_head_sha"] = pinned
    state["outcome"] = outcome
    state["escalation"] = None
    note = (
        f"CI Fix Loop skipped {state['pr']['repo_name']}#{state['pr']['number']}: "
        "the pull request head reports no applicable checks, so this repository ran "
        "no CI on it."
        if outcome == "no_checks"
        else None
    )
    state["skip_note"] = note
    return note


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
        if snapshot["action"]["action"] != "waiting" or expired:
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
        "action_checks": action["checks"],
        "pending_checks": action.get("pending_checks")
        or decision.get("pending_checks")
        or [],
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
    if action["action"] in ("green", "no_checks"):
        record_terminal_outcome(state, run_state, action["action"])
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
            "pending_checks": action.get("pending_checks")
            or decision.get("pending_checks")
            or [],
            "aggregate_checks": decision.get("aggregate_checks") or [],
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
    completed = finalized_stack_push(state, "rerun", check_key=args.check)
    if completed is not None:
        pushed, checkpoint = completed
        resume = pushed.get("resume") or {}
        emit(
            {
                "result": "empty_commit_published",
                "state": str(path),
                "check": args.check,
                "name": resume.get("name"),
                "run_id": resume.get("run_id"),
                "head_sha": pushed["head_sha"],
                "reruns": 1,
                "max_reruns": MAX_RERUNS_PER_CHECK,
                "accepted_push": checkpoint,
                "recovered": True,
            }
        )
        return
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
    reruns = state.get("reruns")
    previous = reruns.get(args.check) if isinstance(reruns, dict) else None
    if (
        isinstance(previous, dict)
        and previous.get("method") == "empty_commit"
        and previous.get("head_sha") == run_state["head_sha"]
        and previous.get("status") != "published"
    ):
        publish_empty_rerun_commit(
            path,
            state,
            args.check,
            entry,
            run_id=int(previous.get("run_id") or 0),
            permission_detail=str(previous.get("permission_detail") or ""),
        )
        return
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
    # Stamp the watermark before asking GitHub to re-run, so a run that starts
    # quickly cannot finish inside the gap and be mistaken for the old result.
    requested_at = utc_now()
    try:
        rerun_failed_jobs(state["pr"], run_id)
    except RerunPermissionDenied as error:
        publish_empty_rerun_commit(
            path,
            state,
            args.check,
            entry,
            run_id=run_id,
            permission_detail=str(error),
        )
        return
    reruns = state.setdefault("reruns", {})
    reruns[args.check] = {
        "count": already + 1,
        "name": entry["name"],
        "run_id": run_id,
        "head_sha": run_state["head_sha"],
        "requested_at": requested_at,
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
    require_stack_guard(state)
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
        repo_root = Path(state["repo_root"])
        commit = git(repo_root, "rev-parse", commit)
        refuse_test_suppression(repo_root, [commit])
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
    checks = apply_rerun_watermark(checks, state.get("reruns"), pinned)
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
    note = record_terminal_outcome(state, run_state, args.outcome)
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


def accepted_push_checkpoint(
    state: dict[str, Any],
    *,
    previous_head: str,
    head_sha: str,
    commits: list[str],
    kind: str = "fix",
    checkpoint_id: str | None = None,
) -> dict[str, Any]:
    pipeline_budget = (
        state.get("pipeline_budget")
        if stored_budget_scope(state) == "pipeline"
        else None
    ) or {}
    checkpoint = {
        "id": checkpoint_id or uuid.uuid4().hex,
        "accepted_at": utc_now(),
        "previous_head_sha": previous_head,
        "head_sha": head_sha,
        "commits": commits,
        "kind": kind,
        "pipeline_run": pipeline_budget.get("run"),
        "pipeline_iteration": pipeline_budget.get("iteration"),
    }
    state.setdefault("accepted_pushes", []).append(checkpoint)
    return checkpoint


def prepare_pending_stack_push(
    path: Path,
    state: dict[str, Any],
    *,
    previous_head: str,
    head_sha: str,
    commits: list[str],
    kind: str,
    validation: dict[str, Any] | None = None,
    resume: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    guard = (active_run(state).get("stack_guard") or {})
    if not guard:
        return None
    pending = state.get("pending_stack_push")
    expected = {
        "previous_head_sha": previous_head,
        "head_sha": head_sha,
        "commits": commits,
        "kind": kind,
        "pipeline_run": guard.get("run_id"),
        "member": guard.get("member"),
    }
    if isinstance(pending, dict):
        if any(pending.get(key) != value for key, value in expected.items()):
            raise WorkflowError(
                "another native stack push intent is already pending in this PR state"
            )
        return pending
    pending = {
        "id": uuid.uuid4().hex,
        "prepared_at": utc_now(),
        **expected,
        "validation": validation,
        "resume": resume,
    }
    state["pending_stack_push"] = pending
    save_state(path, state)
    return pending


def finalize_pending_stack_push(
    path: Path, state: dict[str, Any], pending: dict[str, Any]
) -> dict[str, Any]:
    checkpoint = next(
        (
            entry
            for entry in state.get("accepted_pushes") or []
            if entry.get("id") == pending["id"]
        ),
        None,
    )
    if checkpoint is None:
        checkpoint = accepted_push_checkpoint(
            state,
            previous_head=pending["previous_head_sha"],
            head_sha=pending["head_sha"],
            commits=list(pending["commits"]),
            kind=pending["kind"],
            checkpoint_id=pending["id"],
        )
    validation = pending.get("validation")
    if isinstance(validation, dict) and not any(
        entry.get("pending_push_id") == pending["id"]
        for entry in state.get("local_validation") or []
    ):
        state.setdefault("local_validation", []).append(
            {**validation, "pending_push_id": pending["id"]}
        )
    run_state = active_run(state)
    run_state["status"] = "published"
    run_state["published_head_sha"] = pending["head_sha"]
    state["reruns"] = {}
    state["clean_at_head_sha"] = None
    archive_run(state)
    state["last_stack_push"] = {
        **pending,
        "checkpoint_id": checkpoint["id"],
        "completed_at": utc_now(),
    }
    state.pop("pending_stack_push", None)
    save_state(path, state)
    return checkpoint


def finalized_stack_push(
    state: dict[str, Any], command: str, *, check_key: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    run_state = state.get("run") or {}
    completed = state.get("last_stack_push")
    guard = run_state.get("stack_guard") or {}
    if (
        run_state.get("status") != "published"
        or not isinstance(completed, dict)
        or (completed.get("resume") or {}).get("command") != command
        or completed.get("head_sha") != run_state.get("published_head_sha")
        or completed.get("pipeline_run") != guard.get("run_id")
        or completed.get("member") != guard.get("member")
    ):
        return None
    if check_key is not None and (completed.get("resume") or {}).get("check") != check_key:
        return None
    checkpoint = next(
        (
            entry
            for entry in state.get("accepted_pushes") or []
            if entry.get("id") == completed.get("checkpoint_id")
        ),
        None,
    )
    if checkpoint is None:
        return None
    return completed, checkpoint


def recover_landed_pending_stack_push(
    path: Path, state: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    pending = state.get("pending_stack_push")
    if not isinstance(pending, dict):
        return None
    run_state = active_run(state)
    guard = run_state.get("stack_guard") or {}
    if (
        pending.get("pipeline_run") != guard.get("run_id")
        or pending.get("member") != guard.get("member")
        or pending.get("previous_head_sha") != run_state.get("head_sha")
    ):
        raise WorkflowError("the pending native stack push does not match this run")
    live_head = metadata_for(parse_target(state["pr"]["pr_url"]))["head_sha"]
    if live_head != pending.get("head_sha"):
        return None
    checkpoint = finalize_pending_stack_push(path, state, pending)
    return pending, checkpoint


def require_empty_child(repo_root: Path, commit_sha: str, pinned: str) -> None:
    parents = git(repo_root, "rev-list", "--parents", "-n", "1", commit_sha).split()
    same_tree = git(repo_root, "rev-parse", f"{commit_sha}^{{tree}}") == git(
        repo_root, "rev-parse", f"{pinned}^{{tree}}"
    )
    if parents != [commit_sha, pinned] or not same_tree:
        raise WorkflowError(
            "the empty-commit fallback did not create exactly one tree-identical child "
            "of the pinned head; refusing to push"
        )


def publish_empty_rerun_commit(
    path: Path,
    state: dict[str, Any],
    check_key: str,
    attribution: dict[str, Any],
    *,
    run_id: int,
    permission_detail: str,
) -> None:
    """Publish one empty commit when GitHub explicitly denies a workflow re-run."""
    run_state = active_run(state)
    recovered = recover_landed_pending_stack_push(path, state)
    if recovered is not None:
        pending, checkpoint = recovered
        if pending.get("kind") != "ci_rerun":
            raise WorkflowError("the recovered native stack push is not a CI re-run")
        emit(
            {
                "result": "empty_commit_published",
                "state": str(path),
                "check": check_key,
                "name": attribution["name"],
                "run_id": run_id,
                "head_sha": pending["head_sha"],
                "reruns": 1,
                "max_reruns": MAX_RERUNS_PER_CHECK,
                "accepted_push": checkpoint,
                "recovered": True,
            }
        )
        return
    require_stack_guard(state)
    reruns = state.setdefault("reruns", {})
    fallback = reruns.get(check_key)
    if fallback is not None and (
        not isinstance(fallback, dict)
        or fallback.get("method") != "empty_commit"
        or fallback.get("head_sha") != run_state["head_sha"]
        or fallback.get("status") == "published"
    ):
        raise WorkflowError(f"{attribution['name']} already used its one retry")

    repo_root = Path(state["repo_root"])
    dirty = git(repo_root, "status", "--porcelain=v1")
    if dirty:
        raise WorkflowError(
            "GitHub denied the workflow re-run, but the empty-commit fallback is "
            f"unsafe because the worktree is not clean:\n{dirty}"
        )

    pr = state["pr"]
    pinned = run_state["head_sha"]
    local_head = git(repo_root, "rev-parse", "HEAD")
    branch = pr.get("head_branch")
    if (
        not isinstance(branch, str)
        or not branch
        or branch == pr.get("base_branch")
    ):
        raise WorkflowError(
            "GitHub denied the workflow re-run, but the PR head branch is not safely "
            "writable"
        )
    local_branch = git(repo_root, "branch", "--show-current")
    if local_branch and local_branch != branch:
        raise WorkflowError(
            "GitHub denied the workflow re-run, but the empty-commit fallback is "
            f"unsafe from local branch {local_branch!r}; expected {branch!r} or detached"
        )
    refreshed = metadata_for(parse_target(pr["pr_url"]))
    pr_head = refreshed["head_sha"]
    if fallback is None and pr_head != pinned:
        raise WorkflowError(
            "GitHub denied the workflow re-run, but the empty-commit fallback is "
            f"unsafe because the PR head moved from {pinned} to {pr_head}"
        )
    require_fork_head(pr)
    remote = find_push_remote(repo_root, pr["head_owner"], pr["head_repo"])
    remote_current = remote_head(pr["head_owner"], pr["head_repo"], branch)

    if fallback is None:
        if local_head != pinned:
            raise WorkflowError(
                "GitHub denied the workflow re-run, but the empty-commit fallback is "
                f"unsafe because local HEAD is {local_head}, not pinned head {pinned}"
            )
        if pr_head != pinned or remote_current != pinned:
            raise WorkflowError(
                "GitHub denied the workflow re-run, but the empty-commit fallback is "
                "unsafe because the PR or remote head moved"
            )
        fallback = {
            "count": 1,
            "name": attribution["name"],
            "run_id": run_id,
            "head_sha": pinned,
            "requested_at": utc_now(),
            "method": "empty_commit",
            "status": "creating",
            "permission_detail": permission_detail,
        }
        reruns[check_key] = fallback
        save_state(path, state)

    commit_sha = fallback.get("commit_sha")
    if not isinstance(commit_sha, str) or not commit_sha:
        if local_head == pinned:
            if pr_head != pinned or remote_current != pinned:
                raise WorkflowError(
                    "the PR head moved before the empty commit was created"
                )
            run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "commit",
                    "--allow-empty",
                    "--no-verify",
                    "-m",
                    EMPTY_RERUN_COMMIT_MESSAGE,
                ]
            )
            commit_sha = git(repo_root, "rev-parse", "HEAD")
        else:
            commit_sha = local_head
    require_empty_child(repo_root, commit_sha, pinned)
    if fallback.get("commit_sha") != commit_sha or fallback.get("status") == "creating":
        fallback["commit_sha"] = commit_sha
        fallback["status"] = "prepared"
        save_state(path, state)

    pending = prepare_pending_stack_push(
        path,
        state,
        previous_head=pinned,
        head_sha=commit_sha,
        commits=[commit_sha],
        kind="ci_rerun",
        resume={
            "command": "rerun",
            "check": check_key,
            "name": attribution["name"],
            "run_id": run_id,
        },
    )
    if remote_current == pinned:
        if pr_head != pinned:
            raise WorkflowError(
                "the PR and remote heads disagree; refusing the empty-commit fallback"
            )
        if git(repo_root, "rev-parse", "HEAD") != commit_sha:
            raise WorkflowError(
                "the prepared empty commit is no longer checked out; refusing to push"
            )
        run(["git", "-C", str(repo_root), "push", remote, f"HEAD:{branch}"])
        fallback["status"] = "pushed"
        save_state(path, state)
    elif remote_current != commit_sha:
        raise WorkflowError(
            "the remote head moved to an unexpected commit; refusing the empty-commit "
            "fallback"
        )

    pushed_head = wait_for_remote_head(
        pr["head_owner"], pr["head_repo"], branch, commit_sha
    )
    if pushed_head != commit_sha:
        raise WorkflowError(
            f"empty-commit fallback head mismatch: expected {commit_sha}, remote "
            f"{pushed_head}"
        )
    pr_head = metadata_for(parse_target(pr["pr_url"]))["head_sha"]
    if pr_head != commit_sha:
        time.sleep(PR_HEAD_LAG_RETRY_DELAY)
        pr_head = metadata_for(parse_target(pr["pr_url"]))["head_sha"]
    if pr_head != commit_sha:
        raise WorkflowError(
            f"empty-commit fallback PR head mismatch: expected {commit_sha}, PR head "
            f"{pr_head}"
        )

    fallback["status"] = "published"
    fallback["published_head_sha"] = commit_sha
    if pending is not None:
        accepted_push = finalize_pending_stack_push(path, state, pending)
    else:
        run_state["status"] = "published"
        run_state["published_head_sha"] = commit_sha
        accepted_push = next(
            (
                checkpoint
                for checkpoint in state.get("accepted_pushes") or []
                if checkpoint.get("kind") == "ci_rerun"
                and checkpoint.get("previous_head_sha") == pinned
                and checkpoint.get("head_sha") == commit_sha
            ),
            None,
        )
        if accepted_push is None:
            accepted_push = accepted_push_checkpoint(
                state,
                previous_head=pinned,
                head_sha=commit_sha,
                commits=[commit_sha],
                kind="ci_rerun",
            )
        state["clean_at_head_sha"] = None
        archive_run(state)
        save_state(path, state)
    emit(
        {
            "result": "empty_commit_published",
            "state": str(path),
            "check": check_key,
            "name": attribution["name"],
            "run_id": run_id,
            "head_sha": commit_sha,
            "reruns": 1,
            "max_reruns": MAX_RERUNS_PER_CHECK,
            "accepted_push": accepted_push,
        }
    )


def local_validation_entry(args: argparse.Namespace, head_sha: str) -> dict[str, Any]:
    """Describe the local validation behind one publication.

    Three answers are distinct and a reader needs all three. `passed` names the
    commands that ran and passed, `skipped` carries the reason none ran, and
    `unreported` says the publication claimed nothing either way. `rewrote` names
    the subset that changed files, because a fixing command's rewrites have to
    reach the commits being pushed and a record that only says "ran clean"
    cannot show whether they did.

    Nothing here refuses a push. A repository that offers no covering command
    must still publish, and a malformed claim is folded into a coherent record
    rather than raised: naming a command as rewriting implies it ran, so it
    counts as validated too. The record exists so someone can read what the loop
    did instead of inferring it from the checks that fail afterwards.
    """
    entry: dict[str, Any] = {"head_sha": head_sha}
    rewrote = [command.strip() for command in (args.rewrote or []) if command.strip()]
    commands = [
        command.strip() for command in (args.validated or []) if command.strip()
    ]
    for command in rewrote:
        if command not in commands:
            commands.append(command)
    if commands:
        entry["status"] = "passed"
        entry["commands"] = commands
        entry["rewrote"] = rewrote
        return entry
    reason = (args.not_validated or "").strip()
    if reason:
        entry["status"] = "skipped"
        entry["reason"] = reason
        return entry
    entry["status"] = "unreported"
    return entry


def command_publish(args: argparse.Namespace) -> None:
    require_tools()
    path = cli_path(args.state)
    state = load_state(path)
    completed = finalized_stack_push(state, "publish")
    if completed is not None:
        pushed, checkpoint = completed
        emit(
            {
                "result": "published",
                "state": str(path),
                "head_sha": pushed["head_sha"],
                "commits": pushed["commits"],
                "accepted_push": checkpoint,
                "iterations": state["iterations"],
                "local_validation": pushed.get("validation"),
                "recovered": True,
            }
        )
        return
    recovered = recover_landed_pending_stack_push(path, state)
    if recovered is not None:
        pending, checkpoint = recovered
        emit(
            {
                "result": "published",
                "state": str(path),
                "head_sha": pending["head_sha"],
                "commits": pending["commits"],
                "accepted_push": checkpoint,
                "iterations": state["iterations"],
                "local_validation": pending.get("validation"),
                "recovered": True,
            }
        )
        return
    require_stack_guard(state)
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

    # The last gate before anything reaches the pull request. Every commit here
    # passed `record`, but an amend after that would not have, so check them all.
    refuse_test_suppression(repo_root, new_commits)

    pr = state["pr"]
    require_fork_head(pr)
    remote = find_push_remote(repo_root, pr["head_owner"], pr["head_repo"])
    remote_before = remote_head(pr["head_owner"], pr["head_repo"], pr["head_branch"])
    if remote_before not in {pinned, local_head}:
        raise WorkflowError(
            f"head ref moved from {pinned} to {remote_before}; refusing to push"
        )
    validation = local_validation_entry(args, local_head)
    pending = prepare_pending_stack_push(
        path,
        state,
        previous_head=pinned,
        head_sha=local_head,
        commits=commits,
        kind="fix",
        validation=validation,
        resume={"command": "publish"},
    )
    if remote_before != local_head:
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

    if pending is not None:
        accepted_push = finalize_pending_stack_push(path, state, pending)
    else:
        run_state["status"] = "published"
        run_state["published_head_sha"] = local_head
        accepted_push = accepted_push_checkpoint(
            state,
            previous_head=pinned,
            head_sha=local_head,
            commits=commits,
        )
        state.setdefault("local_validation", []).append(validation)
        # The published head is new, so nothing this loop learned about the old
        # head's checks still applies.
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
            "accepted_push": accepted_push,
            "iterations": state["iterations"],
            "local_validation": validation,
        }
    )


def load_stack_state(path: Path) -> dict[str, Any]:
    state = load_state(path)
    if state.get("kind") != STACK_STATE_KIND:
        raise WorkflowError(f"state file is not a native stack run: {path}")
    return state


def stack_target(state: dict[str, Any]) -> dict[str, Any]:
    target = state.get("target")
    if not isinstance(target, dict):
        raise WorkflowError("native stack state has no target")
    return parse_target(str(target.get("pr_url") or ""))


def stack_stop(
    path: Path,
    state: dict[str, Any],
    reason: str,
    detail: str,
    *,
    member: int | None = None,
) -> None:
    state["status"] = "stopped"
    state["outcome"] = None
    state["reason"] = reason
    state["detail"] = detail
    if member is not None:
        state["blocked_member"] = member
    save_state(path, state)
    emit(
        {
            "result": "stopped",
            "state": str(path),
            "reason": reason,
            "detail": detail,
            "blocked_member": member,
        }
    )


def refresh_stack_state(state: dict[str, Any]) -> dict[str, Any]:
    stack = read_native_stack(stack_target(state))
    if stack is None:
        raise WorkflowError("the selected pull request is no longer in a native stack")
    if stack["number"] != state.get("stack_number"):
        raise WorkflowError(
            f"the selected pull request moved from native stack "
            f"{state.get('stack_number')} to {stack['number']}"
        )
    fingerprint = stack_topology_fingerprint(stack)
    if fingerprint != state.get("topology_fingerprint"):
        raise WorkflowError("the native stack topology changed during the CI run")
    live_by_number = {member["number"]: member for member in stack["members"]}
    recorded_numbers = [member["number"] for member in state.get("members") or []]
    if recorded_numbers != [member["number"] for member in stack["members"]]:
        raise WorkflowError("the native stack member order changed during the CI run")
    for recorded in state["members"]:
        live = live_by_number[recorded["number"]]
        recorded.update(
            {
                "title": live["title"],
                "head_branch": live["head_branch"],
                "base_branch": live["base_branch"],
                "head_sha": live["head_sha"],
                "is_draft": live["is_draft"],
                "state": live["state"],
            }
        )
        if live["state"] != "OPEN":
            raise WorkflowError(
                f"native stack member #{live['number']} is no longer open"
            )
    return stack


def stale_cleared_member(state: dict[str, Any]) -> dict[str, Any] | None:
    for member in state.get("members") or []:
        if (
            member.get("ci_status") == "clear"
            and member.get("clean_at_head_sha") != member.get("head_sha")
        ):
            return member
    return None


def verify_stack_member_guard(
    path: Path, target: dict[str, Any], head_sha: str
) -> dict[str, Any]:
    state = load_stack_state(path)
    if state.get("status") != "active":
        raise WorkflowError(
            f"native stack run {state.get('run_id')} is {state.get('status')}, not active"
        )
    refresh_stack_state(state)
    stale = stale_cleared_member(state)
    if stale is not None:
        raise WorkflowError(
            f"native stack member #{stale['number']} moved from "
            f"{stale.get('clean_at_head_sha')} to {stale.get('head_sha')} after "
            "its CI result was recorded"
        )
    cursor = int(state.get("cursor", 0))
    members = state.get("members") or []
    if cursor >= len(members):
        raise WorkflowError("native stack run has no current member")
    member = members[cursor]
    if member["number"] != target["number"]:
        raise WorkflowError(
            f"native stack run expects pull request #{member['number']}, not "
            f"#{target['number']}"
        )
    if member["head_sha"] != head_sha:
        raise WorkflowError(
            f"native stack member #{member['number']} moved from {head_sha} to "
            f"{member['head_sha']}"
        )
    predecessor = members[cursor - 1] if cursor else None
    if predecessor is not None:
        if (
            predecessor.get("ci_status") != "clear"
            or predecessor.get("clean_at_head_sha") != predecessor.get("head_sha")
        ):
            raise WorkflowError(
                f"native stack predecessor #{predecessor['number']} is not clear at "
                "its current head"
            )
        if not commit_contains(
            state["repository"], predecessor["head_sha"], member["head_sha"]
        ):
            raise WorkflowError(
                f"native stack member #{member['number']} does not contain clear "
                f"predecessor #{predecessor['number']} at {predecessor['head_sha']}"
            )
    return {
        "state": str(path),
        "run_id": state["run_id"],
        "stack_number": state["stack_number"],
        "member": member["number"],
        "member_head_sha": member["head_sha"],
        "predecessor": None if predecessor is None else predecessor["number"],
        "predecessor_head_sha": (
            None if predecessor is None else predecessor["head_sha"]
        ),
    }


def require_stack_guard(state: dict[str, Any]) -> None:
    run_state = active_run(state)
    guard = run_state.get("stack_guard")
    if not isinstance(guard, dict):
        return
    path_value = guard.get("state")
    if not isinstance(path_value, str) or not path_value:
        raise WorkflowError("native stack guard has no coordinator state path")
    refreshed = verify_stack_member_guard(
        cli_path(path_value),
        parse_target(state["pr"]["pr_url"]),
        run_state["head_sha"],
    )
    if refreshed["run_id"] != guard.get("run_id"):
        raise WorkflowError(
            "a different native stack run now owns this member; refusing to edit or push"
        )


def stop_for_stale_cleared_member(
    path: Path, state: dict[str, Any]
) -> bool:
    member = stale_cleared_member(state)
    if member is None:
        return False
    stack_stop(
        path,
        state,
        "cleared_member_head_changed",
        f"pull request #{member['number']} moved from "
        f"{member.get('clean_at_head_sha')} to {member.get('head_sha')} after "
        "its CI result was recorded",
        member=member["number"],
    )
    return True


def command_stack_start(args: argparse.Namespace) -> None:
    require_tools()
    repo_root = resolve_repo_root(args.repo_root)
    target = resolve_target(args.target, repo_root)
    if args.pipeline_run:
        emit(
            {
                "result": "single",
                "target": target["pr_url"],
                "reason": "orchestrated_invocation",
                "pr": {
                    "number": target["number"],
                    "pr_url": target["pr_url"],
                    "repo_name": target["repo_name"],
                },
            }
        )
        return
    stack = read_native_stack(target)
    if stack is None:
        emit(
            {
                "result": "single",
                "target": target["pr_url"],
                "pr": {
                    "number": target["number"],
                    "pr_url": target["pr_url"],
                    "repo_name": target["repo_name"],
                },
            }
        )
        return
    selected = next(
        (member for member in stack["members"] if member["number"] == target["number"]),
        None,
    )
    if selected is None:
        raise WorkflowError(
            f"pull request #{target['number']} is not present in its native stack"
        )
    not_open = [
        member["number"] for member in stack["members"] if member["state"] != "OPEN"
    ]
    if not_open:
        raise WorkflowError(
            f"native stack members are no longer open: "
            f"{', '.join(f'#{number}' for number in not_open)}"
        )
    resolver = conflict_resolver_script()
    if not resolver.is_file():
        raise WorkflowError(
            "native stack CI requires PR Conflict Resolver before any repair starts; "
            "install pr-conflict-resolver@trask-plugins"
        )
    run_id = uuid.uuid4().hex
    path = (
        cli_path(args.state)
        if args.state
        else default_stack_state_path(target, stack["number"], run_id)
    )
    if path.exists():
        raise WorkflowError(f"native stack state already exists: {path}")
    state = {
        "version": STATE_VERSION,
        "kind": STACK_STATE_KIND,
        "created_at": utc_now(),
        "status": "active",
        "run_id": run_id,
        "repo_root": str(repo_root),
        "repository": target["repo_name"],
        "target": {
            "number": selected["number"],
            "title": selected["title"],
            "pr_url": target["pr_url"],
        },
        "stack_number": stack["number"],
        "stack_id": stack.get("id"),
        "trunk": stack["trunk"],
        "topology_fingerprint": stack_topology_fingerprint(stack),
        "cursor": 0,
        "members": [
            {
                **member,
                "ci_status": "pending",
                "stage_outcome": None,
                "clean_at_head_sha": None,
                "dispatched_head_sha": None,
                "iterations": 0,
                "accepted_pushes": [],
            }
            for member in stack["members"]
        ],
        "propagations": [],
        "propagation_attempts": [],
        "propagated_pushes": [],
        "superseded_pushes": [],
        "reason": None,
        "detail": None,
    }
    save_state(path, state)
    emit(
        {
            "result": "stack",
            "state": str(path),
            "run_id": run_id,
            "repository": target["repo_name"],
            "stack_number": stack["number"],
            "selected_pr": selected["number"],
            "selected_title": selected["title"],
            "members": [member["number"] for member in stack["members"]],
        }
    )


def command_stack_next(args: argparse.Namespace) -> None:
    require_tools()
    path = cli_path(args.state)
    state = load_stack_state(path)
    if state.get("status") != "active":
        emit(
            {
                "result": state.get("status"),
                "state": str(path),
                "reason": state.get("reason"),
                "detail": state.get("detail"),
                "blocked_member": state.get("blocked_member"),
            }
        )
        return
    try:
        refresh_stack_state(state)
    except WorkflowError as error:
        stack_stop(path, state, "topology_changed", str(error))
        return
    if stop_for_stale_cleared_member(path, state):
        return
    cursor = int(state.get("cursor", 0))
    members = state["members"]
    if cursor >= len(members):
        skipped = [
            member["number"]
            for member in members
            if member.get("stage_outcome") == "skipped"
        ]
        state["status"] = "complete"
        state["outcome"] = "skipped" if skipped else "green"
        state["reason"] = "members_without_checks" if skipped else "all_members_green"
        state["detail"] = (
            "these native stack members report no applicable checks: "
            + ", ".join(f"#{number}" for number in skipped)
            if skipped
            else (
                f"all {len(members)} native stack members are green at their "
                "current heads"
            )
        )
        save_state(path, state)
        emit(
            {
                "result": "complete",
                "state": str(path),
                "stack_number": state["stack_number"],
                "members": [member["number"] for member in members],
                "outcome": state["outcome"],
                "skipped_members": skipped,
                "propagations": len(state.get("propagations") or []),
            }
        )
        return

    member = members[cursor]
    member_target = parse_target(f"{state['repository']}#{member['number']}")
    member_state_path = stack_member_state_path(path, member["number"])
    if member.get("ci_status") == "active":
        if member_state_path.is_file():
            member_state = load_state(member_state_path)
            pending = member_state.get("pending_stack_push")
            member_guard = ((member_state.get("run") or {}).get("stack_guard") or {})
            member_owned_by_run = (
                member_guard.get("run_id") == state["run_id"]
                and member_guard.get("member") == member["number"]
            )
            member_guard_matches_head = (
                member_owned_by_run
                and member_guard.get("member_head_sha") == member["head_sha"]
            )
            pending_matches_run = (
                isinstance(pending, dict)
                and pending.get("pipeline_run") == state["run_id"]
                and pending.get("member") == member["number"]
                and member_owned_by_run
            )
            if pending_matches_run and pending.get("head_sha") == member["head_sha"]:
                finalize_pending_stack_push(
                    member_state_path, member_state, pending
                )
            elif (
                pending_matches_run
                and pending.get("previous_head_sha") == member["head_sha"]
            ):
                resume = pending.get("resume") or {}
                command = resume.get("command")
                if command not in {"publish", "rerun"}:
                    stack_stop(
                        path,
                        state,
                        "pending_push_unrecoverable",
                        f"pull request #{member['number']} has a pending push with "
                        "no supported resume command",
                        member=member["number"],
                    )
                    return
                emit(
                    {
                        "result": f"resume_{command}",
                        "state": str(path),
                        "member": member["number"],
                        "member_state": str(member_state_path),
                        "check": resume.get("check"),
                        "validation": pending.get("validation"),
                        "reason": "prepared_push_not_published",
                    }
                )
                return
            if member_guard_matches_head and not isinstance(pending, dict):
                unfinished_reruns = [
                    (check_key, rerun)
                    for check_key, rerun in (member_state.get("reruns") or {}).items()
                    if isinstance(rerun, dict)
                    and rerun.get("method") == "empty_commit"
                    and rerun.get("head_sha") == member["head_sha"]
                    and rerun.get("status") in {"creating", "prepared", "pushed"}
                ]
                if len(unfinished_reruns) > 1:
                    stack_stop(
                        path,
                        state,
                        "pending_push_unrecoverable",
                        f"pull request #{member['number']} has multiple unfinished "
                        "empty-commit retries",
                        member=member["number"],
                    )
                    return
                if unfinished_reruns:
                    check_key, rerun = unfinished_reruns[0]
                    emit(
                        {
                            "result": "resume_rerun",
                            "state": str(path),
                            "member": member["number"],
                            "member_state": str(member_state_path),
                            "check": check_key,
                            "validation": None,
                            "reason": f"empty_commit_{rerun['status']}",
                        }
                    )
                    return
            propagated = set(state.get("propagated_pushes") or [])
            pending_pushes = [
                checkpoint
                for checkpoint in member_state.get("accepted_pushes") or []
                if checkpoint.get("pipeline_run") == state["run_id"]
                and checkpoint.get("id")
                and checkpoint["id"] not in propagated
            ]
            current_pushes = [
                checkpoint
                for checkpoint in pending_pushes
                if checkpoint.get("head_sha") == member["head_sha"]
            ]
            superseded = [
                checkpoint["id"]
                for checkpoint in pending_pushes
                if checkpoint.get("head_sha") != member["head_sha"]
            ]
            if superseded:
                state.setdefault("superseded_pushes", []).extend(superseded)
                state["superseded_pushes"] = sorted(
                    set(state["superseded_pushes"])
                )
                state.setdefault("propagated_pushes", []).extend(superseded)
                state["propagated_pushes"] = sorted(
                    set(state["propagated_pushes"])
                )
            if current_pushes:
                checkpoint = current_pushes[-1]
                save_state(path, state)
                emit(
                    {
                        "result": "propagate",
                        "state": str(path),
                        "stack_number": state["stack_number"],
                        "fixed_pr": member["number"],
                        "expected_head": member["head_sha"],
                        "checkpoint_id": checkpoint["id"],
                        "reason": "accepted_push_not_propagated",
                    }
                )
                return
        dispatched_head = member.get("dispatched_head_sha")
        if dispatched_head and dispatched_head != member["head_sha"]:
            stack_stop(
                path,
                state,
                "active_member_head_changed",
                f"pull request #{member['number']} moved from {dispatched_head} to "
                f"{member['head_sha']} while its CI repair was active",
                member=member["number"],
            )
            return
    if cursor:
        predecessor = members[cursor - 1]
        if predecessor.get("ci_status") != "clear":
            stack_stop(
                path,
                state,
                "predecessor_not_clear",
                f"pull request #{predecessor['number']} is not clear at its current head",
                member=member["number"],
            )
            return
        if predecessor.get("clean_at_head_sha") != predecessor["head_sha"]:
            stack_stop(
                path,
                state,
                "predecessor_head_changed",
                f"pull request #{predecessor['number']} moved after it was cleared",
                member=member["number"],
            )
            return
        if not commit_contains(
            state["repository"], predecessor["head_sha"], member["head_sha"]
        ):
            attempt = f"{predecessor['number']}:{predecessor['head_sha']}"
            if attempt in set(state.get("propagation_attempts") or []):
                stack_stop(
                    path,
                    state,
                    "propagation_did_not_contain",
                    f"propagating pull request #{predecessor['number']} at "
                    f"{predecessor['head_sha']} did not make pull request "
                    f"#{member['number']} contain that head",
                    member=member["number"],
                )
                return
            save_state(path, state)
            emit(
                {
                    "result": "propagate",
                    "state": str(path),
                    "stack_number": state["stack_number"],
                    "fixed_pr": predecessor["number"],
                    "expected_head": predecessor["head_sha"],
                    "next_member": member["number"],
                    "reason": "predecessor_head_is_not_contained",
                }
            )
            return

    member["ci_status"] = "active"
    member["dispatched_head_sha"] = member["head_sha"]
    save_state(path, state)
    emit(
        {
            "result": "run_member",
            "state": str(path),
            "run_id": state["run_id"],
            "stack_number": state["stack_number"],
            "member": member["number"],
            "title": member["title"],
            "target": member_target["pr_url"],
            "head_sha": member["head_sha"],
            "base_branch": member["base_branch"],
            "member_state": str(member_state_path),
            "stack_state": str(path),
            "pipeline_run": state["run_id"],
            "pipeline_iteration": 1,
            "pipeline_max_iterations": 1,
        }
    )


def command_stack_record(args: argparse.Namespace) -> None:
    require_tools()
    path = cli_path(args.state)
    state = load_stack_state(path)
    if state.get("status") != "active":
        raise WorkflowError("cannot record a member on a finished native stack run")
    try:
        refresh_stack_state(state)
    except WorkflowError as error:
        stack_stop(path, state, "topology_changed", str(error))
        return
    if stop_for_stale_cleared_member(path, state):
        return
    cursor = int(state.get("cursor", 0))
    members = state["members"]
    if cursor >= len(members):
        raise WorkflowError("the native stack run has no current member")
    member = members[cursor]
    member_state_path = cli_path(args.member_state)
    member_state = load_state(member_state_path)
    pr = member_state.get("pr") or {}
    if (
        pr.get("number") != member["number"]
        or str(pr.get("repo_name") or "").casefold()
        != str(state["repository"]).casefold()
    ):
        raise WorkflowError(
            f"{member_state_path} does not belong to "
            f"{state['repository']}#{member['number']}"
        )
    member_run = member_state.get("run") or {}
    guard = member_run.get("stack_guard") or {}
    if (
        member_state.get("budget_scope") != "pipeline"
        or member_run.get("budget_scope") != "pipeline"
        or guard.get("run_id") != state["run_id"]
        or guard.get("member") != member["number"]
        or guard.get("member_head_sha") != member["head_sha"]
        or cli_path(str(guard.get("state") or "")) != path
    ):
        raise WorkflowError(
            f"{member_state_path} was not produced by native stack run "
            f"{state['run_id']}"
        )
    outcome = stage_outcome(member_state)
    clean_head = member_state.get("clean_at_head_sha")
    if outcome not in STACK_CLEAR_OUTCOMES or clean_head != member["head_sha"]:
        escalation = member_state.get("escalation") or {}
        detail = (
            escalation.get("detail")
            or (
                f"CI Fix Loop ended as {outcome!r} at {clean_head!r}, not clear at "
                f"the live head {member['head_sha']}"
            )
        )
        stack_stop(
            path,
            state,
            "member_not_clear",
            str(detail),
            member=member["number"],
        )
        return
    accepted = [
        checkpoint
        for checkpoint in member_state.get("accepted_pushes") or []
        if checkpoint.get("pipeline_run") == state["run_id"]
    ]
    member.update(
        {
            "ci_status": "clear",
            "stage_outcome": outcome,
            "clean_at_head_sha": clean_head,
            "iterations": int(member_state.get("iterations", 0)),
            "accepted_pushes": accepted,
            "skip_note": member_state.get("skip_note"),
        }
    )
    state["cursor"] = cursor + 1
    save_state(path, state)
    emit(
        {
            "result": "recorded",
            "state": str(path),
            "member": member["number"],
            "stage_outcome": outcome,
            "head_sha": clean_head,
            "accepted_pushes": accepted,
            "remaining": len(members) - state["cursor"],
        }
    )


def command_stack_propagate(args: argparse.Namespace) -> None:
    require_tools()
    path = cli_path(args.state)
    state = load_stack_state(path)
    if state.get("status") != "active":
        raise WorkflowError("cannot propagate a finished native stack run")
    try:
        refresh_stack_state(state)
    except WorkflowError as error:
        stack_stop(path, state, "topology_changed", str(error))
        return
    if stop_for_stale_cleared_member(path, state):
        return
    fixed = next(
        (
            member
            for member in state["members"]
            if member["number"] == args.fixed_pr
        ),
        None,
    )
    if fixed is None:
        raise WorkflowError(
            f"pull request #{args.fixed_pr} is not in native stack "
            f"{state['stack_number']}"
        )
    if fixed["head_sha"] != args.expected_head:
        stack_stop(
            path,
            state,
            "source_head_changed",
            f"pull request #{args.fixed_pr} is at {fixed['head_sha']}, not "
            f"{args.expected_head}",
            member=args.fixed_pr,
        )
        return
    attempt = f"{args.fixed_pr}:{args.expected_head}"
    script = conflict_resolver_script()
    if not script.is_file():
        stack_stop(
            path,
            state,
            "propagation_unavailable",
            f"PR Conflict Resolver is not installed at {script}",
            member=args.fixed_pr,
        )
        return
    resolver_state_path = stack_propagation_state_path(
        path, args.fixed_pr, args.expected_head
    )
    if resolver_state_path.is_file():
        resolver_state = json.loads(resolver_state_path.read_text(encoding="utf-8"))
        if resolver_state.get("status") == "resolved":
            fixed_index = state["members"].index(fixed)
            current_descendants = state["members"][fixed_index + 1 :]
            recorded_descendants = resolver_state.get("members_before") or []
            keys = ("number", "head_sha", "head_branch", "base_branch")
            current_snapshot = [
                tuple(member.get(key) for key in keys)
                for member in current_descendants
            ]
            recorded_snapshot = [
                tuple(member.get(key) for key in keys)
                for member in recorded_descendants
            ]
            if current_snapshot != recorded_snapshot:
                stack_stop(
                    path,
                    state,
                    "propagation_snapshot_changed",
                    "a descendant moved after PR Conflict Resolver prepared the "
                    "propagation; refusing to publish its preserved workspace",
                    member=args.fixed_pr,
                )
                return
    process = run(
        [
            sys.executable,
            str(script),
            "descendant-propagate",
            f"{state['repository']}#{args.fixed_pr}",
            "--stack-number",
            str(state["stack_number"]),
            "--fixed-pr",
            str(args.fixed_pr),
            "--expected-head",
            args.expected_head,
            "--repo-root",
            state["repo_root"],
            "--state",
            str(resolver_state_path),
        ],
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "no output"
        stack_stop(
            path,
            state,
            "propagation_failed",
            detail,
            member=args.fixed_pr,
        )
        return
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        stack_stop(
            path,
            state,
            "propagation_failed",
            f"PR Conflict Resolver returned invalid JSON: {error}",
            member=args.fixed_pr,
        )
        return
    if not isinstance(result, dict) or result.get("result") not in {
        "published",
        "no_descendants",
    }:
        detail = (
            result.get("detail")
            if isinstance(result, dict)
            else "PR Conflict Resolver returned no result object"
        )
        stack_stop(
            path,
            state,
            "propagation_conflicted"
            if isinstance(result, dict) and result.get("result") == "conflicted"
            else "propagation_failed",
            str(detail or result),
            member=args.fixed_pr,
        )
        return
    contained = False
    for delay in (0, *PROPAGATION_CONTAINMENT_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            refresh_stack_state(state)
        except WorkflowError as error:
            stack_stop(path, state, "topology_changed", str(error))
            return
        fixed_index = next(
            index
            for index, member in enumerate(state["members"])
            if member["number"] == args.fixed_pr
        )
        descendants = state["members"][fixed_index + 1 :]
        if all(
            commit_contains(
                state["repository"], args.expected_head, member["head_sha"]
            )
            for member in descendants
        ):
            contained = True
            break
    if not contained:
        stack_stop(
            path,
            state,
            "propagation_did_not_contain",
            f"PR Conflict Resolver reported {result['result']}, but the descendants "
            f"do not contain pull request #{args.fixed_pr} at {args.expected_head}",
            member=args.fixed_pr,
        )
        return
    propagation = {
        "fixed_pr": args.fixed_pr,
        "fixed_head_sha": args.expected_head,
        "result": result["result"],
        "members_published": result.get("members_published") or [],
        "recorded_at": utc_now(),
    }
    state.setdefault("propagations", []).append(propagation)
    state.setdefault("propagation_attempts", []).append(attempt)
    state["propagation_attempts"] = sorted(set(state["propagation_attempts"]))
    if args.checkpoint_id:
        state.setdefault("propagated_pushes", []).append(args.checkpoint_id)
        state["propagated_pushes"] = sorted(set(state["propagated_pushes"]))
    cursor = int(state.get("cursor", 0))
    if (
        cursor < len(state["members"])
        and state["members"][cursor]["number"] == args.fixed_pr
    ):
        state["members"][cursor]["dispatched_head_sha"] = args.expected_head
    save_state(path, state)
    emit(
        {
            "state": str(path),
            "stack_number": state["stack_number"],
            **propagation,
            "propagation_result": propagation["result"],
            "result": "propagated",
        }
    )


def command_stack_status(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    state = load_stack_state(path)
    if state.get("status") in {"active", "complete"}:
        require_tools()
        try:
            refresh_stack_state(state)
        except WorkflowError as error:
            stack_stop(path, state, "topology_changed", str(error))
            return
        if stop_for_stale_cleared_member(path, state):
            return
    emit(
        {
            "result": "ready",
            "state": str(path),
            "status": state.get("status"),
            "run_id": state.get("run_id"),
            "repository": state.get("repository"),
            "stack_number": state.get("stack_number"),
            "selected_pr": (state.get("target") or {}).get("number"),
            "cursor": state.get("cursor"),
            "outcome": state.get("outcome"),
            "members": [
                {
                    "number": member["number"],
                    "head_sha": member["head_sha"],
                    "ci_status": member.get("ci_status"),
                    "stage_outcome": member.get("stage_outcome"),
                    "clean_at_head_sha": member.get("clean_at_head_sha"),
                    "accepted_pushes": member.get("accepted_pushes") or [],
                    "skip_note": member.get("skip_note"),
                }
                for member in state.get("members") or []
            ],
            "propagations": state.get("propagations") or [],
            "reason": state.get("reason"),
            "detail": state.get("detail"),
            "blocked_member": state.get("blocked_member"),
            "last_helper_activity": last_helper_activity(state),
        }
    )


def command_stack_cleanup(args: argparse.Namespace) -> None:
    path = cli_path(args.state)
    load_stack_state(path)
    path.unlink()
    emit({"result": "cleaned_up", "state": str(path)})


def stage_outcome(state: dict[str, Any]) -> str | None:
    """Name this run's ending in the vocabulary an orchestrator records.

    A pipeline reads greenness from GitHub rather than from here, so this states
    only how the loop itself ended: `cleared` when it recorded green, `skipped`
    when the head ran no applicable checks, `carried` when it spent its own
    iteration cap, and `escalated` when it handed the pull request back to a
    person for any other reason. A cap bounds one pass of the orchestrator, which
    gives the stage the rest of its budget on the next pass rather than ending
    the run.

    Returning `None` means this state supports no claim about an ending, and the
    field is then left out so a reader sees an absent answer rather than a
    manufactured one. State exists from the moment `preflight` writes it, so a
    run killed before it decided anything leaves exactly the same absence as a
    run still in flight. Neither is `no_progress`, which asserts that a run ran
    to completion and achieved nothing. Only the agent can support that claim,
    because only a live agent can report on a run it saw end, and it says so in
    its own report instead.

    A reader is entitled to take any value it finds at face value, so a value
    this function cannot support must not appear at all.
    """
    escalation = state.get("escalation")
    if escalation:
        if escalation.get("reason") == "max_iterations_reached":
            return "carried"
        return "escalated"
    outcome = state.get("outcome")
    if outcome == "no_checks":
        return "skipped"
    if outcome == "green":
        return "cleared"
    return None


def stage_outcome_fields(state: dict[str, Any]) -> dict[str, str]:
    """Carry the stage outcome only when the state supports naming one."""
    outcome = stage_outcome(state)
    return {"stage_outcome": outcome} if outcome else {}


def work_progress(state: dict[str, Any]) -> dict[str, Any] | None:
    run_state = state.get("run") or {}
    decision = run_state.get("decision")
    if not isinstance(decision, dict):
        return None
    action = decision.get("action")
    phase = {
        "attribute": "diagnosing",
        "fix": "fixing",
        "rerun": "rerunning",
        "waiting": "waiting",
    }.get(action)
    if phase is None:
        return None
    action_checks = decision.get("action_checks") or decision.get("checks") or []
    pending_checks = decision.get("pending_checks") or []
    return {
        "phase": phase,
        "action": action,
        "reason": decision.get("reason"),
        "action_checks": action_checks,
        "pending_checks": pending_checks,
        "observed_at": decision.get("observed_at"),
        "detail": decision.get("detail"),
    }


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
        "local_validation": state.get("local_validation") or [],
        "escalation": state.get("escalation"),
        "outcome": state.get("outcome"),
        **stage_outcome_fields(state),
        "clean_at_head_sha": state.get("clean_at_head_sha"),
        "skip_note": state.get("skip_note"),
        "iterations": int(state.get("iterations", 0)),
        "pipeline_budget": state.get("pipeline_budget"),
        "invocation_budget": state.get("invocation_budget"),
        "budget_scope": state.get("budget_scope", "lifetime"),
        "accepted_pushes": state.get("accepted_pushes") or [],
        "progress": work_progress(state),
        "last_helper_activity": last_helper_activity(state),
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
                    "clean_at_head_sha": None,
                    "history": [],
                    "local_validation": [],
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
            **stage_outcome_fields(state),
            "clean_at_head_sha": state.get("clean_at_head_sha"),
            "skip_note": state.get("skip_note"),
            "escalation": state.get("escalation"),
            "local_validation": state.get("local_validation") or [],
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
            "budget_scope": state.get("budget_scope", "lifetime"),
            "invocation_budget": state.get("invocation_budget"),
            "accepted_pushes": state.get("accepted_pushes") or [],
            "progress": work_progress(state),
            "last_helper_activity": last_helper_activity(state),
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

    stack_start = subparsers.add_parser(
        "stack-start",
        help="detect a native stack and start its bottom-up CI-only run",
    )
    stack_start.add_argument(
        "target",
        nargs="?",
        help=(
            "PR URL or owner/repo#number; omit only from a worktree "
            "attached to the PR's branch"
        ),
    )
    stack_start.add_argument("--repo-root")
    stack_start.add_argument("--state")
    stack_start.add_argument(
        "--pipeline-run",
        help="return the single-PR path when another orchestrator owns stack scope",
    )
    stack_start.set_defaults(function=command_stack_start)

    stack_next = subparsers.add_parser(
        "stack-next",
        help="return the next member repair or descendant propagation action",
    )
    stack_next.add_argument("--state", required=True)
    stack_next.set_defaults(function=command_stack_next)

    stack_record = subparsers.add_parser(
        "stack-record",
        help="record the current member's verified CI Fix Loop outcome",
    )
    stack_record.add_argument("--state", required=True)
    stack_record.add_argument("--member-state", required=True)
    stack_record.set_defaults(function=command_stack_record)

    stack_propagate = subparsers.add_parser(
        "stack-propagate",
        help="atomically carry one current member head through its descendants",
    )
    stack_propagate.add_argument("--state", required=True)
    stack_propagate.add_argument("--fixed-pr", type=int, required=True)
    stack_propagate.add_argument("--expected-head", required=True)
    stack_propagate.add_argument("--checkpoint-id")
    stack_propagate.set_defaults(function=command_stack_propagate)

    stack_status = subparsers.add_parser(
        "stack-status", help="print compact native stack CI state"
    )
    stack_status.add_argument("--state", required=True)
    stack_status.set_defaults(function=command_stack_status)

    stack_cleanup = subparsers.add_parser(
        "stack-cleanup", help="delete completed native stack coordination state"
    )
    stack_cleanup.add_argument("--state", required=True)
    stack_cleanup.set_defaults(function=command_stack_cleanup)

    preflight = subparsers.add_parser(
        "preflight",
        help="verify and check out a PR, then pin the head its checks ran on",
    )
    preflight.add_argument(
        "target",
        nargs="?",
        help=(
            "PR URL or owner/repo#number; omit only from a worktree "
            "attached to the PR's branch"
        ),
    )
    preflight.add_argument("--repo-root")
    preflight.add_argument("--state")
    preflight.add_argument(
        "--stack-state",
        help="native stack coordinator state that must still authorize this member",
    )
    preflight.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
    invocation = preflight.add_mutually_exclusive_group()
    invocation.add_argument(
        "--new-invocation",
        action="store_true",
        help="start a fresh standalone budget and return its invocation run token",
    )
    invocation.add_argument(
        "--invocation-run",
        help="reuse the invocation run token returned by its first preflight",
    )
    preflight.add_argument(
        "--pipeline-run",
        help=(
            "opaque identifier for one pipeline run, compared only for equality; "
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
    publish_validation = publish.add_mutually_exclusive_group()
    publish_validation.add_argument(
        "--validated",
        action="append",
        metavar="COMMAND",
        help="a covering check that ran locally and passed; repeat for each one",
    )
    publish_validation.add_argument(
        "--not-validated",
        metavar="REASON",
        help="why no covering check ran locally before this push",
    )
    publish.add_argument(
        "--rewrote",
        action="append",
        metavar="COMMAND",
        help="a covering check that rewrote files; those rewrites must already be "
        "in the commits this pushes",
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
