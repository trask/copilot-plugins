import base64
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "actions_review_loop", ROOT / "scripts" / "actions_review_loop.py")
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)
REVISION = "a" * 40
REQUEST = "b" * 32


def checkpoint():
    request = {
        "schema": 1, "repo": adapter.TEST_REPO, "pr": 1, "request_id": REQUEST,
        "head_repo_id": 1400255214, "frozen_sha": "c" * 40,
        "workflow_revision": REVISION, "source_private": False, "mode": "shadow",
    }
    return {"schema": 1, "request": request, "generation": 10, "stage": "blocked",
            "expected_sha": "c" * 40,
            "reason": "validation_unqualified", "artifacts": [], "run": {
                "id": 123, "attempt": 1, "conclusion": "success"},
            "report": {
                "schema": 1, "repo": adapter.TEST_REPO, "pr": 1,
                "request_id": REQUEST, "workflow_revision": REVISION,
                "frozen_sha": "c" * 40, "run_id": 123, "run_attempt": 1,
                "request_digest": hashlib.sha256(adapter.canonical(request)).hexdigest(),
                "validation": "unattested",
            }}


def remote(run_id=123, path=None):
    return {
        "id": run_id, "run_attempt": 1, "repository": {
            "id": adapter.CENTRAL_ID, "full_name": adapter.CENTRAL},
        "head_repository": {"id": adapter.CENTRAL_ID, "full_name": adapter.CENTRAL},
        "event": "workflow_dispatch", "head_branch": "main", "head_sha": REVISION,
        "path": path or adapter.WORKER_PATH, "status": "completed",
        "conclusion": "success", "display_title": "Copilot shadow " + REQUEST,
    }


class FakeAPI:
    def __init__(self):
        self.state = checkpoint()
        self.calls = []
        self.posts = []
        self.remote = remote()
        self.listing = []
        self.error = None
        self.cli = b"qualified central cancellation source"
        self.cancel_core = b"qualified central cancellation transaction"
        self.cancel_state = b"qualified parent-bound state transaction"
        self.cancel_workflow = b"qualified central main dispatch workflow"
        self.owner_id = adapter.OWNER_ID

    def get(self, path):
        self.calls.append(path)
        if path == f"repos/{adapter.CENTRAL}":
            return {"id": adapter.CENTRAL_ID, "full_name": adapter.CENTRAL,
                    "owner": {"id": adapter.OWNER_ID}, "default_branch": "main", "private": True}
        if path == "user":
            return {"id": self.owner_id}
        if "/git/ref/" in path:
            branch = path.split("/heads/")[1]
            return {"ref": "refs/heads/" + branch, "object": {"sha": REVISION}}
        if "/actions/runs/" in path:
            result = copy.deepcopy(self.remote)
            result["id"] = int(path.rsplit("/", 1)[1])
            if result["id"] != 123:
                result["path"] = adapter.COORDINATOR_PATH
            return result
        if "/actions/workflows/" in path:
            return {"workflow_runs": self.listing, "total_count": len(self.listing)}
        raise AssertionError(path)

    def file(self, path, revision, limit):
        self.calls.append(("file", path, revision, limit))
        if path == "loop/cli.py":
            return self.cli
        if path == "loop/coordinator.py":
            return self.cancel_core
        if path == "loop/state.py":
            return self.cancel_state
        if path == ".github/workflows/coordinator.yml":
            return self.cancel_workflow
        return adapter.canonical(self.state)

    def dispatch(self, inputs):
        self.posts.append(inputs)
        if self.error:
            raise self.error


def args(command="launch", **kwargs):
    values = {
        "command": command, "backend": "actions", "target": adapter.TEST_REPO + "#1",
        "mode": "shadow", "request_id": REQUEST, "generation": 10, "revision": REVISION,
        "authorize_personal_publication": True, "publication_auth": "fine_grained_pat",
    }
    return SimpleNamespace(**dict(values, **kwargs))


