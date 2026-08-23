#!/usr/bin/env python3
"""Run the PR pipeline as two bounded foreground sweeps."""

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
import uuid
from typing import Any, Callable


MAX_SWEEPS = 2
DEFAULT_STAGE_MODEL = "gpt-5.6-sol"
DEFAULT_EFFORT = "high"
CLAUDE_FAMILY = "claude"
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

STAGES: tuple[dict[str, Any], ...] = (
    {
        "stage": STAGE_CONFLICT,
        "plugin": STAGE_CONFLICT,
        "agent": f"{STAGE_CONFLICT}:{STAGE_CONFLICT}",
        "module": "conflict_fix_loop",
        "marker": ("mergeable_at_head_sha",),
        "model": DEFAULT_STAGE_MODEL,
    },
    {
        "stage": STAGE_SELF_REVIEW,
        "plugin": STAGE_SELF_REVIEW,
        "agent": f"{STAGE_SELF_REVIEW}:{STAGE_SELF_REVIEW}",
        "module": "self_review_loop",
        "marker": ("review", "clean_at_head_sha"),
        "model": "claude-opus-5",
        "requires_family": CLAUDE_FAMILY,
    },
    {
        "stage": STAGE_COPILOT_REVIEW,
        "plugin": STAGE_COPILOT_REVIEW,
        "agent": f"{STAGE_COPILOT_REVIEW}:{STAGE_COPILOT_REVIEW}",
        "module": "copilot_review_loop",
        "marker": ("clean_at_head_sha",),
        "model": DEFAULT_STAGE_MODEL,
    },
    {
        "stage": STAGE_CI,
        "plugin": STAGE_CI,
        "agent": f"{STAGE_CI}:{STAGE_CI}",
        "module": "ci_fix_loop",
        "marker": ("clean_at_head_sha",),
        "model": DEFAULT_STAGE_MODEL,
    },
    {
        "stage": STAGE_DESCRIPTION,
        "plugin": STAGE_DESCRIPTION,
        "agent": f"{STAGE_DESCRIPTION}:{STAGE_DESCRIPTION}",
        "module": "pr_description",
        "marker": ("validated_head_sha",),
        "model": DEFAULT_STAGE_MODEL,
    },
)
STAGE_NAMES = tuple(entry["stage"] for entry in STAGES)
STAGE_BY_NAME = {entry["stage"]: entry for entry in STAGES}

STAGE_PERMISSION_FLAGS = ("--allow-all-tools", "--allow-all-paths")
STAGE_AUTOPILOT_FLAGS = ("--autopilot", "--max-autopilot-continues", "5")
PIPELINE_RUN_FLAG = "--pipeline-run"
PIPELINE_ITERATION_FLAG = "--pipeline-iteration"
PIPELINE_MAX_ITERATIONS_FLAG = "--pipeline-max-iterations"
CLEARING_OUTCOMES = frozenset({"cleared", "skipped"})


class WorkflowError(RuntimeError):
    pass


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise WorkflowError(
            f"{' '.join(command)} did not return within {timeout} seconds"
        ) from error
    if check and process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "no output"
        raise WorkflowError(
            f"{' '.join(command)} failed ({process.returncode}): {detail}"
        )
    return process


def git(repo_root: Path, *arguments: str) -> str:
    return run(["git", "-C", str(repo_root), *arguments]).stdout.strip()


