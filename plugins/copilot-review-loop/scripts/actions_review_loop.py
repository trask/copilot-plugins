#!/usr/bin/env python3
"""Explicit, data-only client for the private central Actions review loop."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid


CENTRAL = "trask/copilot-workflows"
CENTRAL_ID = 1398868192
OWNER_ID = 218610
COORDINATOR = "coordinator.yml"
WORKER_PATH = ".github/workflows/copilot-worker.lock.yml"
COORDINATOR_PATH = ".github/workflows/" + COORDINATOR
STATE_BRANCH = "review-loop-state"
SCHEMA = "github.copilot.actions-review-loop-client.v1"
MAX_OUTPUT = 2 * 1024 * 1024
MAX_STATE = 1024 * 1024
MAX_STATE_FILES = 1000
MAX_STATE_TOTAL = 16 * MAX_STATE
MAX_MATCHING_STATES = 20
TIMEOUT = 60
IS_WINDOWS = os.name == "nt"
SHA = re.compile(r"[0-9a-f]{40}\Z")
REQUEST = re.compile(r"[0-9a-f]{32}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}\Z")
# Source pins admit only the central handler with transactional cancellation.
CANCEL_SOURCE_SHA256 = {
    "loop/cli.py": "3109418f4e3bef570db6c1e2d7f5c73fc2548fc7fad99017ba780462ec596837",
    "loop/coordinator.py": "1f07ca1c5568458c77fc4092f0b785cd702da9422f0d01a4d692261a9820bfa4",
    "loop/state.py": "c32f9b1bb44a56e4b68272ef7697c686b45cf44191040c8292d834cd641e2a28",
    ".github/workflows/coordinator.yml": "ae8b57066c1bdd40d4a263f650c06a9965ceb3d61ba8347df5faa4f57bc5e205",
}
TERMINAL = {"preview_complete", "shadow_complete", "blocked", "failed",
            "cancelled", "exhausted", "clean"}
STAGES = TERMINAL | {
    "preview", "ready", "source_pending", "dispatch_intent", "dispatched",
    "running", "verify_pending", "auth_pending", "capability_recheck",
    "capability_intent", "waiting_capability", "publish_pending",
    "publication_intent", "published", "root_effects", "review_request_intent",
    "waiting_review", "waiting_ci",
}


class AdapterError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise AdapterError(message)


def unique(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "Duplicate JSON key")
        value[key] = item
    return value


def parse_json(data):
    require(len(data) <= MAX_OUTPUT, "JSON exceeds output limit")
    try:
        return json.loads(data, object_pairs_hook=unique,
                          parse_constant=lambda _: require(False, "Non-finite JSON"))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise AdapterError("Invalid JSON") from error


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def integer(value):
    return type(value) is int and value > 0


def pattern(regex, value):
    return isinstance(value, str) and regex.fullmatch(value) is not None


def run_gh(arguments, body=None):
    """Bound both pipes while preserving the user's ordinary gh authentication."""
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {
                       "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME",
                       "USERPROFILE", "APPDATA", "LOCALAPPDATA", "GH_CONFIG_DIR",
                       "GH_TOKEN", "GITHUB_TOKEN", "HTTPS_PROXY", "HTTP_PROXY",
                       "NO_PROXY", "SSL_CERT_FILE",
                   }}
    environment.update(GH_HOST="github.com", GH_PROMPT_DISABLED="1", GH_PAGER="cat")
    streams = [bytearray(), bytearray()]
    overflow = threading.Event()
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
    try:
        with subprocess.Popen(
            ["gh", "api", "--hostname", "github.com", *arguments],
            stdin=subprocess.PIPE if body is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
            **options,
        ) as process:
            def read(pipe, buffer):
                while chunk := pipe.read1(8192):
                    if len(buffer) + len(chunk) > MAX_OUTPUT:
                        overflow.set()
                        process.kill()
                        break
                    buffer.extend(chunk)

            readers = [
                threading.Thread(target=read, args=(pipe, buffer), daemon=True)
                for pipe, buffer in zip((process.stdout, process.stderr), streams)
            ]
            for reader in readers:
                reader.start()
            try:
                if body is not None:
                    process.stdin.write(canonical(body))
                    process.stdin.close()
                process.wait(timeout=TIMEOUT)
            except (subprocess.TimeoutExpired, BrokenPipeError) as error:
                process.kill()
                process.wait(timeout=5)
                raise AdapterError("GitHub API timeout or interrupted input") from error
            finally:
                for reader in readers:
                    reader.join(timeout=5)
            require(not any(reader.is_alive() for reader in readers),
                    "GitHub API pipes did not close")
            require(not overflow.is_set(), "GitHub API output exceeds limit")
            # Do not echo gh diagnostics, which may contain authentication details.
            require(process.returncode == 0,
                    "GitHub API failed; check ordinary gh access and central permissions")
            return bytes(streams[0])
    except OSError as error:
        raise AdapterError("Could not start GitHub CLI") from error