@pytest.mark.parametrize("command", [
    ["launch", adapter.TEST_REPO + "#1", "--mode", "shadow", "--receipt", "receipt"],
    ["status", adapter.TEST_REPO + "#1"],
    ["reconcile", "--receipt", "receipt"],
])
def test_explicit_backend_required(command):
    with pytest.raises(SystemExit):
        adapter.parser().parse_args(command)


@pytest.mark.parametrize("value", [
    "1", "trask/another#1", adapter.TEST_REPO + "#2",
    "https://example.com/trask/copilot-review-loop-test/pull/1",
    "https://github.com/trask/copilot-review-loop-test/pull/1?token=x",
    "https://github.com/trask/copilot-review-loop-test/pull/1/../2",
])
def test_unsupported_targets(value):
    with pytest.raises(adapter.AdapterError):
        adapter.target(value)


def test_default_backend_agent_and_models_unchanged():
    agent = (ROOT / "agents" / "copilot-review-loop.agent.md").read_text()
    original = (ROOT / "scripts" / "copilot_review_loop.py").read_text()
    actions = (ROOT / "agents" / "actions-copilot-review-loop.agent.md").read_text()
    assert "python <helper> agent-task <target>" in agent
    assert 'LOCAL_DECISION_MODEL = "gpt-6-sol"' in original
    assert "Hosted Agent Tasks" in agent
    assert "Never switch between backends or fall back after an error" in actions
    assert "cloud_task" not in (ROOT / "scripts" / "actions_review_loop.py").read_text()


def test_launch_is_one_central_post_and_private_receipt():
    api = FakeAPI()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "receipt.json"
        result = adapter.execute(api, args(receipt=str(path)))
        assert result["dispatch"] == "acknowledged"
        assert result["request_binding"] == "unconfirmed"
        assert result["pipeline_success"] is False
        assert api.posts == [{
            "operation": "launch", "target": "https://github.com/" + adapter.TEST_REPO + "/pull/1",
            "mode": "shadow", "previous_request": "", "previous_generation": "",
            "publication_auth": "disabled",
        }]
        assert json.loads(path.read_bytes())["backend"] == "actions"
        with pytest.raises(adapter.AdapterError, match="already exists"):
            adapter.execute(api, args(receipt=str(path)))
        assert len(api.posts) == 1


def test_uncertain_dispatch_never_repeats_and_reconcile_is_read_only():
    api = FakeAPI()
    api.error = adapter.AdapterError("timeout")
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "receipt.json")
        with pytest.raises(adapter.AdapterError, match="Dispatch uncertain"):
            adapter.execute(api, args(receipt=path))
        assert json.loads(Path(path).read_bytes())["dispatch"] == "uncertain"
        assert adapter.execute(api, args("reconcile", receipt=path))["result"] == "pending"
        with pytest.raises(adapter.AdapterError):
            adapter.execute(api, args(receipt=path))
        assert len(api.posts) == 1


def test_post_acknowledgment_receipt_failure_does_not_retry():
    api = FakeAPI()
    original = adapter.save_receipt
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "receipt.json")
        def write(path, value, *, create=False):
            if create:
                original(path, value, create=True)
            else:
                raise OSError("disk full")
        with mock.patch.object(adapter, "save_receipt", side_effect=write):
            with pytest.raises(adapter.AdapterError, match="never repeat POST"):
                adapter.execute(api, args(receipt=path))
        assert json.loads(Path(path).read_bytes())["dispatch"] == "uncertain"
        assert len(api.posts) == 1


def test_duplicate_reconciliation_cannot_establish_request_or_success():
    api = FakeAPI()
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "receipt.json")
        adapter.execute(api, args(receipt=path))
        api.listing = [
            dict(remote(i, adapter.COORDINATOR_PATH),
                 display_title=f"Review loop launch {i}") for i in (456, 789)
        ]
        result = adapter.execute(api, args("reconcile", receipt=path))
        assert result["result"] == "ambiguous"
        assert result["request_binding"] == "unconfirmed"
        assert result["pipeline_success"] is False
        assert len(api.posts) == 1


