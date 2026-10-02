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
REPO = "octo/widget"
REPO_ID = 4242


def checkpoint():
    request = {
        "schema": 2, "repo": REPO, "repo_id": REPO_ID, "pr": 1, "request_id": REQUEST,
        "head_repo": REPO, "head_repo_id": REPO_ID, "head_ref": "fix-review",
        "target_private": False, "authorized_actor_id": adapter.OWNER_ID,
        "launch_run": {"id": 99, "attempt": 1, "actor_id": adapter.OWNER_ID},
        "frozen_at": 100, "deadline": 7300, "budgets": {
            "max_iterations": 5, "deadline_seconds": 7200},
        "frozen_sha": "c" * 40,
        "workflow_revision": REVISION, "source_private": False, "mode": "shadow",
    }
    return {"schema": 2, "request": request, "generation": 10, "stage": "blocked",
            "iteration": 1,
            "expected_sha": "c" * 40,
            "reason": "validation_unqualified", "artifacts": [], "run": {
                "id": 123, "attempt": 1, "conclusion": "success"},
            "report": {
                "schema": 2, "repo": REPO, "pr": 1,
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
        "actor": {"id": adapter.OWNER_ID}, "triggering_actor": {"id": adapter.OWNER_ID},
    }


def native_fields(report):
    report["validation_claim"] = {"schema": 1, "status": "passed", "commands": [
        {"argv": ["python", "-m", "pytest"], "exit_code": 0}]}
    return {
        "status": "passed", "source_bundle_sha256": None,
        "executor": "awf-0.28.23-no-model",
        "plan_sha256": hashlib.sha256(adapter.canonical([["python", "-m", "pytest"]])).hexdigest(),
        "setup": {"argv": ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                           "checkout", "--quiet", "--detach", "d" * 40],
                  "exit_code": 0, "log_sha256": "1" * 64},
        "commands": [{"argv": ["python", "-m", "pytest"], "exit_code": 0,
                      "log_sha256": "2" * 64}],
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
        self.extra_states = {}
        self.launch_remote = remote(99, adapter.COORDINATOR_PATH)

    def states(self):
        request = self.state["request"]
        if self.state["schema"] == 2:
            key = request.get("repo_id") or hashlib.sha256(
                request["repo"].casefold().encode()).hexdigest()[:32]
            name = f"pr-v2-{key}-{request['pr']}.json"
        else:
            name = f"pr-{request['head_repo_id']}-{request['pr']}.json"
        return dict(self.extra_states, **{name: self.state})

    def entry(self, name, state):
        data = adapter.canonical(state)
        return {"path": name, "type": "blob", "mode": "100644", "size": len(data),
                "sha": hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()}

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
        if "/git/commits/" in path:
            return {"sha": REVISION, "tree": {"sha": REVISION}}
        if "/git/trees/" in path:
            return {"sha": REVISION, "truncated": False,
                    "tree": [self.entry(name, state) for name, state in self.states().items()]}
        if "/actions/runs/" in path:
            if path.endswith("/99"):
                return copy.deepcopy(self.launch_remote)
            result = copy.deepcopy(self.remote)
            result["id"] = int(path.rsplit("/", 1)[1])
            if result["id"] != 123:
                result["path"] = adapter.COORDINATOR_PATH
            return result
        if "/actions/artifacts/" in path:
            source = self.state["source"]
            return {"id": source["artifact_id"], "name": "source-" + self.state["request"]["request_id"],
                    "workflow_run": {"id": source["manifest"]["run_id"], "head_sha": REVISION},
                    "digest": source["artifact_digest"], "expired": False, "size_in_bytes": 500}
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
        return adapter.canonical(self.states()[path])

    def dispatch(self, inputs):
        self.posts.append(inputs)
        if self.error:
            raise self.error


def args(command="launch", **kwargs):
    values = {
        "command": command, "backend": "actions", "target": REPO + "#1",
        "mode": "shadow", "request_id": REQUEST, "generation": 10, "revision": REVISION,
        "authorize_publication": True, "publication_auth": "fine_grained_pat",
    }
    return SimpleNamespace(**dict(values, **kwargs))


@pytest.mark.parametrize("command", [
    ["launch", REPO + "#1", "--mode", "shadow", "--receipt", "receipt"],
    ["status", REPO + "#1"],
    ["reconcile", "--receipt", "receipt"],
])
def test_explicit_backend_required(command):
    with pytest.raises(SystemExit):
        adapter.parser().parse_args(command)


@pytest.mark.parametrize("value", [
    "1", "owner/repo#0", "owner/repo#01", "owner/repo#100000000",
    "owner/../repo#1", "owner/..#1", "-owner/repo#1", "owner-/repo#1",
    "owner/repo#1\n", "owner/repo#1?token=x", "owner/repo#1/../2",
    "owner with space/repo#1", "owner/repo;command#1", None,
    "https://example.com/octo/widget/pull/1",
    "https://github.com/octo/widget/pull/1?token=x",
    "https://github.com/octo/widget/pull/1/../2",
])
def test_invalid_targets(value):
    with pytest.raises(adapter.AdapterError):
        adapter.target(value)


@pytest.mark.parametrize("value,repo,number", [
    ("octo/widget#42", "octo/widget", 42),
    ("https://github.com/org-name/service.js/pull/123", "org-name/service.js", 123),
    ("team/private_repo#7", "team/private_repo", 7),
    ("trask/copilot-review-loop-test#2", "trask/copilot-review-loop-test", 2),
])
def test_generic_targets_have_no_repository_or_language_allowlist(value, repo, number):
    assert adapter.target(value) == (repo, number)


def test_imported_entrypoint_cannot_switch_backend():
    api = FakeAPI()
    with pytest.raises(adapter.AdapterError, match="no fallback"):
        adapter.execute(api, args("status", backend="agent-tasks"))
    assert not api.calls
    assert not api.posts


@pytest.mark.parametrize("source_private,target_private", [(False, False), (True, True), (True, False)])
def test_fork_status_binds_actual_head_without_target_credential_probe(source_private, target_private):
    api = FakeAPI()
    api.state["request"].update(head_repo="contributor/widget", head_repo_id=5151,
                                source_private=source_private, target_private=target_private)
    api.state["report"]["request_digest"] = hashlib.sha256(
        adapter.canonical(api.state["request"])).hexdigest()
    result = adapter.execute(api, args("status"))
    assert result["target_repository_id"] == REPO_ID
    assert result["head_repository_id"] == 5151
    assert result["head_repository"] == "contributor/widget"
    assert result["source_private"] is source_private
    assert result["target_private"] is target_private
    assert result["launch_request_binding"] == "unconfirmed"
    assert all(call == f"repos/{adapter.CENTRAL}" or call.startswith(f"repos/{adapter.CENTRAL}/")
               for call in api.calls
               if isinstance(call, str))
    assert not api.posts


@pytest.mark.parametrize("field,value", [
    ("repo_id", True), ("head_repo_id", 0), ("head_repo", "../widget"),
    ("head_ref", "refs/pull/1/merge.lock"), ("head_ref", "refs/../pull/1"),
    ("head_ref", "--upload-pack=bad"), ("target_private", "false"),
    ("authorized_actor_id", 42), ("head_repo", "contributor/widget"),
])
def test_frozen_base_head_and_actor_mismatches_fail_closed(field, value):
    api = FakeAPI()
    api.state["request"][field] = value
    api.state["report"] = None
    with pytest.raises(adapter.AdapterError):
        adapter.execute(api, args("status"))
    assert not api.posts


def gated_state():
    state = checkpoint()
    request = state["request"]
    for key in ("repo_id", "head_repo", "head_repo_id", "head_ref", "frozen_sha"):
        request[key] = None
    for key in ("source_private", "target_private"):
        del request[key]
    request.update(freeze_status="not_frozen", mode="preview")
    state.update(stage="blocked", reason="human_gate_target_repository_read_access",
                 iteration=0, run=None, report=None, expected_sha=None)
    return state


def test_unfrozen_private_access_gate_retains_real_dispatch_provenance():
    api = FakeAPI()
    api.state = gated_state()
    result = adapter.execute(api, args("status"))
    assert result["central_stage"] == "blocked"
    assert result["reason"] == "human_gate_target_repository_read_access"
    assert result["source_frozen"] is False
    assert result["target_repository_id"] is None
    assert result["source_private"] is None
    assert result["target_private"] is None
    assert result["launch_run_id"] == 99
    assert result["launch_actor_id"] == adapter.OWNER_ID
    assert result["pipeline_success"] is False


def test_frozen_numeric_state_preferred_over_separate_hash_access_gate():
    api = FakeAPI()
    gate = gated_state()
    gate["request"]["request_id"] = "d" * 32
    key = hashlib.sha256(REPO.casefold().encode()).hexdigest()[:32]
    api.extra_states[f"pr-v2-{key}-1.json"] = gate
    result = adapter.execute(api, args("status"))
    assert result["request_id"] == REQUEST
    assert result["source_frozen"] is True


@pytest.mark.parametrize("field,value", [
    ("head_repo_id", 5151), ("frozen_sha", "c" * 40), ("repo_id", REPO_ID),
])
def test_unfrozen_gate_cannot_claim_actual_repository_or_source(field, value):
    api = FakeAPI()
    api.state = gated_state()
    api.state["request"][field] = value
    with pytest.raises(adapter.AdapterError):
        adapter.execute(api, args("status"))
    assert not api.posts


@pytest.mark.parametrize("field,value", [
    ("actor", {"id": 42}), ("triggering_actor", {"id": 42}),
    ("run_attempt", 2), ("event", "schedule"), ("head_branch", "topic"),
    ("path", adapter.WORKER_PATH), ("head_repository", {"id": 1}),
])
def test_launch_dispatch_provenance_requires_owner_main_attempt_one(field, value):
    api = FakeAPI()
    api.launch_remote[field] = value
    with pytest.raises(adapter.AdapterError):
        adapter.execute(api, args("status"))
    assert not api.posts


@pytest.mark.parametrize("bound,mode,request_revision", [
    (True, "shadow", REVISION), (False, "preview", REVISION),
    (False, "shadow", "e" * 40),
])
def test_reconciliation_binds_only_actual_launch_run_target_mode_and_revision(bound, mode, request_revision):
    api = FakeAPI()
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "receipt.json")
        adapter.execute(api, args(receipt=path))
        api.state.update(run=None, report=None, iteration=0)
        api.state["request"].update(mode=mode, workflow_revision=request_revision)
        api.listing = [dict(api.launch_remote, display_title="Review loop launch 99")]
        result = adapter.execute(api, args("reconcile", receipt=path))
    assert result["request_binding"] == ("confirmed" if bound else "unconfirmed")
    assert result["pipeline_success"] is False
    assert result["checkpoint_observation"]["mode"] == mode
    if bound:
        assert result["coordinator_run_id"] == 99
        assert result["request_id"] == REQUEST
    assert len(api.posts) == 1