class API:
    def get(self, path):
        value = parse_json(run_gh([path, "--method", "GET"]))
        require(isinstance(value, dict), "GitHub API response must be a JSON object")
        return value

    def dispatch(self, inputs):
        run_gh([f"repos/{CENTRAL}/actions/workflows/{COORDINATOR}/dispatches",
                "--method", "POST", "--input", "-"],
               {"ref": "main", "inputs": inputs})

    def file(self, path, revision, limit):
        value = self.get(f"repos/{CENTRAL}/contents/{path}?ref={revision}")
        require(isinstance(value, dict) and value.get("type") == "file"
                and value.get("path") == path and value.get("encoding") == "base64"
                and integer(value.get("size")) and value["size"] <= limit
                and pattern(SHA, value.get("sha"))
                and isinstance(value.get("content"), str), "Unbounded or wrong central file")
        try:
            data = base64.b64decode("".join(value["content"].split()), validate=True)
        except (ValueError, KeyError, TypeError) as error:
            raise AdapterError("Invalid central file encoding") from error
        require(len(data) == value["size"] and len(data) <= limit,
                "Central file size mismatch")
        blob = b"blob " + str(len(data)).encode("ascii") + b"\0" + data
        require(hashlib.sha1(blob).hexdigest() == value["sha"],
                "Central Git blob identity mismatch")
        return data


def target(value):
    require(isinstance(value, str), "Use an exact GitHub PR URL or owner/repo#number")
    match = re.fullmatch(
        r"(?:https://github\.com/([A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100})/pull/"
        r"|([A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100})#)([1-9][0-9]{0,7})",
        value,
    )
    require(match is not None, "Use an exact GitHub PR URL or owner/repo#number")
    repo, number = match[1] or match[2], int(match[3])
    owner, name = repo.split("/")
    require(owner[0] != "-" and owner[-1] != "-" and name not in {".", ".."},
            "Invalid GitHub repository name")
    return repo, number


def repository_name(value):
    if not pattern(REPOSITORY, value):
        return False
    owner, name = value.split("/")
    return owner[0] != "-" and owner[-1] != "-" and name not in {".", ".."}


def head_ref(value):
    return (isinstance(value, str) and 0 < len(value) <= 200
            and re.fullmatch(r"[A-Za-z0-9_./-]+", value) is not None
            and not any(part in value for part in ("..", "//", "@{"))
            and not value.startswith(("-", "/", "."))
            and not value.endswith(("/", ".", ".lock")))


def central(api):
    repo = api.get(f"repos/{CENTRAL}")
    require(repo.get("id") == CENTRAL_ID and repo.get("full_name") == CENTRAL
            and isinstance(repo.get("owner"), dict) and repo["owner"].get("id") == OWNER_ID
            and repo.get("default_branch") == "main" and repo.get("private") is True,
            "Central repository identity or private visibility mismatch")


def main_revision(api):
    value = api.get(f"repos/{CENTRAL}/git/ref/heads/main")
    require(value.get("ref") == "refs/heads/main"
            and isinstance(value.get("object"), dict)
            and pattern(SHA, value["object"].get("sha")), "Wrong central main ref")
    return value["object"]["sha"]


def binding(api, run_id, revision, path, *, events=frozenset({"workflow_dispatch"}),
            owner=False):
    require(integer(run_id), "Invalid run identity")
    run = api.get(f"repos/{CENTRAL}/actions/runs/{run_id}")
    require(run.get("id") == run_id and run.get("run_attempt") == 1
            and type(run.get("run_attempt")) is int
            and isinstance(run.get("repository"), dict)
            and run["repository"].get("id") == CENTRAL_ID
            and run["repository"].get("full_name") == CENTRAL
            and isinstance(run.get("head_repository"), dict)
            and run["head_repository"].get("id") == CENTRAL_ID
            and run["head_repository"].get("full_name") == CENTRAL
            and run.get("event") in events
            and run.get("head_branch") == "main"
            and isinstance(run.get("path"), str) and run["path"].split("@")[0] == path
            and pattern(SHA, run.get("head_sha"))
            and (revision is None or run["head_sha"] == revision),
            "Workflow/run attempt identity mismatch")
    if owner:
        require(isinstance(run.get("actor"), dict)
                and type(run["actor"].get("id")) is int and run["actor"]["id"] == OWNER_ID
                and isinstance(run.get("triggering_actor"), dict)
                and type(run["triggering_actor"].get("id")) is int
                and run["triggering_actor"]["id"] == OWNER_ID, "Owner dispatch identity mismatch")
    return run