def test_run_success_alone_not_a_report():
    api = FakeAPI()
    api.state.update(stage="verify_pending", reason=None, report=None)
    result = adapter.execute(api, args("status"))
    assert result["pipeline_success"] is False
    assert result["central_stage"] == "verify_pending"
    assert result["validation"] is None


def test_missing_report_schema_cannot_expose_a_validation_claim():
    api = FakeAPI()
    api.state["report"] = {"validation": "passed"}
    with pytest.raises(adapter.AdapterError, match="Unbound"):
        adapter.execute(api, args("status"))
    api.state["report"] = {"run_ids": [123], "agent_started": False}
    result = adapter.execute(api, args("status"))
    assert result["validation"] is None
    assert result["pipeline_success"] is False


def test_unqualified_objective_pass_is_not_clean():
    api = FakeAPI()
    api.state["report"]["objective_validation"] = {"status": "missing", "qualified": False}
    result = adapter.execute(api, args("status"))
    assert result["central_stage"] == "blocked"
    assert result["reason"] == "validation_unqualified"
    assert result["pipeline_success"] is False


def test_missing_objective_cannot_claim_general_qualification():
    api = FakeAPI()
    api.state["report"]["objective_validation"] = {"status": "missing", "qualified": True}
    with pytest.raises(adapter.AdapterError, match="qualified"):
        adapter.execute(api, args("status"))


@pytest.mark.parametrize("stage,reason", [
    ("ready", None), ("blocked", "human_gate_COPILOT_GITHUB_TOKEN"),
    ("failed", "worker_failure"), ("cancelled", "durable_user_cancellation"),
    ("exhausted", "elapsed_deadline"), ("waiting_ci", None),
])
def test_pending_and_terminal_results_remain_explicit(stage, reason):
    api = FakeAPI()
    api.state.update(stage=stage, reason=reason, report=None)
    result = adapter.execute(api, args("status"))
    assert result["central_stage"] == stage
    assert result["reason"] == reason
    assert result["pipeline_success"] is False


@pytest.mark.parametrize("container,key,value", [
    ("state", "schema", 2), ("state", "schema", True),
    ("state", "generation", 11), ("state", "stage", "unknown"),
    ("state", "expected_sha", "invalid"),
    ("request", "schema", 2), ("request", "request_id", "d" * 32),
    ("request", "repo", adapter.OTEL), ("request", "pr", 2),
    ("request", "head_repo_id", 210933087), ("request", "workflow_revision", "e" * 40),
    ("request", "source_private", True),
    ("report", "schema", {"id": "github.copilot.agent-task-result", "version": 5}),
    ("report", "request_id", "d" * 32), ("report", "request_digest", "f" * 64),
    ("report", "repo", adapter.OTEL), ("report", "pr", 2),
    ("report", "workflow_revision", "e" * 40),
    ("report", "run_id", 456), ("report", "run_attempt", 2),
    ("run", "attempt", 2),
])
def test_all_identity_mismatches_fail_closed(container, key, value):
    api = FakeAPI()
    subject = api.state if container == "state" else api.state[container]
    subject[key] = value
    with pytest.raises(adapter.AdapterError):
        adapter.execute(api, args("status"))
    assert not api.posts


@pytest.mark.parametrize("key,value", [
    ("path", adapter.COORDINATOR_PATH), ("head_sha", "e" * 40),
    ("run_attempt", 2), ("event", "push"), ("head_branch", "topic"),
    ("display_title", "Copilot shadow " + "d" * 32),
    ("repository", {"id": 1, "full_name": adapter.CENTRAL}),
    ("head_repository", {"id": 1, "full_name": adapter.CENTRAL}),
    ("conclusion", "failure"), ("status", "in_progress"),
])
def test_remote_provenance_required_for_candidate_report(key, value):
    api = FakeAPI()
    api.remote[key] = value
    with pytest.raises(adapter.AdapterError):
        adapter.execute(api, args("status"))