def test_reconciliation_never_adopts_unrelated_latest_launch_run():
    api = FakeAPI()
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "receipt.json")
        adapter.execute(api, args(receipt=path))
        api.listing = [dict(remote(456, adapter.COORDINATOR_PATH),
                            display_title="Review loop launch 456")]
        result = adapter.execute(api, args("reconcile", receipt=path))
    assert result["request_binding"] == "unconfirmed"
    assert len(api.posts) == 1


def test_legacy_state_is_read_only_and_cannot_start_or_cancel():
    api = FakeAPI()
    api.state["schema"] = api.state["request"]["schema"] = api.state["report"]["schema"] = 1
    api.state["report"]["request_digest"] = hashlib.sha256(
        adapter.canonical(api.state["request"])).hexdigest()
    result = adapter.execute(api, args("status"))
    assert result["legacy_read_only"] is True
    assert result["pipeline_success"] is False
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "receipt.json")
        for operation in ("cancel", "start-publication"):
            with pytest.raises(adapter.AdapterError, match="Legacy"):
                adapter.execute(api, args(operation, receipt=path))
    assert not api.posts


@pytest.mark.parametrize("fault", ["truncated", "files", "file_size", "total_size", "path",
                                 "symlink", "duplicate", "blob", "namespace", "matches"])