def frozen_identity(api, state, request):
    require(request.get("authorized_actor_id") == OWNER_ID
            and type(request.get("authorized_actor_id")) is int,
            "Authorized actor identity mismatch")
    launch = request.get("launch_run")
    require(isinstance(launch, dict) and set(launch) == {"id", "attempt", "actor_id"}
            and integer(launch.get("id")) and type(launch.get("attempt")) is int
            and launch["attempt"] == 1 and type(launch.get("actor_id")) is int
            and launch["actor_id"] == request["authorized_actor_id"],
            "Launch run identity mismatch")
    binding(api, launch["id"], None, COORDINATOR_PATH, owner=True)
    budgets = request.get("budgets")
    require(isinstance(budgets, dict) and type(budgets.get("max_iterations")) is int
            and budgets["max_iterations"] in {2, 5}
            and type(budgets.get("deadline_seconds")) is int
            and budgets["deadline_seconds"] == 7200
            and type(request.get("frozen_at")) is int
            and type(request.get("deadline")) is int
            and request["frozen_at"] < request["deadline"]
            <= request["frozen_at"] + budgets["deadline_seconds"]
            and type(state.get("iteration")) is int
            and 0 <= state["iteration"] <= budgets["max_iterations"],
            "Invalid frozen pipeline budget or consumed count")
    if request.get("freeze_status") == "not_frozen":
        require(all(request.get(key) is None for key in
                    ("repo_id", "head_repo", "head_repo_id", "head_ref", "frozen_sha"))
                and state.get("expected_sha") is None
                and state.get("stage") == "blocked"
                and state.get("reason") == "human_gate_target_repository_read_access"
                and state.get("run") is None and state.get("report") is None
                and state.get("iteration") == 0 and request.get("mode") == "preview",
                "Unfrozen access gate cannot carry source or worker evidence")
        return
    require(integer(request.get("repo_id")) and request["repo_id"] < 10 ** 20
            and integer(request.get("head_repo_id"))
            and repository_name(request.get("head_repo")) and head_ref(request.get("head_ref"))
            and pattern(SHA, request.get("frozen_sha"))
            and type(request.get("source_private")) is bool
            and type(request.get("target_private")) is bool,
            "Frozen base/head identity mismatch")
    same_repo = request["repo"].casefold() == request["head_repo"].casefold()
    require(same_repo == (request["repo_id"] == request["head_repo_id"])
            and (not same_repo or request["source_private"] == request["target_private"]),
            "Inconsistent base/head repository identity")
    if request["mode"] == "publish":
        publication = request.get("publication")
        require(isinstance(publication, dict) and publication.get("profile") == "generic-v2"
                and type(publication.get("authorized_actor_id")) is int
                and publication["authorized_actor_id"] == request["authorized_actor_id"]
                and publication.get("auth_mode") in {"disabled", "fine_grained_pat"}
                and publication.get("reply_bot_threads") is False
                and type(publication.get("max_pipelines")) is int
                and publication["max_pipelines"] == budgets["max_iterations"],
                "Publication profile or authorization mismatch")


def validation_plan(report):
    claim = report.get("validation_claim")
    require(isinstance(claim, dict) and type(claim.get("schema")) is int
            and claim["schema"] == 1 and claim.get("status") in {"passed", "failed", "not_run"}
            and isinstance(claim.get("commands"), list)
            and 1 <= len(claim["commands"]) <= 8, "Invalid untrusted validation plan")
    commands = []
    for command in claim["commands"]:
        require(isinstance(command, dict) and set(command) == {"argv", "exit_code"}
                and isinstance(command.get("argv"), list) and 1 <= len(command["argv"]) <= 40
                and all(isinstance(arg, str) and 0 < len(arg) <= 500
                        and not any(char in arg for char in ("\0", "\n", "\r"))
                        for arg in command["argv"])
                and type(command.get("exit_code")) is int
                and 0 <= command["exit_code"] <= 255, "Invalid validation command")
        commands.append(command["argv"])
    require(sum(len(arg) for argv in commands for arg in argv) <= 16000,
            "Validation plan exceeds limit")
    return commands