@pytest.mark.parametrize("key,value", [
    ("schema", 2), ("generation", 11), ("request_digest", "0" * 64),
    ("run_id", 456), ("run_attempt", 2), ("candidate_commit", "d" * 40),
    ("candidate_tree", "d" * 40), ("bundle_sha256", "0" * 64), ("qualified", True),
])
def test_objective_receipt_identity_mismatches(key, value):
    api = FakeAPI()
    report = api.state["report"]
    report["candidate"] = {
        "commit": "d" * 40, "tree": "e" * 40, "bundle_sha256": "f" * 64}
    objective = {
        "schema": 1, "generation": 10, "request_digest": report["request_digest"],
        "run_id": 123, "run_attempt": 1, "candidate_commit": "d" * 40,
        "candidate_tree": "e" * 40, "bundle_sha256": "f" * 64,
        "qualified": False, "status": "passed",
    }
    # Use a distinct invalid commit for the commit mismatch case.
    objective[key] = "a" * 40 if key == "candidate_commit" else value
    report["objective_validation"] = objective
    with pytest.raises(adapter.AdapterError):
        adapter.execute(api, args("status"))


def test_objective_pass_with_identity_is_still_unqualified():
    api = FakeAPI()
    report = api.state["report"]
    report["candidate"] = {
        "commit": "d" * 40, "tree": "e" * 40, "bundle_sha256": "f" * 64}
    report["objective_validation"] = {
        "schema": 1, "generation": 10, "request_digest": report["request_digest"],
        "run_id": 123, "run_attempt": 1, "candidate_commit": "d" * 40,
        "candidate_tree": "e" * 40, "bundle_sha256": "f" * 64,
        "qualified": False, "status": "passed",
    }
    result = adapter.execute(api, args("status"))
    assert result["objective_status"] == "passed"
    assert result["general_qualified"] is False
    assert result["pipeline_success"] is False


def test_status_reads_only_one_bounded_checkpoint_and_run():
    api = FakeAPI()
    result = adapter.execute(api, args("status"))
    assert result["worker_run_id"] == 123
    files = [call for call in api.calls if isinstance(call, tuple)]
    assert files == [("file", "pr-1400255214-1.json", REVISION, adapter.MAX_STATE)]
    assert not any("/artifacts" in call or "/logs" in call for call in api.calls
                   if isinstance(call, str))
    assert not api.posts


def test_central_must_remain_private():
    api = FakeAPI()
    original = api.get
    def get(path):
        value = original(path)
        if path == f"repos/{adapter.CENTRAL}":
            value["private"] = False
        return value
    with mock.patch.object(api, "get", side_effect=get):
        with pytest.raises(adapter.AdapterError, match="private"):
            adapter.execute(api, args("status"))
    assert not api.posts


def test_validator_run_attempt_mismatch():
    api = FakeAPI()
    api.state["report"]["validation_run"] = {"id": 456, "attempt": 2}
    with pytest.raises(adapter.AdapterError, match="attempt"):
        adapter.execute(api, args("status"))


def test_verifier_validator_link_is_remotely_bound_before_presentation():
    api = FakeAPI()
    api.state["report"]["validation_run"] = {"id": 456, "attempt": 1}
    result = adapter.execute(api, args("status"))
    assert result["validation_run_id"] == 456
    assert result["validation_run_url"] == f"https://github.com/{adapter.CENTRAL}/actions/runs/456"
    assert f"repos/{adapter.CENTRAL}/actions/runs/456" in api.calls
    assert result["worker_run_attempt"] == 1
    assert result["target_repository_id"] == 1400255214
    assert result["expected_sha"] == "c" * 40