def git_or_none(repo_root: Path, *arguments: str) -> str | None:
    result = run(
        ["git", "-C", str(repo_root), *arguments],
        check=False,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def git_succeeds(repo_root: Path, *arguments: str) -> bool:
    return (
        run(["git", "-C", str(repo_root), *arguments], check=False).returncode == 0
    )


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def report_event(
    report: Callable[[dict[str, Any]], None] | None,
    event: str,
    **fields: Any,
) -> None:
    if report is not None:
        report({"event": event, **fields})


def gh_json(arguments: list[str]) -> Any:
    process = run(["gh", *arguments])
    try:
        return json.loads(process.stdout) if process.stdout.strip() else None
    except json.JSONDecodeError as error:
        raise WorkflowError(f"gh returned invalid JSON: {error}") from error


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_cli_path(value: str, *, windows: bool) -> str:
    if windows:
        match = re.fullmatch(r"/([A-Za-z])(?:/(.*))?", value)
        if match:
            drive, remainder = match.groups()
            return f"{drive.upper()}:/{remainder or ''}"
    return value


def copilot_home() -> Path:
    value = os.environ.get("COPILOT_HOME", "").strip()
    return (
        Path(normalize_cli_path(value, windows=IS_WINDOWS))
        if value
        else Path.home() / ".copilot"
    )


def require_tools() -> None:
    missing = [name for name in ("git", "gh", "copilot") if shutil.which(name) is None]
    if missing:
        raise WorkflowError(f"required tools not found: {', '.join(missing)}")


SHIM_SUFFIXES = (".cmd", ".bat")


def path_image(name: str) -> str | None:
    suffixes = [
        suffix
        for suffix in os.environ.get("PATHEXT", ".EXE").split(os.pathsep)
        if suffix and suffix.lower() not in SHIM_SUFFIXES
    ]
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for suffix in suffixes:
            candidate = Path(directory) / f"{name}{suffix}"
            if candidate.is_file():
                return str(candidate)
    return None


def resolve_launch_program(name: str) -> str:
    if not IS_WINDOWS:
        return name
    if os.sep in name or (os.altsep and os.altsep in name):
        if Path(name).suffix.lower() in SHIM_SUFFIXES:
            raise WorkflowError(f"the stage program {name} is a command shim")
        return name
    if Path(name).suffix:
        resolved = shutil.which(name)
        if resolved is None:
            raise WorkflowError(f"the stage program {name} is not on PATH")
        if Path(resolved).suffix.lower() in SHIM_SUFFIXES:
            raise WorkflowError(f"the stage program {name} resolves to a command shim")
        return resolved
    image = path_image(name)
    if image is not None:
        return image
    resolved = shutil.which(name)
    if resolved is None:
        raise WorkflowError(f"the stage program {name} is not on PATH")
    raise WorkflowError(
        f"the stage program {name} resolves only to the command shim {resolved}"
    )


def build_target(owner: str, repo: str, number: int) -> dict[str, Any]:
    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        "repo_name": f"{owner}/{repo}",
        "pr_url": f"https://github.com/{owner}/{repo}/pull/{number}",
    }


def parse_target(target: str, repo_name: str | None = None) -> dict[str, Any]:
    match = PR_URL_PATTERN.fullmatch(target) or SHORT_TARGET_PATTERN.fullmatch(target)
    if match:
        values = match.groupdict()
        return build_target(values["owner"], values["repo"], int(values["number"]))
    bare = BARE_NUMBER_PATTERN.fullmatch(target)
    if bare and repo_name:
        owner, separator, repo = repo_name.partition("/")
        if separator and owner and repo:
            return build_target(owner, repo, int(bare.group("number")))
    if bare:
        raise WorkflowError("a bare PR number requires repository context")
    raise WorkflowError(
        "target must be a GitHub PR URL, owner/repo#number, or bare PR number"
    )


def resolve_repo_root() -> Path:
    root = run(["git", "rev-parse", "--show-toplevel"]).stdout.strip()
    if not root:
        raise WorkflowError("the current directory is not in a git repository")
    return Path(root).resolve()


def github_repo_from_remote(url: str) -> str | None:
    match = re.search(
        r"(?:github\.com[/:])(?P<owner>[^/:\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?$",
        url.strip(),
        re.IGNORECASE,
    )
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def repo_name_for(repo_root: Path) -> str | None:
    result = run(
        ["gh", "repo", "view", "--json", "nameWithOwner"],
        cwd=repo_root,
        check=False,
    )
    if result.returncode == 0:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = None
        name = payload.get("nameWithOwner") if isinstance(payload, dict) else None
        if isinstance(name, str) and "/" in name:
            return name
    remote = git_or_none(repo_root, "remote", "get-url", "origin")
    return github_repo_from_remote(remote or "")


def resolve_target(value: str | None, repo_root: Path) -> dict[str, Any]:
    if value:
        return parse_target(value, repo_name_for(repo_root))
    payload = gh_json(["pr", "view", "--json", "url"])
    url = payload.get("url") if isinstance(payload, dict) else None
    if not isinstance(url, str):
        raise WorkflowError(
            "no pull request was named and the checked-out branch has no pull request"
        )
    return parse_target(url)


