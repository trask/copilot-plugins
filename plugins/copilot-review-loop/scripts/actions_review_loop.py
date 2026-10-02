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
OTEL = "open-telemetry/opentelemetry-java-instrumentation"
TEST_REPO = "trask/copilot-review-loop-test"
REPOSITORIES = {OTEL: 210933087, TEST_REPO: 1400255214}
COORDINATOR = "coordinator.yml"
WORKER_PATH = ".github/workflows/copilot-worker.lock.yml"
COORDINATOR_PATH = ".github/workflows/" + COORDINATOR
STATE_BRANCH = "review-loop-state"
SCHEMA = "github.copilot.actions-review-loop-client.v1"
MAX_OUTPUT = 2 * 1024 * 1024
MAX_STATE = 1024 * 1024
TIMEOUT = 60
IS_WINDOWS = os.name == "nt"
SHA = re.compile(r"[0-9a-f]{40}\Z")
REQUEST = re.compile(r"[0-9a-f]{32}\Z")
# Source pins admit only the central handler with transactional cancellation.
CANCEL_SOURCE_SHA256 = {
    "loop/cli.py": "cb45112189f63ae244f628952a0c243d77bac3b3d7016e4b28ccdf6d15b51e95",
    "loop/coordinator.py": "58cd9e6fdaaead7995429745fe9fb6fe07710e1fd063174daceb49329bbbbe3d",
    "loop/state.py": "08880c7021e4418f618e861602a6323c67e7257783f4b2777a0f710ce95028c2",
    ".github/workflows/coordinator.yml": "14bafb312d0d54eb9bbfdf67b972143a4da82d829c983bccebd579984b7519ff",
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
    match = re.fullmatch(
        r"(?:https://github\.com/([^/]+/[^/]+)/pull/|([^#]+)#)([1-9][0-9]{0,7})",
        value,
    )
    require(match is not None, "Use an exact GitHub PR URL or owner/repo#number")
    repo, number = match[1] or match[2], int(match[3])
    require(repo in REPOSITORIES and (repo != TEST_REPO or number == 1),
            "Unsupported target; only OTel or personal test PR1 is supported")
    return repo, number


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


def binding(api, run_id, revision, path, *, events=frozenset({"workflow_dispatch"})):
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
            and run.get("head_sha") == revision, "Workflow/run attempt identity mismatch")
    return run


def validate_state(api, state, repo, number, expected=None):
    require(isinstance(state, dict) and type(state.get("schema")) is int
            and state["schema"] == 1, "Unsupported central checkpoint schema")
    request = state.get("request")
    require(isinstance(request, dict) and type(request.get("schema")) is int
            and request["schema"] == 1 and request.get("repo") == repo
            and type(request.get("pr")) is int and request["pr"] == number
            and type(request.get("head_repo_id")) is int
            and request["head_repo_id"] == REPOSITORIES[repo]
            and pattern(REQUEST, request.get("request_id"))
            and pattern(SHA, request.get("workflow_revision"))
            and pattern(SHA, request.get("frozen_sha"))
            and request.get("source_private") is False, "Central request identity mismatch")
    require(integer(state.get("generation")) and pattern(SHA, state.get("expected_sha"))
            and state.get("stage") in STAGES
            and request.get("mode") in {"preview", "shadow", "publish"},
            "Unsupported checkpoint generation, stage or mode")
    require(state.get("reason") is None or
            (isinstance(state["reason"], str) and len(state["reason"]) <= 2000),
            "Invalid checkpoint reason")
    require(request["mode"] != "publish" or repo == TEST_REPO,
            "Upstream publication is forbidden")
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
            require(type(report.get("schema")) is int and report["schema"] == 1
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
                        and report["verification"] == "failed",
                        "Unexpected verifier envelope")
            else:
                require(remote.get("status") == "completed"
                        and remote.get("conclusion") == "success",
                        "A failed or incomplete worker cannot supply a candidate report")
                require(report.get("repo") == repo and type(report.get("pr")) is int
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
                            and type(objective.get("schema")) is int and objective["schema"] == 1
                            and type(objective.get("generation")) is int
                            and objective["generation"] == state["generation"]
                            and objective.get("request_digest") == report["request_digest"]
                            and objective.get("run_id") == run["id"]
                            and type(objective.get("run_attempt")) is int
                            and objective["run_attempt"] == 1
                            and objective.get("candidate_commit") == candidate.get("commit")
                            and objective.get("candidate_tree") == candidate.get("tree")
                            and objective.get("bundle_sha256") == candidate.get("bundle_sha256"),
                            "Objective receipt identity mismatch")
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