@pytest.mark.parametrize("event", ["workflow_dispatch", "workflow_run", "schedule", "pull_request"])
def test_validation_coordinator_accepts_only_its_real_triggers(event):
    api = FakeAPI()
    api.state["report"]["validation_run"] = {"id": 456, "attempt": 1}
    original = api.get
    def get(path):
        value = original(path)
        if path.endswith("/actions/runs/456"):
            value["event"] = event
        return value
    with mock.patch.object(api, "get", side_effect=get):
        if event == "pull_request":
            with pytest.raises(adapter.AdapterError, match="identity"):
                adapter.execute(api, args("status"))
        else:
            assert adapter.execute(api, args("status"))["validation_run_id"] == 456


@pytest.mark.parametrize("artifacts", [
    [{}] * 101,
    [{"id": 1, "name": "candidate", "size_in_bytes": 64 * 1024 * 1024 + 1, "expired": False}],
    [{"id": 1, "name": "candidate", "size_in_bytes": 1, "expired": "false"}],
])
def test_artifact_metadata_bounds_without_download(artifacts):
    api = FakeAPI()
    api.state["artifacts"] = artifacts
    with pytest.raises(adapter.AdapterError):
        adapter.execute(api, args("status"))
    assert not any("/artifacts" in call for call in api.calls if isinstance(call, str))


def test_active_publication_prior_is_rejected_before_dispatch():
    api = FakeAPI()
    api.state["stage"] = "waiting_review"
    with pytest.raises(adapter.AdapterError):
        adapter.execute(api, args("start-publication", receipt="unused"))
    assert not api.posts


@pytest.mark.parametrize("kwargs", [
    {"target": adapter.OTEL + "#12345"},
    {"authorize_personal_publication": False},
    {"publication_auth": "disabled"},
])
def test_publication_requires_explicit_test_only_authorization(kwargs):
    api = FakeAPI()
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(adapter.AdapterError):
            adapter.execute(api, args("start-publication",
                                      receipt=str(Path(directory) / "receipt.json"), **kwargs))
    assert not api.posts


def test_publication_central_gate_and_owner_no_credential_payload():
    api = FakeAPI()
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "receipt.json")
        api.owner_id = 42
        with pytest.raises(adapter.AdapterError, match="owner"):
            adapter.execute(api, args("start-publication", receipt=path))
        assert not api.posts
        api.owner_id = adapter.OWNER_ID
        result = adapter.execute(api, args("start-publication", receipt=path))
        assert result["pipeline_success"] is False
        assert api.posts[0]["publication_auth"] == "fine_grained_pat"
        assert api.posts[0]["previous_generation"] == "10"
        assert api.posts[0]["previous_request"] == REQUEST
        assert "token" not in json.dumps(api.posts).lower()


def test_cancel_refuses_unqualified_central_handler_before_post():
    api = FakeAPI()
    api.state.update(stage="verify_pending", report=None)
    with tempfile.TemporaryDirectory() as directory:
        with mock.patch.object(adapter, "CANCEL_SOURCE_SHA256", {}):
            with pytest.raises(adapter.AdapterError, match="guard"):
                adapter.execute(api, args("cancel", receipt=str(Path(directory) / "r.json")))
    assert not api.posts