def test_bounded_state_discovery_rejects_unsafe_or_ambiguous_trees(fault):
    api = FakeAPI()
    original = api.get
    def get(path):
        value = original(path)
        if "/git/trees/" not in path:
            return value
        entry = value["tree"][0]
        if fault == "truncated":
            value["truncated"] = True
        elif fault == "files":
            value["tree"] = [entry] * 1001
        elif fault == "file_size":
            entry["size"] = adapter.MAX_STATE + 1
        elif fault == "total_size":
            value["tree"] = [dict(entry, path=f"request-{i:032x}.json",
                                   size=adapter.MAX_STATE) for i in range(17)]
        elif fault == "path":
            entry["path"] = "../checkpoint.json"
        elif fault == "symlink":
            entry["mode"] = "120000"
        elif fault == "duplicate":
            value["tree"].append(entry)
        elif fault == "blob":
            entry["sha"] = "0" * 40
        elif fault == "namespace":
            name = f"pr-v2-{REPO_ID + 1}-1.json"
            api.extra_states[name] = copy.deepcopy(api.state)
            value["tree"] = [api.entry(name, api.state)]
        elif fault == "matches":
            value["tree"] = [dict(entry, path=f"pr-v2-{i}-1.json") for i in range(1, 22)]
        return value
    with mock.patch.object(api, "get", side_effect=get):
        with pytest.raises(adapter.AdapterError):
            adapter.execute(api, args("status"))
    assert not api.posts


