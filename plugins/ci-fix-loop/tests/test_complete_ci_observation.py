import contextlib
import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

import test_ci_fix_loop as fixtures


MODULE = fixtures.MODULE
PIPELINE_PATH = Path(__file__).parents[2] / "pr-pipeline" / "scripts" / "pr_pipeline.py"
SPEC = importlib.util.spec_from_file_location("complete_ci_pipeline", PIPELINE_PATH)
PIPELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PIPELINE)


class CompleteCiObservationTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagedAgentTaskContractTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.root / "repo"
        self.repo.mkdir()
        self.state_path = self.fixture.root / "state.json"
        self.preflight = copy.deepcopy(self.fixture.preflight)
        self.preflight["repository_root"] = str(self.repo)
        failure = self.preflight["check_snapshot"]["failures"][0]
        self.checks = [{
            **failure, "class": "failed", "workflow_run_id": 1,
        }]
        self.old = {
            "id": 1, "workflow_id": 10, "name": "CI",
            "head_sha": self.fixture.head, "run_attempt": 1,
            "status": "completed", "conclusion": "failure",
        }
        self.live_checks = self.checks
        self.runs = {"1": self.old}
        self.freeze()

    def freeze(self):
        snapshot = self.preflight["check_snapshot"]
        snapshot.update(
            rollup=MODULE.check_rollup_identity(self.checks),
            workflow_runs=copy.deepcopy(self.runs),
        )
        snapshot["rollup_sha256"] = MODULE.sha256_text(
            json.dumps(snapshot["rollup"], separators=(",", ":"), sort_keys=True)
        )
        snapshot["sha256"] = MODULE.check_snapshot_sha256(snapshot)

    def pipeline_status(self, state, *, verify=False):
        payload = MODULE.status_payload(state, self.state_path)
        if verify:
            payload.update(MODULE.verify_ci_clearance_snapshot(state))
        return PIPELINE.common.inspect_stage(
            PIPELINE.STAGE_BY_NAME[PIPELINE.STAGE_CI],
            PIPELINE.build_target("owner", "repo", 7),
            self.fixture.head, self.fixture.base,
            read_status=lambda *_: {
                "ok": True, "installed": True, "state": str(self.state_path),
                "payload": payload,
            },
        )

    def api(self, arguments):
        if arguments == ["api", "user"]:
            return {"login": "viewer"}
        if arguments == ["api", "repos/owner/repo"]:
            return {"permissions": dict.fromkeys(("admin", "maintain", "push", "triage", "pull"), True)}
        self.assertIn("actions/runs?", arguments[-1])
        return [{"workflow_runs": [
            {**run, "event": "pull_request"} for run in self.runs.values()
        ]}]

    def fetch_run(self, pr, run_id):
        return {**self.runs[str(run_id)], "repository": {"full_name": "owner/repo"}}

    def observe(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(MODULE.subprocess, "Popen", side_effect=AssertionError("external execution")))
        stack.enter_context(mock.patch.object(MODULE, "metadata_for", return_value=self.preflight["pr"]))
        stack.enter_context(mock.patch.object(MODULE, "fetch_rollup", side_effect=lambda _: (self.fixture.head, self.live_checks)))
        stack.enter_context(mock.patch.object(MODULE, "fetch_workflow_run", side_effect=self.fetch_run))
        stack.enter_context(mock.patch.object(MODULE, "gh_json", side_effect=self.api))
        return stack

    def flow(self, *, after_task=None, after_import=None, candidate=False):
        result = self.fixture.candidate_result(["5" * 40] if candidate else [])
        if not candidate:
            artifact = self.fixture.candidate_metadata(
                self.fixture.artifact, self.fixture.head, [MODULE.CI_DIAGNOSIS_PATH],
            )
            result["candidate"]["artifact_commit"] = artifact
            result["candidate"]["generated"]["head_sha"] = self.fixture.artifact
            result["generated"]["head_sha"] = self.fixture.artifact
        diagnosis = {"diagnoses": [{
            "check_key": self.checks[0]["key"], "diagnosis": "unrelated",
            "reason": "Only the original check was diagnosed",
            "evidence": ["Matching original failure in the pinned base log"],
        }]}

        def run(command, **kwargs):
            self.assertIn("--result-file", command)
            Path(command[command.index("--result-file") + 1]).write_text(json.dumps(result), encoding="utf-8")
            if after_task is not None:
                self.runs = after_task
            return MODULE.subprocess.CompletedProcess(command, 0, "", "")

        def apply_candidate(*_args, **kwargs):
            if after_import is not None:
                self.runs = after_import
            return bool(kwargs["remote"]["commits"])

        args = MODULE.build_parser().parse_args([
            "agent-task", self.preflight["pr"]["pr_url"],
            "--repo-root", str(self.repo), "--state", str(self.state_path),
        ])
        with (
            self.observe(),
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=self.repo),
            mock.patch.object(MODULE, "resolve_target", return_value={"repo_name": "owner/repo", "number": 7}),
            mock.patch.object(MODULE, "agent_task_preflight", return_value=self.preflight),
            mock.patch.object(MODULE, "discover_cloud_task", return_value=self.fixture.root / "cloud_task.py"),
            mock.patch.object(MODULE, "run", side_effect=run) as dispatch,
            mock.patch.object(MODULE, "local_identity", return_value=self.preflight["identity"]),
            mock.patch.object(
                MODULE,
                "verify_runtime_candidate",
                side_effect=self.fixture.verified_candidate,
            ),
            mock.patch.object(MODULE, "remote_head", return_value=self.fixture.head),
            mock.patch.object(MODULE, "publication_lock", return_value=contextlib.nullcontext()),
            mock.patch.object(MODULE, "fetch_committed_text", return_value=json.dumps(diagnosis)),
            mock.patch.object(MODULE, "apply_verified_candidate_import", side_effect=apply_candidate) as apply,
            mock.patch.object(MODULE, "emit") as emit,
        ):
            MODULE.command_agent_task(args)
            state = MODULE.load_state(self.state_path)
            inspected = self.pipeline_status(state, verify=True)
        return emit.call_args.args[0], state, inspected, dispatch.call_count, apply.call_count

    def test_new_failed_workflow_absent_from_rollup_blocks_warning_and_candidate(self):
        new = {**self.old, "id": 3, "workflow_id": 20, "name": "New unobserved workflow"}
        for candidate in (False, True):
            with self.subTest(candidate=candidate):
                self.state_path = self.fixture.root / f"new-failure-{candidate}.json"
                output, state, inspected, tasks, imports = self.flow(
                    after_task={"1": self.old, "3": new}, candidate=candidate,
                )
                self.assertEqual("ci_changed", output["result"])
                self.assertIsNone(state["outcome"])
                self.assertNotIn("warning_snapshot_sha256", state)
                self.assertFalse(inspected["clear"])
                self.assertEqual(1, tasks)
                self.assertEqual(0, imports)
                self.assertEqual(1, state["iterations"])

    def test_same_head_attempt_or_conclusion_changes_invalidate_diagnosis(self):
        for change in ({"run_attempt": 2}, {"conclusion": "cancelled"}, {"status": "in_progress"}):
            with self.subTest(change=change):
                self.state_path = self.fixture.root / f"changed-{next(iter(change))}.json"
                output, state, inspected, _, imports = self.flow(after_task={"1": {**self.old, **change}})
                self.assertEqual("ci_changed", output["result"])
                self.assertFalse(inspected["clear"])
                self.assertEqual(0, imports)
                self.assertEqual(1, state["iterations"])

    def test_unchanged_complete_diagnosis_clears_only_as_a_warning(self):
        output, state, inspected, tasks, _ = self.flow()
        self.assertEqual("warning", output["result"])
        self.assertTrue(inspected["clear"])
        self.assertEqual(1, len(inspected["ci_warnings"]))
        self.assertFalse(MODULE.status_payload(state, self.state_path)["all_ci_passed"])
        self.assertEqual(1, tasks)

    def test_new_failure_at_final_warning_gate_is_not_added_to_accepted_fingerprint(self):
        new = {**self.old, "id": 3, "workflow_id": 20, "name": "Late failure"}
        output, state, inspected, _, _ = self.flow(after_import={"1": self.old, "3": new})
        self.assertEqual("ci_changed", output["result"])
        self.assertIsNone(state["outcome"])
        self.assertNotIn("warning_snapshot_sha256", state)
        self.assertFalse(inspected["clear"])
        self.assertEqual(1, state["iterations"])

    def test_preflight_freezes_workflows_missing_from_rollup_and_revalidates_them(self):
        self.runs["3"] = {**self.old, "id": 3, "workflow_id": 20, "name": "Companion", "conclusion": "success"}
        with (
            self.observe(),
            mock.patch.object(MODULE, "git", return_value=""),
            mock.patch.object(MODULE, "checkout_pr"),
            mock.patch.object(MODULE, "local_identity", return_value=self.preflight["identity"]),
            mock.patch.object(MODULE, "require_fork_head"),
            mock.patch.object(MODULE, "find_push_remote"),
            mock.patch.object(MODULE, "baseline_conclusions", return_value={}),
            mock.patch.object(MODULE, "fetch_failed_check_log", return_value="failed"),
        ):
            preflight = MODULE.agent_task_preflight(
                self.repo, MODULE.parse_target(self.preflight["pr"]["pr_url"]),
                state_path=self.state_path,
            )
            self.assertEqual({"1", "3"}, set(preflight["check_snapshot"]["workflow_runs"]))
            MODULE.require_live_check_snapshot(preflight)
            self.runs["3"] = {**self.runs["3"], "run_attempt": 2}
            with self.assertRaisesRegex(MODULE.WorkflowError, "snapshot changed"):
                MODULE.require_live_check_snapshot(preflight)

    def test_preflight_rejects_unrepresented_failure_before_log_download_or_dispatch(self):
        self.runs["3"] = {**self.old, "id": 3, "workflow_id": 20, "name": "Unobserved"}
        with (
            self.observe(),
            mock.patch.object(MODULE, "git", return_value=""),
            mock.patch.object(MODULE, "checkout_pr"),
            mock.patch.object(MODULE, "local_identity", return_value=self.preflight["identity"]),
            mock.patch.object(MODULE, "require_fork_head"),
            mock.patch.object(MODULE, "find_push_remote"),
            mock.patch.object(MODULE, "fetch_failed_check_log") as download,
            self.assertRaisesRegex(MODULE.WorkflowError, "unobserved failure"),
        ):
            MODULE.agent_task_preflight(
                self.repo, MODULE.parse_target(self.preflight["pr"]["pr_url"]),
                state_path=self.state_path,
            )
        download.assert_not_called()

    def test_fresh_complete_green_observation_needs_no_hosted_task_or_budget_charge(self):
        self.checks = [{**self.checks[0], "class": "passed", "conclusion": "SUCCESS"}]
        self.old = {**self.old, "conclusion": "success"}
        self.runs = {"1": self.old}
        self.freeze()
        self.preflight["check_snapshot"].update(
            failures=[], decision={"decision": "green", "reason": "checks_passed", "checks": [], "detail": "passed"},
        )
        self.live_checks = [{**self.checks[0], "workflow_run_id": 3}]
        self.runs = {"3": {**self.old, "id": 3}}
        output, state, inspected, tasks, imports = self.flow()
        self.assertEqual("green", output["result"])
        self.assertTrue(inspected["clear"])
        self.assertEqual(0, tasks)
        self.assertEqual(0, imports)
        self.assertEqual(0, state["iterations"])
        self.assertEqual(
            MODULE.ci_warning_snapshot_sha256(self.preflight["pr"], self.live_checks, self.runs),
            state["green_snapshot_sha256"],
        )