def test_cancel_uses_exact_request_generation_and_guard_source_pin():
    api = FakeAPI()
    api.state.update(stage="verify_pending", report=None)
    pins = {"loop/cli.py": hashlib.sha256(api.cli).hexdigest(),
            "loop/coordinator.py": hashlib.sha256(api.cancel_core).hexdigest(),
            "loop/state.py": hashlib.sha256(api.cancel_state).hexdigest(),
            ".github/workflows/coordinator.yml": hashlib.sha256(api.cancel_workflow).hexdigest()}
    with tempfile.TemporaryDirectory() as directory:
        with mock.patch.object(adapter, "CANCEL_SOURCE_SHA256", pins):
            result = adapter.execute(api, args("cancel", receipt=str(Path(directory) / "r.json")))
        assert result["dispatch"] == "acknowledged"
        assert result["pipeline_success"] is False
        assert api.posts[0]["operation"] == "cancel"
        assert api.posts[0]["previous_request"] == REQUEST
        assert api.posts[0]["previous_generation"] == "10"
    api = FakeAPI()
    api.state.update(stage="verify_pending", report=None)
    with mock.patch.object(adapter, "CANCEL_SOURCE_SHA256", dict(pins, **{"loop/coordinator.py": "0" * 64})):
        with pytest.raises(adapter.AdapterError, match="mismatch"):
            adapter.execute(api, args("cancel", receipt=str(Path(tempfile.gettempdir()) / "unused-receipt.json")))
    assert not api.posts


def test_release_pins_all_qualified_cancellation_boundaries():
    assert set(adapter.CANCEL_SOURCE_SHA256) == {
        "loop/cli.py", "loop/coordinator.py", "loop/state.py", ".github/workflows/coordinator.yml"}
    assert all(len(value) == 64 and all(char in "0123456789abcdef" for char in value)
               for value in adapter.CANCEL_SOURCE_SHA256.values())


def test_cancelled_generation_does_not_accept_retained_old_candidate():
    api = FakeAPI()
    api.state.update(stage="cancelled", generation=11, reason="durable_user_cancellation")
    result = adapter.execute(api, args("status", generation=11))
    assert result["central_stage"] == "cancelled"
    assert result["validation"] is None
    assert result["pipeline_success"] is False


def test_already_cancelled_current_generation_is_read_only_noop():
    api = FakeAPI()
    api.state.update(stage="cancelled", generation=11, reason="durable_user_cancellation")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "receipt.json"
        result = adapter.execute(api, args("cancel", generation=11, receipt=str(path)))
        assert result["operation_result"] == "already_cancelled"
        assert result["dispatch"] == "not_needed"
        assert not path.exists()
    assert not api.posts
    assert not any(call == ("file", "loop/cli.py", REVISION, 128000) for call in api.calls)


@pytest.mark.parametrize("stage", sorted(adapter.TERMINAL - {"cancelled"}))
def test_cancel_rejects_completed_terminal_evidence(stage):
    api = FakeAPI()
    api.state["stage"] = stage
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(adapter.AdapterError, match="terminal"):
            adapter.execute(api, args("cancel", receipt=str(Path(directory) / "r.json")))
    assert not api.posts


@pytest.mark.parametrize("data", [
    b'{"schema":1,"schema":2}', b'{"value":NaN}', b'{"value":Infinity}',
    b"invalid", b"x" * (adapter.MAX_OUTPUT + 1),
], ids=["duplicate-key", "nan", "infinity", "invalid", "oversized"])
def test_json_and_size_limits(data):
    with pytest.raises(adapter.AdapterError):
        adapter.parse_json(data)


def test_oversized_listing_fails_without_dispatch():
    api = FakeAPI()
    api.listing = [remote(i) for i in range(1, 22)]
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(adapter.AdapterError, match="listing"):
            adapter.execute(api, args(receipt=str(Path(directory) / "r.json")))
    assert not api.posts


def test_private_receipts_not_in_checkout():
    with pytest.raises(adapter.AdapterError, match="outside"):
        adapter.receipt_path(str(ROOT / "private-receipt.json"))
    with pytest.raises(adapter.AdapterError, match="absolute"):
        adapter.receipt_path("relative.json")


def test_api_only_posts_to_central_main():
    with mock.patch.object(adapter, "run_gh", return_value=b"") as run:
        adapter.API().dispatch({"operation": "launch"})
    assert run.call_args.args[0] == [
        f"repos/{adapter.CENTRAL}/actions/workflows/coordinator.yml/dispatches",
        "--method", "POST", "--input", "-"]
    assert run.call_args.args[1] == {"ref": "main", "inputs": {"operation": "launch"}}