def read_state(api, repo, number, expected=None):
    ref = api.get(f"repos/{CENTRAL}/git/ref/heads/{STATE_BRANCH}")
    require(ref.get("ref") == "refs/heads/" + STATE_BRANCH
            and isinstance(ref.get("object"), dict)
            and pattern(SHA, ref["object"].get("sha")), "Wrong state branch identity")
    revision = ref["object"]["sha"]
    name = (f"pr-{number}.json" if repo == OTEL
            else f"pr-{REPOSITORIES[repo]}-{number}.json")
    state = parse_json(api.file(name, revision, MAX_STATE))
    validate_state(api, state, repo, number, expected)
    return state, revision


def observation(state, revision):
    request = state["request"]
    report = (state.get("report") or {}) if state["stage"] != "cancelled" else {}
    if report.get("schema") != 1:
        report = {}
    objective = report.get("objective_validation") or {}
    validation_run = report.get("validation_run") or {}
    return {
        "schema": SCHEMA, "backend": "actions", "central_repository": CENTRAL,
        "state_revision": revision, "target": f"{request['repo']}#{request['pr']}",
        "target_repository_id": request["head_repo_id"],
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
        "objective_status": objective.get("status"),
        "general_qualified": objective.get("qualified"),
        "next_check_at": state.get("next_check_at"),
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
        require(pattern(REQUEST, args.request_id) and args.generation > 0
                and pattern(SHA, args.revision), "Exact prior request identity required")
        require(operation != "start-publication" or (repo == TEST_REPO and number == 1),
                "Upstream publication is forbidden")
        state, state_revision = read_state(
            api, repo, number, (args.request_id, args.generation, args.revision))
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
            require(repo == TEST_REPO and number == 1
                    and args.authorize_personal_publication
                    and args.publication_auth == "fine_grained_pat"
                    and state["stage"] in TERMINAL,
                    "Publication requires explicit personal test PR1 authorization")
            require(api.get("user").get("id") == OWNER_ID, "Personal-owner dispatch required")
            auth = "fine_grained_pat"
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
            and isinstance(value.get("baseline_run_ids"), list)
            and all(integer(item) for item in value["baseline_run_ids"])
            and isinstance(value.get("since"), str)
            and re.fullmatch(r"[0-9T:Z-]{20}", value["since"]),
            "Receipt identity mismatch")
    inputs = value.get("inputs", {})
    require(isinstance(inputs, dict)
            and inputs.get("operation") in {"launch", "start-publication", "cancel"},
            "Unsupported receipt operation")
    target(inputs.get("target", ""))
    candidates = []
    for run in runs(api, value["since"]):
        if run.get("id") in value["baseline_run_ids"]:
            continue
        suffix = inputs.get("previous_request") or str(run.get("id"))
        if run.get("display_title") != f"Review loop {inputs['operation']} {suffix}":
            continue
        checked = binding(api, run["id"], value["central_revision"], COORDINATOR_PATH)
        candidates.append({"id": checked["id"],
                           "url": f"https://github.com/{CENTRAL}/actions/runs/{checked['id']}",
                           "status": checked.get("status"), "conclusion": checked.get("conclusion")})
    return {
        "schema": SCHEMA, "backend": "actions", "dispatch": value["dispatch"],
        "coordinator_run_candidates": candidates, "request_binding": "unconfirmed",
        "pipeline_success": False,
        "result": "ambiguous" if len(candidates) > 1 else "pending" if not candidates else "unconfirmed",
        "message": "Run titles do not prove target/request ownership. No POST was retried.",
    }


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
            required = name != "status"
            command.add_argument("--request-id", required=required)
            command.add_argument("--generation", type=int, required=required)
            command.add_argument("--revision", required=required)
        if name == "start-publication":
            command.add_argument("--authorize-personal-publication", action="store_true", required=True)
            command.add_argument("--publication-auth", choices=["fine_grained_pat"], required=True)
    return result


def execute(api, args):
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