def native_receipt(api, objective, report, request, state):
    commands = validation_plan(report)
    candidate = report["candidate"]
    require(candidate.get("parent") == request["frozen_sha"]
            and objective.get("executor") == "awf-0.28.23-no-model"
            and objective.get("plan_sha256") == hashlib.sha256(canonical(commands)).hexdigest()
            and objective.get("source_bundle_sha256") == candidate.get("source_bundle_sha256")
            and objective.get("status") in {"passed", "failed", "not_run"}
            and isinstance(objective.get("commands"), list)
            and len(objective["commands"]) <= len(commands),
            "Native plan or source receipt mismatch")
    if request["source_private"]:
        source = state.get("source")
        manifest = source.get("manifest") if isinstance(source, dict) else None
        require(isinstance(manifest, dict) and type(manifest.get("schema")) is int
                and manifest["schema"] == 2 and manifest.get("repo") == request["repo"]
                and manifest.get("repo_id") == request["head_repo_id"]
                and type(manifest.get("repo_id")) is int
                and type(manifest.get("pr")) is int and manifest["pr"] == request["pr"]
                and manifest.get("request_id") == request["request_id"]
                and manifest.get("request_digest") == report["request_digest"]
                and manifest.get("frozen_sha") == request["frozen_sha"]
                and pattern(SHA, manifest.get("tree"))
                and type(manifest.get("history_count")) is int
                and manifest["history_count"] == 1 and type(manifest.get("shallow")) is bool
                and manifest.get("workflow_revision") == request["workflow_revision"]
                and type(manifest.get("generation")) is int
                and manifest["generation"] == state["generation"]
                and integer(manifest.get("run_id")) and manifest.get("run_attempt") == 1
                and type(manifest.get("run_attempt")) is int
                and pattern(SHA256, manifest.get("bundle_sha256"))
                and manifest["bundle_sha256"] == candidate.get("source_bundle_sha256"),
                "Private source receipt identity mismatch")
        binding(api, manifest["run_id"], request["workflow_revision"], COORDINATOR_PATH,
                events={"workflow_dispatch", "workflow_run", "schedule"})
        require(integer(source.get("artifact_id"))
                and isinstance(source.get("artifact_digest"), str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", source["artifact_digest"]),
                "Private source artifact identity missing")
        artifact = api.get(f"repos/{CENTRAL}/actions/artifacts/{source['artifact_id']}")
        producer = artifact.get("workflow_run")
        require(type(artifact.get("id")) is int and artifact["id"] == source["artifact_id"]
                and artifact.get("name") == "source-" + request["request_id"]
                and isinstance(producer, dict) and type(producer.get("id")) is int
                and producer["id"] == manifest["run_id"]
                and producer.get("head_sha") == request["workflow_revision"]
                and artifact.get("digest") == source["artifact_digest"]
                and artifact.get("expired") is False
                and type(artifact.get("size_in_bytes")) is int
                and 0 < artifact["size_in_bytes"] <= 16 * 1024 * 1024 + 65536,
                "Private source artifact provenance mismatch")
    setup = objective.get("setup")
    if setup is not None:
        evidence = [setup, *objective["commands"]]
        expected = [["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                     "checkout", "--quiet", "--detach", candidate["commit"]], *commands]
        for command, argv in zip(evidence, expected):
            require(isinstance(command, dict)
                    and set(command) == {"argv", "exit_code", "log_sha256"}
                    and command.get("argv") == argv and type(command.get("exit_code")) is int
                    and 0 <= command["exit_code"] <= 255
                    and pattern(SHA256, command.get("log_sha256")),
                    "Native command receipt mismatch")
        if objective["status"] == "passed":
            require(len(evidence) == len(expected)
                    and all(command["exit_code"] == 0 for command in evidence),
                    "Passed native receipt lacks complete zero-exit evidence")
    else:
        require(objective["status"] != "passed" and not objective["commands"],
                "Passed native receipt lacks protected setup evidence")


def validate_state(api, state, repo, number, expected=None):
    require(isinstance(state, dict) and type(state.get("schema")) is int
            and state["schema"] in {1, 2}, "Unsupported central checkpoint schema")
    version = state["schema"]
    request = state.get("request")
    require(isinstance(request, dict) and type(request.get("schema")) is int
            and request["schema"] == version and isinstance(request.get("repo"), str)
            and repository_name(request["repo"]) and request["repo"].casefold() == repo.casefold()
            and type(request.get("pr")) is int and request["pr"] == number
            and pattern(REQUEST, request.get("request_id"))
            and pattern(SHA, request.get("workflow_revision")), "Central request identity mismatch")
    require(integer(state.get("generation"))
            and (pattern(SHA, state.get("expected_sha"))
                 or version == 2 and request.get("freeze_status") == "not_frozen")
            and isinstance(state.get("stage"), str) and state["stage"] in STAGES
            and isinstance(request.get("mode"), str)
            and request["mode"] in {"preview", "shadow", "publish"},
            "Unsupported checkpoint generation, stage or mode")
    require(state.get("reason") is None or
            (isinstance(state["reason"], str) and len(state["reason"]) <= 2000),
            "Invalid checkpoint reason")
    if version == 2:
        frozen_identity(api, state, request)
    else:
        require(integer(request.get("head_repo_id"))
                and pattern(SHA, request.get("frozen_sha"))
                and type(request.get("source_private")) is bool, "Legacy request identity mismatch")
    if expected is not None:
        require((request["request_id"], state["generation"], request["workflow_revision"])
                == expected, "Request, generation or trusted revision mismatch")
    run = state.get("run")
    if run is not None:
        require(isinstance(run, dict) and type(run.get("attempt")) is int
                and run["attempt"] == 1, "Worker run attempt mismatch")
        remote = binding(api, run.get("id"), request["workflow_revision"], WORKER_PATH)
        require(remote.get("display_title") == "Copilot shadow " + request["request_id"],
                "Worker request title mismatch")
    report = state.get("report") if state["stage"] != "cancelled" else None
    if report is not None:
        require(isinstance(report, dict), "Invalid trusted report")
        # Diagnostic run IDs are not candidate reports.
        if "schema" in report or "request_id" in report:
            require(type(report.get("schema")) is int and report["schema"] == version
                    and report.get("request_id") == request["request_id"]
                    and report.get("request_digest")
                    == hashlib.sha256(canonical(request)).hexdigest()
                    and run is not None and integer(report.get("run_id"))
                    and report["run_id"] == run["id"]
                    and type(report.get("run_attempt")) is int and report["run_attempt"] == 1,
                    "Trusted report identity mismatch")
            if "verification" in report:
                require(type(report.get("generation")) is int
                        and report["generation"] == state["generation"]
                        and report["verification"] in {"failed", "verified"},
                        "Unexpected verifier envelope")
                if report["verification"] == "verified":
                    result = report.get("result")
                    require(remote.get("status") == "completed"
                            and remote.get("conclusion") == "success"
                            and isinstance(result, dict) and type(result.get("schema")) is int
                            and result["schema"] == version
                            and result.get("request_digest") == report["request_digest"]
                            and result.get("request_id") == request["request_id"]
                            and result.get("repo") == request["repo"]
                            and type(result.get("pr")) is int and result["pr"] == request["pr"]
                            and integer(result.get("run_id")) and result["run_id"] == run["id"]
                            and type(result.get("run_attempt")) is int and result["run_attempt"] == 1
                            and result.get("workflow_revision") == request["workflow_revision"]
                            and result.get("frozen_sha") == request["frozen_sha"],
                            "Nested verifier identity mismatch")
            else:
                require(remote.get("status") == "completed"
                        and remote.get("conclusion") == "success",
                        "A failed or incomplete worker cannot supply a candidate report")
                require(report.get("repo") == request["repo"] and type(report.get("pr")) is int
                        and report["pr"] == number
                        and report.get("workflow_revision") == request["workflow_revision"]
                        and report.get("frozen_sha") == request["frozen_sha"]
                        and report.get("validation") in {
                            "failed", "not_run", "unattested", "passed_attested"},
                        "Trusted report target or revision mismatch")
            if "objective_validation" in report:
                objective = report["objective_validation"]
                require(isinstance(objective, dict) and objective.get("qualified") is False,
                        "Invalid or generally qualified objective receipt")
                if objective.get("status") != "missing":
                    candidate = report.get("candidate", {})
                    require(isinstance(candidate, dict)
                            and pattern(SHA, candidate.get("commit"))
                            and pattern(SHA, candidate.get("tree"))
                            and isinstance(candidate.get("bundle_sha256"), str)
                            and re.fullmatch(r"[0-9a-f]{64}", candidate["bundle_sha256"])
                            and type(objective.get("schema")) is int and objective["schema"] == version
                            and type(objective.get("generation")) is int
                            and objective["generation"] == state["generation"]
                            and objective.get("request_digest") == report["request_digest"]
                            and integer(objective.get("run_id")) and objective["run_id"] == run["id"]
                            and type(objective.get("run_attempt")) is int
                            and objective["run_attempt"] == 1
                            and objective.get("candidate_commit") == candidate.get("commit")
                            and objective.get("candidate_tree") == candidate.get("tree")
                            and objective.get("bundle_sha256") == candidate.get("bundle_sha256"),
                            "Objective receipt identity mismatch")
                    if version == 2:
                        native_receipt(api, objective, report, request, state)
            validation_run = report.get("validation_run")
            if validation_run is not None:
                require(isinstance(validation_run, dict)
                        and type(validation_run.get("attempt")) is int
                        and validation_run["attempt"] == 1,
                        "Validation run attempt mismatch")
                binding(api, validation_run.get("id"), request["workflow_revision"],
                        COORDINATOR_PATH,
                        events={"workflow_dispatch", "workflow_run", "schedule"})
        else:
            require(set(report) <= {"run_ids", "agent_started"}
                    and isinstance(report.get("run_ids"), list)
                    and len(report["run_ids"]) <= 20
                    and all(integer(item) for item in report["run_ids"])
                    and ("agent_started" not in report
                         or type(report["agent_started"]) is bool),
                    "Unbound data is not a trusted report")
    artifacts = state.get("artifacts")
    require(isinstance(artifacts, list) and len(artifacts) <= 100,
            "Artifact metadata exceeds limit")
    for artifact in artifacts:
        require(isinstance(artifact, dict) and integer(artifact.get("id"))
                and isinstance(artifact.get("name"), str) and len(artifact["name"]) <= 200
                and type(artifact.get("size_in_bytes")) is int
                and 0 <= artifact["size_in_bytes"] <= 64 * 1024 * 1024
                and type(artifact.get("expired")) is bool,
                "Invalid or oversized artifact metadata")
    return state


def state_files(api):
    ref = api.get(f"repos/{CENTRAL}/git/ref/heads/{STATE_BRANCH}")
    require(ref.get("ref") == "refs/heads/" + STATE_BRANCH
            and isinstance(ref.get("object"), dict)
            and pattern(SHA, ref["object"].get("sha")), "Wrong state branch identity")
    revision = ref["object"]["sha"]
    commit = api.get(f"repos/{CENTRAL}/git/commits/{revision}")
    require(commit.get("sha") == revision and isinstance(commit.get("tree"), dict)
            and pattern(SHA, commit["tree"].get("sha")), "State commit identity mismatch")
    tree = api.get(f"repos/{CENTRAL}/git/trees/{commit['tree']['sha']}")
    require(tree.get("sha") == commit["tree"]["sha"] and tree.get("truncated") is False
            and isinstance(tree.get("tree"), list) and len(tree["tree"]) <= MAX_STATE_FILES,
            "State tree exceeds limit or is truncated")
    files = {}
    total = 0
    for entry in tree["tree"]:
        require(isinstance(entry, dict) and entry.get("type") == "blob"
                and entry.get("mode") == "100644" and pattern(SHA, entry.get("sha"))
                and isinstance(entry.get("path"), str)
                and re.fullmatch(r"[A-Za-z0-9-]+\.json", entry["path"]) is not None
                and entry["path"] not in files and type(entry.get("size")) is int
                and 0 < entry["size"] <= MAX_STATE, "Unsafe state branch file")
        files[entry["path"]] = entry
        total += entry["size"]
    require(total <= MAX_STATE_TOTAL, "State branch exceeds total byte limit")
    return files, revision


def read_state(api, repo, number, expected=None, *, allow_missing=False):
    files, revision = state_files(api)
    candidates = [name for name in files if re.fullmatch(
        rf"pr-v2-(?:[1-9][0-9]{{0,19}}|[0-9a-f]{{32}})-{number}\.json"
        rf"|pr-(?:[1-9][0-9]{{0,19}}-)?{number}\.json", name)]
    require(len(candidates) <= MAX_MATCHING_STATES, "Matching PR checkpoints exceed read limit")
    matches = []
    for name in candidates:
        data = api.file(name, revision, MAX_STATE)
        blob = b"blob " + str(len(data)).encode("ascii") + b"\0" + data
        require(len(data) == files[name]["size"]
                and hashlib.sha1(blob).hexdigest() == files[name]["sha"],
                "Checkpoint differs from pinned state tree")
        state = parse_json(data)
        request = state.get("request") if isinstance(state, dict) else None
        require(isinstance(request, dict), "Checkpoint missing request identity")
        if (isinstance(request.get("repo"), str)
                and request["repo"].casefold() == repo.casefold() and request.get("pr") == number):
            if state.get("schema") == 2:
                identity = request.get("repo_id")
                key = str(identity) if integer(identity) else hashlib.sha256(
                    request["repo"].casefold().encode("utf-8")).hexdigest()[:32]
                require(name == f"pr-v2-{key}-{number}.json",
                        "Checkpoint base repository namespace mismatch")
            else:
                require(not name.startswith("pr-v2-"), "Legacy state in active namespace")
            matches.append(state)
    frozen = [state for state in matches if state.get("schema") == 2
              and integer(state["request"].get("repo_id"))]
    current = [state for state in matches if state.get("schema") == 2]
    matches = frozen or current or matches
    if allow_missing and not matches:
        return None, revision
    require(len(matches) == 1, "Unknown or ambiguous PR checkpoint")
    state = matches[0]
    validate_state(api, state, repo, number, expected)
    return state, revision


def observation(state, revision):
    request = state["request"]
    report = (state.get("report") or {}) if state["stage"] != "cancelled" else {}
    if report.get("schema") != state["schema"]:
        report = {}
    objective = report.get("objective_validation") or {}
    validation_run = report.get("validation_run") or {}
    return {
        "schema": SCHEMA, "backend": "actions", "central_repository": CENTRAL,
        "state_revision": revision, "target": f"{request['repo']}#{request['pr']}",
        "checkpoint_schema": state["schema"], "legacy_read_only": state["schema"] == 1,
        "active_schema": state["schema"] == 2,
        "target_repository_id": request.get("repo_id") if state["schema"] == 2 else None,
        "head_repository": request.get("head_repo"),
        "head_repository_id": request.get("head_repo_id"),
        "head_ref": request.get("head_ref"), "source_private": request.get("source_private"),
        "target_private": request.get("target_private"),
        "source_frozen": pattern(SHA, request.get("frozen_sha")),
        "launch_run_id": (request.get("launch_run") or {}).get("id"),
        "launch_run_attempt": (request.get("launch_run") or {}).get("attempt"),
        "launch_actor_id": (request.get("launch_run") or {}).get("actor_id"),
        "launch_request_binding": "unconfirmed",
        "request_id": request["request_id"], "generation": state["generation"],
        "frozen_sha": request["frozen_sha"],
        "expected_sha": state["expected_sha"],
        "workflow_revision": request["workflow_revision"], "mode": request["mode"],
        "central_stage": state["stage"], "reason": state.get("reason"),
        "terminal": state["stage"] in TERMINAL,
        "pipeline_success": False,
        "completion_evidence": "Central checkpoint observation, not local acceptance",
        "worker_run_id": (state.get("run") or {}).get("id"),
        "worker_run_attempt": (state.get("run") or {}).get("attempt"),
        "worker_run_url": (f"https://github.com/{CENTRAL}/actions/runs/{state['run']['id']}"
                           if state.get("run") else None),
        "validation_run_id": validation_run.get("id"),
        "validation_run_url": (
            f"https://github.com/{CENTRAL}/actions/runs/{validation_run['id']}"
            if validation_run else None),
        "validation": report.get("validation"),
        "verification": report.get("verification"),
        "objective_status": objective.get("status"),
        "general_qualified": objective.get("qualified"),
        "next_check_at": state.get("next_check_at"),
        "pipelines_consumed": state.get("iteration"),
        "max_pipelines": (request.get("budgets") or {}).get("max_iterations"),
    }


def receipt_path(value):
    path = Path(value)
    require(path.is_absolute() and not path.is_symlink(), "Receipt must be an absolute regular path")
    checkout = Path(__file__).resolve().parents[3]
    require(not path.resolve().is_relative_to(checkout), "Keep private receipts outside the checkout")
    for directory in (Path.cwd(), *Path.cwd().parents):
        if (directory / ".git").exists():
            require(not path.resolve().is_relative_to(directory),
                    "Keep private receipts outside the active checkout")
            break
    return path


def save_receipt(path, value, *, create=False):
    with path.open("xb" if create else "wb") as file:
        file.write(canonical(value) + b"\n")
        file.flush()
        os.fsync(file.fileno())


def runs(api, since):
    result = api.get(
        f"repos/{CENTRAL}/actions/workflows/{COORDINATOR}/runs"
        f"?event=workflow_dispatch&created=%3E%3D{since}&per_page=20",
    )
    require(isinstance(result.get("workflow_runs"), list)
            and type(result.get("total_count")) is int
            and 0 <= result["total_count"] <= 20 and len(result["workflow_runs"]) <= 20
            and all(isinstance(run, dict) and integer(run.get("id"))
                    for run in result["workflow_runs"]),
            "Coordinator run listing exceeds reconciliation limit")
    return result["workflow_runs"]


def dispatch(api, args):
    repo, number = target(args.target)
    operation = args.command
    if operation == "launch":
        require(args.mode in {"preview", "shadow"}, "Launch cannot publish")
        previous, generation = "", ""
        auth = "disabled"
    else:
        identity = (args.request_id, args.generation, args.revision)
        new_phase = operation == "start-publication" and all(item is None for item in identity)
        require(new_phase or (pattern(REQUEST, args.request_id) and integer(args.generation)
                             and pattern(SHA, args.revision)),
                "Supply all three exact prior identity fields, or none for a new publication target")
        if new_phase:
            state = None
            previous, generation = "", "0"
        else:
            state, state_revision = read_state(api, repo, number, identity)
            require(state["schema"] == 2, "Legacy checkpoints are inactive and read-only")
            previous, generation = args.request_id, str(args.generation)
        auth = "disabled"
        if operation == "cancel":
            receipt_path(args.receipt)
            if state["stage"] == "cancelled":
                return dict(observation(state, state_revision), dispatch="not_needed",
                            operation_result="already_cancelled")
            require(state["stage"] not in TERMINAL,
                    "Completed terminal evidence cannot be cancelled")
        if operation == "start-publication":
            require(args.authorize_publication
                    and args.publication_auth == "fine_grained_pat"
                    and (state is None or state["stage"] in TERMINAL),
                    "Publication requires explicit target mutation authorization and terminal prior state")
            auth = "fine_grained_pat"
    user = api.get("user")
    require(type(user.get("id")) is int and user["id"] == OWNER_ID, "Central-owner dispatch required")
    revision = main_revision(api)
    if operation == "cancel":
        require(set(CANCEL_SOURCE_SHA256) == {
            "loop/cli.py", "loop/coordinator.py", "loop/state.py",
            ".github/workflows/coordinator.yml",
        },
                "Central exact-request cancellation guard is not qualified")
        for source, digest in CANCEL_SOURCE_SHA256.items():
            require(hashlib.sha256(api.file(source, revision, 128000)).hexdigest() == digest,
                    "Central exact-request cancellation guard source mismatch")
    path = receipt_path(args.receipt)
    require(not path.exists(), "Receipt already exists; reconcile without repeating POST")
    since = dt.datetime.fromtimestamp(time.time() - 5, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    baseline = runs(api, since)
    inputs = {
        "operation": operation, "target": f"https://github.com/{repo}/pull/{number}",
        "mode": args.mode if operation == "launch" else
                "publish" if operation == "start-publication" else state["request"]["mode"],
        "previous_request": previous, "previous_generation": generation,
        "publication_auth": auth,
    }
    value = {
        "schema": SCHEMA, "backend": "actions", "client_id": uuid.uuid4().hex,
        "central_revision": revision, "inputs": inputs, "since": since,
        "actor_id": user["id"],
        "baseline_run_ids": [run["id"] for run in baseline], "dispatch": "uncertain",
        "request_binding": "unconfirmed",
    }
    save_receipt(path, value, create=True)
    try:
        api.dispatch(inputs)
    except AdapterError as error:
        raise AdapterError(f"Dispatch uncertain; reconcile receipt, never repeat POST. {error}") from error
    value["dispatch"] = "acknowledged"
    try:
        save_receipt(path, value)
    except OSError as error:
        raise AdapterError(
            "Dispatch acknowledged but receipt update failed; reconcile, never repeat POST"
        ) from error
    return dict(value, receipt=str(path), pipeline_success=False,
                message="Queued centrally. HTTP acknowledgment does not bind a request or prove completion.")


def reconcile(api, args):
    path = receipt_path(args.receipt)
    require(path.is_file() and path.stat().st_size <= 16000, "Invalid dispatch receipt")
    value = parse_json(path.read_bytes())
    require(isinstance(value, dict) and value.get("schema") == SCHEMA
            and value.get("backend") == "actions"
            and pattern(SHA, value.get("central_revision"))
            and value.get("dispatch") in {"acknowledged", "uncertain"}
            and type(value.get("actor_id")) is int and value["actor_id"] == OWNER_ID
            and isinstance(value.get("baseline_run_ids"), list)
            and len(value["baseline_run_ids"]) <= 20
            and all(integer(item) for item in value["baseline_run_ids"])
            and isinstance(value.get("since"), str)
            and re.fullmatch(r"[0-9T:Z-]{20}", value["since"]),
            "Receipt identity mismatch")
    inputs = value.get("inputs", {})
    require(isinstance(inputs, dict)
            and set(inputs) == {"operation", "target", "mode", "previous_request",
                                "previous_generation", "publication_auth"}
            and inputs.get("operation") in {"launch", "start-publication", "cancel"}
            and inputs.get("mode") in {"preview", "shadow", "publish"}
            and inputs.get("publication_auth") in {"disabled", "fine_grained_pat"},
            "Unsupported receipt operation")
    repo, number = target(inputs.get("target", ""))
    candidates = []
    for run in runs(api, value["since"]):
        if run.get("id") in value["baseline_run_ids"]:
            continue
        suffix = inputs.get("previous_request") or str(run.get("id"))
        if run.get("display_title") != f"Review loop {inputs['operation']} {suffix}":
            continue
        checked = binding(api, run["id"], value["central_revision"], COORDINATOR_PATH, owner=True)
        candidates.append({"id": checked["id"],
                           "url": f"https://github.com/{CENTRAL}/actions/runs/{checked['id']}",
                           "status": checked.get("status"), "conclusion": checked.get("conclusion")})
    result = {
        "schema": SCHEMA, "backend": "actions", "dispatch": value["dispatch"],
        "coordinator_run_candidates": candidates, "request_binding": "unconfirmed",
        "pipeline_success": False,
        "result": "ambiguous" if len(candidates) > 1 else "pending" if not candidates else "unconfirmed",
        "message": "Run titles do not prove target/request ownership. No POST was retried.",
    }
    if inputs["operation"] in {"launch", "start-publication"} and candidates:
        state, revision = read_state(api, repo, number, allow_missing=True)
        if state is not None:
            result["checkpoint_observation"] = observation(state, revision)
            launch = state["request"].get("launch_run")
            if (state["schema"] == 2 and integer(state["request"].get("repo_id"))
                    and state["request"]["mode"] == inputs["mode"]
                    and state["request"]["workflow_revision"] == value["central_revision"]
                    and launch is not None
                    and any(run["id"] == launch["id"] for run in candidates)):
                result.update(request_binding="confirmed", result="bound",
                              request_id=state["request"]["request_id"],
                              generation=state["generation"], coordinator_run_id=launch["id"],
                              message="Trusted target/mode/revision/request launch_run matches the dispatch candidate. No completion is implied.")
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("launch", "start-publication", "cancel", "status", "reconcile"):
        command = commands.add_parser(name)
        command.add_argument("--backend", choices=["actions"], required=True)
        if name != "reconcile":
            command.add_argument("target")
        if name in {"launch", "start-publication", "cancel", "reconcile"}:
            command.add_argument("--receipt", required=True)
        if name == "launch":
            command.add_argument("--mode", choices=["preview", "shadow"], required=True)
        if name in {"start-publication", "cancel", "status"}:
            required = name == "cancel"
            command.add_argument("--request-id", required=required)
            command.add_argument("--generation", type=int, required=required)
            command.add_argument("--revision", required=required)
        if name == "start-publication":
            command.add_argument("--authorize-publication", action="store_true", required=True)
            command.add_argument("--publication-auth", choices=["fine_grained_pat"], required=True)
    return result


def execute(api, args):
    require(args.backend == "actions", "Explicit Actions backend required; no fallback")
    require(args.command in {"launch", "start-publication", "cancel", "status", "reconcile"},
            "Unsupported Actions client operation")
    central(api)
    if args.command == "status":
        repo, number = target(args.target)
        values = (args.request_id, args.generation, args.revision)
        require(all(value is None for value in values) or
                (pattern(REQUEST, args.request_id) and integer(args.generation)
                 and pattern(SHA, args.revision)), "Supply all three expected identity fields")
        state, revision = read_state(api, repo, number,
                                      None if args.request_id is None else values)
        return observation(state, revision)
    if args.command == "reconcile":
        return reconcile(api, args)
    return dispatch(api, args)


def main():
    try:
        value = execute(API(), parser().parse_args())
        print(json.dumps(value, sort_keys=True, ensure_ascii=True))
        return 0
    except (AdapterError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"schema": SCHEMA, "backend": "actions", "result": "error",
                          "pipeline_success": False,
                          "error": str(error) if isinstance(error, AdapterError)
                          else "Invalid or unavailable adapter data"}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