def read_pull_request(target: dict[str, Any]) -> dict[str, Any]:
    payload = gh_json(
        [
            "pr",
            "view",
            str(target["number"]),
            "--repo",
            target["repo_name"],
            "--json",
            "number,title,url,state,isDraft,headRefName,baseRefName,headRefOid",
        ]
    )
    if not isinstance(payload, dict):
        raise WorkflowError(f"could not read {target['pr_url']}")
    return {
        "number": payload.get("number"),
        "title": payload.get("title"),
        "pr_url": payload.get("url") or target["pr_url"],
        "repo_name": target["repo_name"],
        "owner": target["owner"],
        "repo": target["repo"],
        "state": payload.get("state"),
        "is_draft": bool(payload.get("isDraft")),
        "head_branch": payload.get("headRefName"),
        "base_branch": payload.get("baseRefName"),
        "head_sha": payload.get("headRefOid"),
    }


def target_remote(repo_root: Path, target: dict[str, Any]) -> str:
    wanted = target["repo_name"].lower()
    listing = git_or_none(repo_root, "remote", "-v") or ""
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            name = github_repo_from_remote(fields[1])
            if name and name.lower() == wanted:
                return fields[0]
    return f"https://github.com/{target['repo_name']}.git"


def worktree_dirt(repo_root: Path) -> str:
    return git(repo_root, "status", "--porcelain=v1")


def unreachable_commit_count(repo_root: Path) -> int:
    value = git_or_none(
        repo_root,
        "rev-list",
        "--count",
        "HEAD",
        "--not",
        "--branches",
        "--remotes",
        "--tags",
    )
    try:
        return int(value or "0")
    except ValueError:
        return 0