def test_other_repository_same_pr_cannot_supply_status():
    api = FakeAPI()
    other = copy.deepcopy(api.state)
    other["request"].update(repo="other/project", repo_id=5151, head_repo="other/project",
                            head_repo_id=5151)
    api.extra_states["pr-v2-5151-1.json"] = other
    result = adapter.execute(api, args("status"))
    assert result["target"] == REPO + "#1"
    assert result["target_repository_id"] == REPO_ID


def test_two_frozen_checkpoints_for_same_repository_reject_ambiguity():
    api = FakeAPI()
    other = copy.deepcopy(api.state)
    other["request"].update(repo_id=5151, head_repo_id=5151)
    api.extra_states["pr-v2-5151-1.json"] = other
    with pytest.raises(adapter.AdapterError, match="ambiguous"):
        adapter.execute(api, args("status"))
    assert not api.posts


def test_casefolded_target_uses_bound_repository_identity():
    api = FakeAPI()
    result = adapter.execute(api, args("status", target=REPO.upper() + "#1"))
    assert result["target"] == REPO + "#1"
    assert result["target_repository_id"] == REPO_ID


@pytest.mark.parametrize("field,value", [
    ("max_iterations", True), ("max_iterations", 6), ("deadline_seconds", 0),
])
def test_new_schema_frozen_budget_rejects_unsupported_counts(field, value):
    api = FakeAPI()
    api.state["request"]["budgets"][field] = value
    api.state["report"] = None
    with pytest.raises(adapter.AdapterError, match="budget"):
        adapter.execute(api, args("status"))
    assert not api.posts


def test_default_backend_agent_and_models_unchanged():
    agent = (ROOT / "agents" / "copilot-review-loop.agent.md").read_text()
    original = (ROOT / "scripts" / "copilot_review_loop.py").read_text()
    actions = (ROOT / "agents" / "actions-copilot-review-loop.agent.md").read_text()
    assert "python <helper> agent-task <target>" in agent
    assert 'LOCAL_DECISION_MODEL = "gpt-6-sol"' in original
    assert "Hosted Agent Tasks" in agent
    assert "Never switch between backends or fall back after an error" in actions
    assert "cloud_task" not in (ROOT / "scripts" / "actions_review_loop.py").read_text()


def test_generic_agent_and_documented_mutation_consent_match_helper():
    agent = (ROOT / "agents" / "actions-copilot-review-loop.agent.md").read_text()
    docs = (ROOT / "docs" / "actions-backend.md").read_text()
    helper = (ROOT / "scripts" / "actions_review_loop.py").read_text()
    for text in (agent, docs, helper):
        assert "--authorize-personal-publication" not in text
        assert "open-telemetry/opentelemetry-java-instrumentation" not in text
        assert "Gradle" not in text
        assert "--authorize-publication" in text
    assert "read the target's instructions" in agent
    assert "personal-test-only" in docs
    assert "legacy_read_only" in helper


@pytest.mark.parametrize("operation", ["replace", "repair-publication", "continue-personal-test",
                                       "tick", "reconcile-publication"])