def test_git_file_size_encoding_and_blob_identity():
    data = b'{"schema":1}'
    value = {"type": "file", "path": "pr-1.json", "encoding": "base64",
             "size": len(data), "content": base64.b64encode(data).decode(),
             "sha": hashlib.sha1(b"blob 12\0" + data).hexdigest()}
    api = adapter.API()
    with mock.patch.object(api, "get", return_value=value):
        assert api.file("pr-1.json", REVISION, 100) == data
        for key, bad in [("sha", "0" * 40), ("size", 1000), ("path", "other"),
                         ("encoding", "none"), ("content", "!!!")]:
            changed = dict(value, **{key: bad})
            with mock.patch.object(api, "get", return_value=changed):
                with pytest.raises(adapter.AdapterError):
                    api.file("pr-1.json", REVISION, 100)


class Process:
    def __init__(self, stdout=b"{}", stderr=b"", returncode=0):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.stdin = io.BytesIO()
        self.returncode = returncode
        self.killed = False
        self.timeout = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def wait(self, timeout):
        assert timeout in {adapter.TIMEOUT, 5}
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("gh", timeout)
        return self.returncode

    def kill(self):
        self.killed = True


def test_dispatch_stdin_is_only_explicit_json_inputs():
    class Input(io.BytesIO):
        def close(self):
            self.written = self.getvalue()
            super().close()
    process = Process(stdout=b"")
    process.stdin = Input()
    body = {"ref": "main", "inputs": {"operation": "launch", "mode": "shadow"}}
    with mock.patch.object(adapter.subprocess, "Popen", return_value=process) as popen:
        assert adapter.run_gh(["dispatches", "--method", "POST", "--input", "-"], body) == b""
    assert process.stdin.written == adapter.canonical(body)
    assert popen.call_args.kwargs["stdin"] == subprocess.PIPE


@pytest.mark.parametrize("windows", [False, True])
def test_subprocess_bounds_no_window_and_auth_redaction(windows):
    process = Process()
    with mock.patch.object(adapter, "IS_WINDOWS", windows), \
         mock.patch.object(adapter.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
         mock.patch.dict(adapter.os.environ, {
             "GH_TOKEN": "ordinary-session-auth", "GH_DEBUG": "api",
             "COPILOT_GITHUB_TOKEN": "inference-secret",
             "REVIEW_LOOP_TEST_PUBLISH_TOKEN": "publisher-secret",
         }), \
         mock.patch.object(adapter.subprocess, "Popen", return_value=process) as popen:
        assert adapter.run_gh(["user"]) == b"{}"
    options = popen.call_args.kwargs
    assert options.get("creationflags", 0) == (0x08000000 if windows else 0)
    assert options["env"]["GH_TOKEN"] == "ordinary-session-auth"
    assert options["env"]["GH_HOST"] == "github.com"
    assert "COPILOT_GITHUB_TOKEN" not in options["env"]
    assert "REVIEW_LOOP_TEST_PUBLISH_TOKEN" not in options["env"]
    assert "GH_DEBUG" not in options["env"]
    assert "ordinary-session-auth" not in repr(popen.call_args.args)


@pytest.mark.parametrize("failure", ["stdout", "stderr", "timeout", "exit"])
def test_subprocess_limits_and_errors_never_echo_secret(failure):
    process = Process(
        stdout=b"x" * (adapter.MAX_OUTPUT + 1) if failure == "stdout" else b"{}",
        stderr=b"x" * (adapter.MAX_OUTPUT + 1) if failure == "stderr" else b"secret-value",
        returncode=1 if failure == "exit" else 0,
    )
    process.timeout = failure == "timeout"
    with mock.patch.object(adapter.subprocess, "Popen", return_value=process):
        with pytest.raises(adapter.AdapterError) as error:
            adapter.run_gh(["user"])
    assert "secret-value" not in str(error.value)
    if failure != "exit":
        assert process.killed