def fetch_pr_head(repo_root: Path, target: dict[str, Any]) -> dict[str, Any]:
    remote = target_remote(repo_root, target)
    reference = f"refs/pull/{target['number']}/head"
    result = run(
        ["git", "-C", str(repo_root), "fetch", "--quiet", remote, reference],
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        return {
            "result": "blocked",
            "reason": "checkout_failed",
            "detail": f"could not fetch {reference}: {detail}",
        }
    landed = git_or_none(repo_root, "rev-parse", "FETCH_HEAD")
    if not landed:
        return {
            "result": "blocked",
            "reason": "checkout_failed",
            "detail": f"fetching {reference} did not produce FETCH_HEAD",
        }
    return {"result": "ready", "head_sha": landed}


def checkout_fetched_head(repo_root: Path, head_sha: str) -> dict[str, Any]:
    result = run(
        ["git", "-C", str(repo_root), "checkout", "--quiet", "--detach", head_sha],
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        return {
            "result": "blocked",
            "reason": "checkout_failed",
            "detail": f"could not check out pull request head {head_sha}: {detail}",
        }
    landed = git_or_none(repo_root, "rev-parse", "HEAD")
    if landed != head_sha:
        return {
            "result": "blocked",
            "reason": "checkout_failed",
            "detail": (
                f"the worktree is on {landed} after checking out pull request "
                f"head {head_sha}"
            ),
        }
    return {
        "result": "ready",
        "head_sha": landed,
    }


def sync_worktree(
    repo_root: Path,
    target: dict[str, Any],
    pr: dict[str, Any],
    *,
    known_safe_head: str | None,
) -> dict[str, Any]:
    dirt = worktree_dirt(repo_root)
    if dirt:
        return {
            "result": "blocked",
            "reason": "dirty_worktree",
            "detail": f"the worktree has uncommitted changes:\n{dirt}",
        }
    fetched = fetch_pr_head(repo_root, target)
    if fetched["result"] != "ready":
        return fetched
    desired = fetched["head_sha"]
    local = git(repo_root, "rev-parse", "HEAD")
    if local == desired:
        return {"result": "ready", "head_sha": local, "changed": False}

    branch = git_or_none(repo_root, "branch", "--show-current") or ""
    safe_to_move = local == known_safe_head
    safe_to_move = safe_to_move or git_succeeds(
        repo_root, "merge-base", "--is-ancestor", local, desired
    )
    if branch and branch != pr.get("head_branch"):
        safe_to_move = True
    if not safe_to_move and unreachable_commit_count(repo_root) == 0:
        safe_to_move = branch != pr.get("head_branch")
    if not safe_to_move:
        return {
            "result": "blocked",
            "reason": "local_head_not_published",
            "detail": (
                f"the worktree head {local} is not the pull request head {desired}; "
                "moving it could hide local commits"
            ),
        }
    checked_out = checkout_fetched_head(repo_root, desired)
    if checked_out["result"] != "ready":
        return checked_out
    return {
        **checked_out,
        "changed": True,
        "previous_head_sha": local,
    }


def settle_after_stage(
    repo_root: Path,
    target: dict[str, Any],
    *,
    started_head_sha: str,
) -> dict[str, Any]:
    dirt = worktree_dirt(repo_root)
    if dirt:
        return {
            "result": "blocked",
            "reason": "stage_left_dirty_worktree",
            "detail": f"a stage left uncommitted changes:\n{dirt}",
        }
    local = git(repo_root, "rev-parse", "HEAD")
    fetched = fetch_pr_head(repo_root, target)
    if fetched["result"] != "ready":
        return fetched
    remote = fetched["head_sha"]
    if local == remote:
        return {"result": "ready", "head_sha": remote, "changed": remote != started_head_sha}

    published = git_succeeds(
        repo_root, "merge-base", "--is-ancestor", local, remote
    )
    if local != started_head_sha and not published:
        return {
            "result": "blocked",
            "reason": "stage_left_unpublished_commits",
            "detail": (
                f"a stage moved the local head from {started_head_sha} to {local}, "
                f"but the pull request head is {remote}"
            ),
        }
    checked_out = checkout_fetched_head(repo_root, remote)
    if checked_out["result"] != "ready":
        return checked_out
    return {
        **checked_out,
        "changed": checked_out["head_sha"] != started_head_sha,
        "previous_head_sha": local,
    }


def stage_script_path(entry: dict[str, Any]) -> Path:
    return (
        copilot_home()
        / "installed-plugins"
        / "trask-plugins"
        / entry["plugin"]
        / "scripts"
        / f"{entry['module']}.py"
    )


def stage_state_path(entry: dict[str, Any], target: dict[str, Any]) -> Path:
    name = f"{target['owner']}--{target['repo']}--{target['number']}.json"
    return Path.home() / ".copilot" / "run" / entry["plugin"] / name


def read_stage_status(
    entry: dict[str, Any], target: dict[str, Any]
) -> dict[str, Any]:
    script = stage_script_path(entry)
    state = stage_state_path(entry, target)
    common = {
        "installed": script.is_file(),
        "script": str(script),
        "state": str(state),
        "payload": None,
    }
    if not script.is_file():
        return {**common, "ok": False, "reason": "plugin_not_installed"}
    if not state.is_file():
        return {**common, "ok": False, "reason": "no_state"}
    try:
        process = run(
            [sys.executable, str(script), "status", "--state", str(state)],
            check=False,
            timeout=30,
        )
    except WorkflowError as error:
        return {
            **common,
            "ok": False,
            "reason": "status_timeout",
            "detail": str(error),
        }
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "no output"
        return {
            **common,
            "ok": False,
            "reason": "status_failed",
            "detail": detail,
        }
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        return {
            **common,
            "ok": False,
            "reason": "invalid_status_json",
            "detail": str(error),
        }
    if not isinstance(payload, dict) or payload.get("result") != "ready":
        return {
            **common,
            "ok": False,
            "reason": "status_not_ready",
            "payload": payload,
        }
    return {**common, "ok": True, "payload": payload}


def string_at(payload: dict[str, Any], path: tuple[str, ...]) -> str | None:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def inspect_stage(
    entry: dict[str, Any], target: dict[str, Any], head_sha: str
) -> dict[str, Any]:
    status = read_stage_status(entry, target)
    payload = status.get("payload")
    marker = (
        string_at(payload, entry["marker"]) if isinstance(payload, dict) else None
    )
    outcome = payload.get("stage_outcome") if isinstance(payload, dict) else None
    clear = marker == head_sha and outcome in CLEARING_OUTCOMES
    if clear:
        reason = None
    elif marker:
        reason = "clearance_is_for_an_older_head"
    else:
        reason = status.get("reason") or outcome or "not_cleared"
    return {
        "stage": entry["stage"],
        "clear": clear,
        "clear_at_head_sha": marker,
        "outcome": outcome,
        "reason": reason,
        "installed": status["installed"],
        "status_state": status["state"],
        **({"detail": status["detail"]} if status.get("detail") else {}),
    }


def inspect_stages(
    target: dict[str, Any], head_sha: str
) -> list[dict[str, Any]]:
    return [inspect_stage(entry, target, head_sha) for entry in STAGES]


def stage_models(overrides: list[str] | None) -> dict[str, str]:
    models = {entry["stage"]: entry["model"] for entry in STAGES}
    for assignment in overrides or []:
        stage, separator, model = assignment.partition("=")
        if not separator or stage not in STAGE_BY_NAME or not model.strip():
            raise WorkflowError(
                f"--stage-model expects <stage>=<model> for a known stage: {assignment}"
            )
        models[stage] = model.strip()
    for entry in STAGES:
        family = entry.get("requires_family")
        if family and family not in models[entry["stage"]].lower():
            raise WorkflowError(
                f"{entry['stage']} requires a {family} model, not "
                f"{models[entry['stage']]}"
            )
    return models


def stage_accepts_pipeline_position(entry: dict[str, Any]) -> bool:
    try:
        return PIPELINE_RUN_FLAG in stage_script_path(entry).read_text(encoding="utf-8")
    except OSError:
        return False


def pipeline_arguments(
    entry: dict[str, Any], run_id: str, sweep: int
) -> list[str]:
    if not stage_accepts_pipeline_position(entry):
        return []
    return [
        PIPELINE_RUN_FLAG,
        run_id,
        PIPELINE_ITERATION_FLAG,
        str(sweep),
        PIPELINE_MAX_ITERATIONS_FLAG,
        str(MAX_SWEEPS),
    ]


def stage_prompt(target: dict[str, Any], arguments: list[str]) -> str:
    name = f"{target['repo_name']}#{target['number']}"
    if not arguments:
        return name
    pairs = zip(arguments[::2], arguments[1::2])
    position = " ".join(f"{flag.lstrip('-')}: {value}" for flag, value in pairs)
    return (
        f"{name}\n\n{position}\n\n"
        "Add these arguments to your preflight command, exactly as written, "
        f"and change nothing else about how you run: {' '.join(arguments)}"
    )


def stage_command(
    entry: dict[str, Any],
    target: dict[str, Any],
    *,
    model: str,
    effort: str,
    run_id: str,
    sweep: int,
) -> list[str]:
    prompt = stage_prompt(target, pipeline_arguments(entry, run_id, sweep))
    return [
        resolve_launch_program("copilot"),
        "-p",
        prompt,
        "--agent",
        entry["agent"],
        "--model",
        model,
        "--effort",
        effort,
        *STAGE_AUTOPILOT_FLAGS,
        *STAGE_PERMISSION_FLAGS,
    ]


def stage_log_path(
    target: dict[str, Any], run_id: str, sweep: int, entry: dict[str, Any]
) -> Path:
    directory = (
        copilot_home()
        / "run"
        / "pr-pipeline"
        / f"{target['owner']}--{target['repo']}--{target['number']}"
        / run_id
    )
    return directory / f"{sweep}-{entry['stage']}.log"


def run_stage(
    entry: dict[str, Any],
    target: dict[str, Any],
    repo_root: Path,
    *,
    model: str,
    effort: str,
    run_id: str,
    sweep: int,
) -> dict[str, Any]:
    log_path = stage_log_path(target, run_id, sweep, entry)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = stage_command(
        entry,
        target,
        model=model,
        effort=effort,
        run_id=run_id,
        sweep=sweep,
    )
    started_at = utc_now()
    try:
        with open(log_path, "w", encoding="utf-8", newline="\n") as log:
            kwargs: dict[str, Any] = {
                "cwd": str(repo_root),
                "stdin": subprocess.DEVNULL,
                "stdout": log,
                "stderr": subprocess.STDOUT,
                "check": False,
            }
            if IS_WINDOWS:
                kwargs["creationflags"] = getattr(
                    subprocess, "CREATE_NO_WINDOW", 0
                )
            process = subprocess.run(command, **kwargs)
        return {
            "returncode": process.returncode,
            "log_path": str(log_path),
            "started_at": started_at,
            "ended_at": utc_now(),
        }
    except OSError as error:
        return {
            "returncode": None,
            "log_path": str(log_path),
            "started_at": started_at,
            "ended_at": utc_now(),
            "error": str(error),
        }


def blocked_result(
    *,
    pr: dict[str, Any],
    run_id: str,
    sweeps: int,
    runs: list[dict[str, Any]],
    reason: str,
    detail: str,
    stage: str | None = None,
) -> dict[str, Any]:
    return {
        "result": "blocked",
        "run_id": run_id,
        "pr": pr,
        "head_sha": pr.get("head_sha"),
        "sweeps": sweeps,
        "runs": runs,
        "stage": stage,
        "reason": reason,
        "detail": detail,
    }


def run_pipeline(
    target: dict[str, Any],
    repo_root: Path,
    *,
    models: dict[str, str],
    effort: str,
    report: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    run_id = uuid.uuid4().hex
    runs: list[dict[str, Any]] = []
    known_safe_head: str | None = None
    completed_sweeps = 0
    report_event(report, "pipeline_started", run_id=run_id, target=target["pr_url"])

    for sweep in range(1, MAX_SWEEPS + 1):
        pr = read_pull_request(target)
        if pr["state"] != "OPEN":
            return blocked_result(
                pr=pr,
                run_id=run_id,
                sweeps=completed_sweeps,
                runs=runs,
                reason="pr_not_open",
                detail=f"the pull request is {pr['state']}",
            )
        synced = sync_worktree(
            repo_root,
            target,
            pr,
            known_safe_head=known_safe_head,
        )
        if synced["result"] != "ready":
            return blocked_result(
                pr=pr,
                run_id=run_id,
                sweeps=completed_sweeps,
                runs=runs,
                reason=synced["reason"],
                detail=synced["detail"],
            )
        known_safe_head = synced["head_sha"]
        sweep_started_head = known_safe_head
        head_changed = False
        report_event(
            report,
            "sweep_started",
            run_id=run_id,
            sweep=sweep,
            head_sha=sweep_started_head,
        )

        for entry in STAGES:
            pr = read_pull_request(target)
            if pr["state"] != "OPEN":
                return blocked_result(
                    pr=pr,
                    run_id=run_id,
                    sweeps=completed_sweeps,
                    runs=runs,
                    stage=entry["stage"],
                    reason="pr_not_open",
                    detail=f"the pull request is {pr['state']}",
                )
            synced = sync_worktree(
                repo_root,
                target,
                pr,
                known_safe_head=known_safe_head,
            )
            if synced["result"] != "ready":
                return blocked_result(
                    pr=pr,
                    run_id=run_id,
                    sweeps=completed_sweeps,
                    runs=runs,
                    stage=entry["stage"],
                    reason=synced["reason"],
                    detail=synced["detail"],
                )
            current_head = synced["head_sha"]
            head_changed = head_changed or current_head != known_safe_head
            known_safe_head = current_head

            before = inspect_stage(entry, target, current_head)
            if before["clear"]:
                record = {
                    "stage": entry["stage"],
                    "sweep": sweep,
                    "action": "already_clear",
                    "started_head_sha": current_head,
                    "ended_head_sha": current_head,
                    "outcome": before["outcome"],
                }
                runs.append(record)
                report_event(report, "stage_finished", run_id=run_id, **record)
                continue
            if not before["installed"]:
                record = {
                    "stage": entry["stage"],
                    "sweep": sweep,
                    "action": "plugin_not_installed",
                    "started_head_sha": current_head,
                    "ended_head_sha": current_head,
                    "outcome": None,
                }
                runs.append(record)
                report_event(report, "stage_finished", run_id=run_id, **record)
                continue

            report_event(
                report,
                "stage_started",
                run_id=run_id,
                stage=entry["stage"],
                sweep=sweep,
                head_sha=current_head,
                started_at=utc_now(),
            )
            launched = run_stage(
                entry,
                target,
                repo_root,
                model=models[entry["stage"]],
                effort=effort,
                run_id=run_id,
                sweep=sweep,
            )
            settled = settle_after_stage(
                repo_root,
                target,
                started_head_sha=current_head,
            )
            record = {
                "stage": entry["stage"],
                "sweep": sweep,
                "action": "launched",
                "model": models[entry["stage"]],
                "started_head_sha": current_head,
                **launched,
            }
            if settled["result"] != "ready":
                record["ended_head_sha"] = git_or_none(repo_root, "rev-parse", "HEAD")
                runs.append(record)
                report_event(report, "stage_finished", run_id=run_id, **record)
                return blocked_result(
                    pr=read_pull_request(target),
                    run_id=run_id,
                    sweeps=completed_sweeps,
                    runs=runs,
                    stage=entry["stage"],
                    reason=settled["reason"],
                    detail=settled["detail"],
                )

            ended_head = settled["head_sha"]
            known_safe_head = ended_head
            head_changed = head_changed or ended_head != current_head
            after = inspect_stage(entry, target, ended_head)
            record.update(
                {
                    "ended_head_sha": ended_head,
                    "outcome": after["outcome"],
                    "clear": after["clear"],
                }
            )
            runs.append(record)
            report_event(report, "stage_finished", run_id=run_id, **record)

        completed_sweeps = sweep
        pr = read_pull_request(target)
        synced = sync_worktree(
            repo_root,
            target,
            pr,
            known_safe_head=known_safe_head,
        )
        if synced["result"] != "ready":
            return blocked_result(
                pr=pr,
                run_id=run_id,
                sweeps=completed_sweeps,
                runs=runs,
                reason=synced["reason"],
                detail=synced["detail"],
            )
        final_head = synced["head_sha"]
        known_safe_head = final_head
        head_changed = head_changed or final_head != sweep_started_head
        stages = inspect_stages(target, final_head)
        report_event(
            report,
            "sweep_finished",
            run_id=run_id,
            sweep=sweep,
            head_sha=final_head,
            head_changed=head_changed,
            uncleared_stages=[
                stage["stage"] for stage in stages if not stage["clear"]
            ],
        )
        if all(stage["clear"] for stage in stages):
            return {
                "result": "complete",
                "run_id": run_id,
                "pr": pr,
                "head_sha": final_head,
                "sweeps": completed_sweeps,
                "stages": stages,
                "runs": runs,
            }
        if sweep == MAX_SWEEPS:
            return {
                "result": "incomplete",
                "reason": "two_sweeps_finished",
                "run_id": run_id,
                "pr": pr,
                "head_sha": final_head,
                "sweeps": completed_sweeps,
                "stages": stages,
                "runs": runs,
            }
        if not head_changed:
            return {
                "result": "incomplete",
                "reason": "stages_not_clear",
                "run_id": run_id,
                "pr": pr,
                "head_sha": final_head,
                "sweeps": completed_sweeps,
                "stages": stages,
                "runs": runs,
            }

    raise WorkflowError("the pipeline ended without a result")


def command_run(args: argparse.Namespace) -> None:
    require_tools()
    repo_root = resolve_repo_root()
    target = resolve_target(args.target, repo_root)
    result = run_pipeline(
        target,
        repo_root,
        models=stage_models(args.stage_model),
        effort=args.effort,
        report=emit,
    )
    emit({"event": "pipeline_finished", **result})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser(
        "run", help="run up to two foreground sweeps over the five stages"
    )
    command.add_argument(
        "target",
        nargs="?",
        help=(
            "PR URL, owner/repo#number, or a bare number when the repository is "
            "known; omit only from a branch attached to the pull request"
        ),
    )
    command.add_argument(
        "--stage-model",
        action="append",
        help="pin one stage's model as <stage>=<model>; repeatable",
    )
    command.add_argument("--effort", default=DEFAULT_EFFORT)
    command.set_defaults(function=command_run)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.function(args)
        return 0
    except (WorkflowError, json.JSONDecodeError, OSError) as error:
        emit({"event": "pipeline_finished", "result": "error", "error": str(error)})
        return 1
    except KeyboardInterrupt:
        emit({"event": "pipeline_finished", "result": "error", "error": "interrupted"})
        return 130


if __name__ == "__main__":
    sys.exit(main())