def test_operator_operations_are_not_exposed_by_imported_entrypoint(operation):
    api = FakeAPI()
    with pytest.raises(adapter.AdapterError, match="Unsupported"):
        adapter.execute(api, args(operation))
    assert not api.calls
    assert not api.posts


def test_launch_is_one_central_post_and_private_receipt():
    api = FakeAPI()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "receipt.json"
        result = adapter.execute(api, args(receipt=str(path)))
        assert result["dispatch"] == "acknowledged"
        assert result["request_binding"] == "unconfirmed"
        assert result["pipeline_success"] is False
        assert api.posts == [{
            "operation": "launch", "target": "https://github.com/" + REPO + "/pull/1",
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


@pytest.mark.parametrize("limit", [2, 5])
def test_exhausted_frozen_budget_remains_terminal_and_read_only(limit):
    api = FakeAPI()
    api.state["request"]["budgets"]["max_iterations"] = limit
    api.state["request"]["publication"] = {"max_pipelines": limit}
    api.state["report"]["request_digest"] = hashlib.sha256(
        adapter.canonical(api.state["request"])).hexdigest()
    api.state.update(stage="exhausted", iteration=limit,
                     reason="remaining_findings_pipeline_budget")
    original = copy.deepcopy(api.state)
    result = adapter.execute(api, args("status"))
    assert result["central_stage"] == "exhausted"
    assert result["reason"] == "remaining_findings_pipeline_budget"
    assert result["workflow_revision"] == REVISION
    assert result["terminal"] is True
    assert result["pipeline_success"] is False
    assert api.state == original
    assert not api.posts


def test_frozen_budget_cannot_be_replaced_by_new_default_under_old_report():
    api = FakeAPI()
    api.state["request"]["budgets"]["max_iterations"] = 2
    api.state["report"]["request_digest"] = hashlib.sha256(
        adapter.canonical(api.state["request"])).hexdigest()
    api.state["request"]["budgets"]["max_iterations"] = 5
    with pytest.raises(adapter.AdapterError, match="report identity"):
        adapter.execute(api, args("status"))
    assert not api.posts


@pytest.mark.parametrize("container,key,value", [
    ("state", "schema", 3), ("state", "schema", True),
    ("state", "generation", 11), ("state", "stage", "unknown"),
    ("state", "expected_sha", "invalid"),
    ("request", "schema", 3), ("request", "request_id", "d" * 32),
    ("request", "repo", "another/service"), ("request", "pr", 2),
    ("request", "head_repo_id", REPO_ID + 1), ("request", "workflow_revision", "e" * 40),
    ("request", "source_private", "false"),
    ("report", "schema", {"id": "github.copilot.agent-task-result", "version": 5}),
    ("report", "request_id", "d" * 32), ("report", "request_digest", "f" * 64),
    ("report", "repo", "another/service"), ("report", "pr", 2),
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
    ("schema", 1), ("generation", 11), ("request_digest", "0" * 64),
    ("run_id", 456), ("run_attempt", 2), ("candidate_commit", "d" * 40),
    ("candidate_tree", "d" * 40), ("bundle_sha256", "0" * 64), ("qualified", True),
])
def test_objective_receipt_identity_mismatches(key, value):
    api = FakeAPI()
    report = api.state["report"]
    report["candidate"] = {
        "commit": "d" * 40, "tree": "e" * 40, "bundle_sha256": "f" * 64,
        "parent": "c" * 40}
    objective = {
        "schema": 2, "generation": 10, "request_digest": report["request_digest"],
        "run_id": 123, "run_attempt": 1, "candidate_commit": "d" * 40,
        "candidate_tree": "e" * 40, "bundle_sha256": "f" * 64,
        "qualified": False, "status": "passed",
        **native_fields(report),
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
        "commit": "d" * 40, "tree": "e" * 40, "bundle_sha256": "f" * 64,
        "parent": "c" * 40}
    report["objective_validation"] = {
        "schema": 2, "generation": 10, "request_digest": report["request_digest"],
        "run_id": 123, "run_attempt": 1, "candidate_commit": "d" * 40,
        "candidate_tree": "e" * 40, "bundle_sha256": "f" * 64,
        "qualified": False, "status": "passed",
        **native_fields(report),
    }
    result = adapter.execute(api, args("status"))
    assert result["objective_status"] == "passed"
    assert result["general_qualified"] is False
    assert result["pipeline_success"] is False


def candidate_checkpoint(api):
    request = api.state["request"]
    report = api.state["report"]
    report["candidate"] = {"commit": "d" * 40, "tree": "e" * 40,
                           "parent": request["frozen_sha"], "bundle_sha256": "f" * 64}
    report["objective_validation"] = {
        "schema": 2, "request_digest": report["request_digest"], "generation": 10,
        "run_id": 123, "run_attempt": 1, "candidate_commit": "d" * 40,
        "candidate_tree": "e" * 40, "bundle_sha256": "f" * 64,
        "qualified": False, **native_fields(report),
    }


@pytest.mark.parametrize("fault", [
    "plan", "executor", "source", "setup_missing", "setup_exit", "command_exit",
    "argv", "log_hash", "missing_command", "extra_command", "candidate_parent", "schema",
])
def test_native_receipt_requires_complete_bound_plan_setup_commands_and_log_hashes(fault):
    api = FakeAPI()
    candidate_checkpoint(api)
    objective = api.state["report"]["objective_validation"]
    if fault == "plan":
        objective["plan_sha256"] = "0" * 64
    elif fault == "executor":
        objective["executor"] = "local-shell"
    elif fault == "source":
        objective["source_bundle_sha256"] = "3" * 64
    elif fault == "setup_missing":
        del objective["setup"]
    elif fault == "setup_exit":
        objective["setup"]["exit_code"] = 1
    elif fault == "command_exit":
        objective["commands"][0]["exit_code"] = 1
    elif fault == "argv":
        objective["commands"][0]["argv"] = ["npm", "test"]
    elif fault == "log_hash":
        objective["commands"][0]["log_sha256"] = "invalid"
    elif fault == "missing_command":
        objective["commands"] = []
    elif fault == "extra_command":
        objective["commands"] *= 2
    elif fault == "candidate_parent":
        api.state["report"]["candidate"]["parent"] = "e" * 40
    elif fault == "schema":
        objective["schema"] = 1
    with pytest.raises(adapter.AdapterError):
        adapter.execute(api, args("status"))
    assert not api.posts


@pytest.mark.parametrize("argv", [
    [], ["x"] * 41, ["x" * 501], ["sh", "-c", "echo x\n"],
])
def test_native_plan_bounds_without_running_commands(argv):
    api = FakeAPI()
    candidate_checkpoint(api)
    api.state["report"]["validation_claim"]["commands"][0]["argv"] = argv
    with pytest.raises(adapter.AdapterError):
        adapter.execute(api, args("status"))
    assert not api.posts


def private_candidate_checkpoint(api):
    api.state["request"].update(head_repo="contributor/widget", head_repo_id=5151,
                                source_private=True)
    api.state["report"]["request_digest"] = hashlib.sha256(
        adapter.canonical(api.state["request"])).hexdigest()
    candidate_checkpoint(api)
    report = api.state["report"]
    report["candidate"]["source_bundle_sha256"] = "3" * 64
    report["objective_validation"]["source_bundle_sha256"] = "3" * 64
    api.state["source"] = {"artifact_id": 300, "artifact_digest": "sha256:" + "4" * 64,
                           "manifest": {
        "schema": 2, "repo": REPO, "repo_id": 5151, "pr": 1, "request_id": REQUEST,
        "request_digest": report["request_digest"], "frozen_sha": "c" * 40,
        "tree": "5" * 40, "history_count": 1, "shallow": True,
        "workflow_revision": REVISION, "generation": 10, "run_id": 456,
        "run_attempt": 1, "bundle_sha256": "3" * 64,
    }}


def test_private_native_receipt_binds_actual_head_source_manifest_and_producer():
    api = FakeAPI()
    private_candidate_checkpoint(api)
    result = adapter.execute(api, args("status"))
    assert result["head_repository_id"] == 5151
    assert result["target_repository_id"] == REPO_ID
    assert result["objective_status"] == "passed"
    assert result["pipeline_success"] is False
    assert f"repos/{adapter.CENTRAL}/actions/runs/456" in api.calls
    assert f"repos/{adapter.CENTRAL}/actions/artifacts/300" in api.calls
    assert not any(call.endswith("/zip") or "/logs" in call for call in api.calls
                   if isinstance(call, str))
    assert not api.posts


@pytest.mark.parametrize("field,value", [
    ("repo_id", REPO_ID), ("repo", "contributor/widget"), ("generation", 11),
    ("request_digest", "0" * 64), ("workflow_revision", "e" * 40),
    ("bundle_sha256", "0" * 64), ("run_attempt", 2), ("schema", 1),
])
def test_private_source_receipt_identity_mismatches(field, value):
    api = FakeAPI()
    private_candidate_checkpoint(api)
    api.state["source"]["manifest"][field] = value
    with pytest.raises(adapter.AdapterError):
        adapter.execute(api, args("status"))
    assert not api.posts


def test_private_source_artifact_digest_mismatch_is_not_native_acceptance():
    api = FakeAPI()
    private_candidate_checkpoint(api)
    original = api.get
    def get(path):
        value = original(path)
        if "/actions/artifacts/" in path:
            value["digest"] = "sha256:" + "0" * 64
        return value
    with mock.patch.object(api, "get", side_effect=get):
        with pytest.raises(adapter.AdapterError, match="artifact provenance"):
            adapter.execute(api, args("status"))


def test_verified_envelope_on_stale_target_is_observed_without_accepting_nested_claims():
    api = FakeAPI()
    report = api.state["report"]
    api.state.update(stage="blocked", reason="stale_target", report={
        "schema": 2, "request_id": REQUEST, "request_digest": report["request_digest"],
        "generation": 10, "run_id": 123, "run_attempt": 1,
        "verification": "verified", "result": report,
    })
    result = adapter.execute(api, args("status"))
    assert result["central_stage"] == "blocked"
    assert result["reason"] == "stale_target"
    assert result["verification"] == "verified"
    assert result["validation"] is None
    assert result["pipeline_success"] is False


def test_status_reads_only_one_bounded_checkpoint_and_run():
    api = FakeAPI()
    result = adapter.execute(api, args("status"))
    assert result["worker_run_id"] == 123
    files = [call for call in api.calls if isinstance(call, tuple)]
    assert files == [("file", f"pr-v2-{REPO_ID}-1.json", REVISION, adapter.MAX_STATE)]
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
    assert result["target_repository_id"] == REPO_ID
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


@pytest.mark.parametrize("auth_mode", ["disabled", "fine_grained_pat"])
def test_generic_publish_access_gate_remains_explicit_without_credential_probe(auth_mode):
    api = FakeAPI()
    request = api.state["request"]
    request.update(mode="publish", publication={
        "profile": "generic-v2", "auth_mode": auth_mode,
        "authorized_actor_id": adapter.OWNER_ID, "reply_bot_threads": False, "max_pipelines": 5,
    })
    api.state.update(stage="blocked", reason="human_gate_target_repository_push_and_review_access",
                     iteration=0, run=None, report=None)
    result = adapter.execute(api, args("status"))
    assert result["central_stage"] == "blocked"
    assert result["reason"] == "human_gate_target_repository_push_and_review_access"
    assert result["pipeline_success"] is False
    assert not api.posts


@pytest.mark.parametrize("kwargs", [
    {"authorize_publication": False},
    {"publication_auth": "disabled"},
])
def test_publication_requires_explicit_authorization(kwargs):
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


def test_generic_publication_without_prior_tuple_is_explicit_new_phase_not_legacy_upgrade():
    api = FakeAPI()
    with tempfile.TemporaryDirectory() as directory:
        result = adapter.execute(api, args(
            "start-publication", target="other/service#42", request_id=None, generation=None,
            revision=None, receipt=str(Path(directory) / "receipt.json")))
    assert result["dispatch"] == "acknowledged"
    assert result["request_binding"] == "unconfirmed"
    assert api.posts == [{"operation": "start-publication",
                          "target": "https://github.com/other/service/pull/42", "mode": "publish",
                          "previous_request": "", "previous_generation": "0",
                          "publication_auth": "fine_grained_pat"}]
    assert not any(isinstance(call, tuple) for call in api.calls)


@pytest.mark.parametrize("identity", [
    {"request_id": None}, {"generation": None}, {"revision": None},
])
def test_generic_publication_partial_prior_tuple_cannot_dispatch(identity):
    api = FakeAPI()
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(adapter.AdapterError, match="all three"):
            adapter.execute(api, args("start-publication",
                                      receipt=str(Path(directory) / "receipt.json"), **identity))
    assert not api.posts


def test_publication_cli_requires_generic_authorization_not_old_test_only_flag():
    command = ["start-publication", "owner/repo#42", "--backend", "actions", "--receipt", "receipt",
               "--publication-auth", "fine_grained_pat"]
    with pytest.raises(SystemExit):
        adapter.parser().parse_args(command)
    parsed = adapter.parser().parse_args([*command, "--authorize-publication"])
    assert parsed.authorize_publication is True
    assert parsed.request_id is None
    with pytest.raises(SystemExit):
        adapter.parser().parse_args([*command, "--authorize-personal-publication"])


def test_cancel_refuses_unqualified_central_handler_before_post():
    api = FakeAPI()
    api.state.update(stage="verify_pending", report=None)
    with tempfile.TemporaryDirectory() as directory:
        with mock.patch.object(adapter, "CANCEL_SOURCE_SHA256", {}):
            with pytest.raises(adapter.AdapterError, match="guard"):
                adapter.execute(api, args("cancel", receipt=str(Path(directory) / "r.json")))
    assert not api.posts


@pytest.mark.parametrize("identity", [
    {"request_id": "d" * 32}, {"generation": 11}, {"revision": "e" * 40},
])
def test_cancel_stale_identity_cannot_create_receipt_or_dispatch(identity):
    api = FakeAPI()
    api.state.update(stage="verify_pending", report=None)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "receipt.json"
        with pytest.raises(adapter.AdapterError, match="trusted revision mismatch"):
            adapter.execute(api, args("cancel", receipt=str(path), **identity))
        assert not path.exists()
    assert not api.posts
    assert not any(isinstance(call, tuple) and call[1] in adapter.CANCEL_SOURCE_SHA256
                   for call in api.calls)


@pytest.mark.parametrize("changed_source", [
    "loop/cli.py", "loop/coordinator.py", "loop/state.py",
    ".github/workflows/coordinator.yml",
])
def test_cancel_uses_exact_request_generation_and_guard_source_pin(changed_source):
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
    with mock.patch.object(adapter, "CANCEL_SOURCE_SHA256", dict(pins, **{changed_source: "0" * 64})):
        with pytest.raises(adapter.AdapterError, match="mismatch"):
            adapter.execute(api, args("cancel", receipt=str(Path(tempfile.gettempdir()) / "unused-receipt.json")))
    assert not api.posts


def test_release_pins_all_qualified_cancellation_boundaries():
    assert adapter.CANCEL_SOURCE_SHA256 == {
        "loop/cli.py": "3109418f4e3bef570db6c1e2d7f5c73fc2548fc7fad99017ba780462ec596837",
        "loop/coordinator.py": "1f07ca1c5568458c77fc4092f0b785cd702da9422f0d01a4d692261a9820bfa4",
        "loop/state.py": "c32f9b1bb44a56e4b68272ef7697c686b45cf44191040c8292d834cd641e2a28",
        ".github/workflows/coordinator.yml": "ae8b57066c1bdd40d4a263f650c06a9965ceb3d61ba8347df5faa4f57bc5e205",
    }


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
             "REVIEW_LOOP_SOURCE_READ_TOKEN": "private-source-secret",
         }), \
         mock.patch.object(adapter.subprocess, "Popen", return_value=process) as popen:
        assert adapter.run_gh(["user"]) == b"{}"
    options = popen.call_args.kwargs
    assert options.get("creationflags", 0) == (0x08000000 if windows else 0)
    assert options["env"]["GH_TOKEN"] == "ordinary-session-auth"
    assert options["env"]["GH_HOST"] == "github.com"
    assert "COPILOT_GITHUB_TOKEN" not in options["env"]
    assert "REVIEW_LOOP_TEST_PUBLISH_TOKEN" not in options["env"]
    assert "REVIEW_LOOP_SOURCE_READ_TOKEN" not in options["env"]
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
