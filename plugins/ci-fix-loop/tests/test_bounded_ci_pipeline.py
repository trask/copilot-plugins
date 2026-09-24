import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "ci_fix_loop.py"
SPEC = importlib.util.spec_from_file_location("ci_fix_bounded_test", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SESSION = "01234567-89ab-cdef-0123-456789abcdef"


class BoundedCiPipelineTest(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.target = MODULE.parse_target("owner/repo#7")
        self.path = Path.cwd() / "bounded-ci-state.json"
        self.args = SimpleNamespace(
            target="owner/repo#7", repo_root=str(Path.cwd()), state=str(self.path),
            pipeline_run="a" * 32, pipeline_iteration=1,
            pipeline_max_iterations=2, stability_polls=1,
            bounded_step=True, model="sol", max_iterations=5,
            github_mutation_policy="allow",
        )
        self.output = []
        self.patches = [
            mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": SESSION}),
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=Path.cwd()),
            mock.patch.object(MODULE, "resolve_target", return_value=self.target),
            mock.patch.object(MODULE, "require_outside_repository"),
            mock.patch.object(MODULE, "coordinator_file_state", side_effect=lambda _: copy.deepcopy(self.state) if self.state else {
                "version": MODULE.STATE_VERSION, "iterations": 0, "history": [],
            }),
            mock.patch.object(MODULE, "load_state", side_effect=lambda _: copy.deepcopy(self.state)),
            mock.patch.object(MODULE, "save_state", side_effect=lambda _, state: self.store(state)),
            mock.patch.object(MODULE, "emit", side_effect=self.emit),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def store(self, state):
        self.state = copy.deepcopy(state)

    def emit(self, payload):
        if MODULE._EMIT_CAPTURE_STACK:
            MODULE._EMIT_CAPTURE_STACK[-1].append(payload)
        else:
            self.output.append(payload)

    def test_running_checks_wait_then_final_state_is_validated(self):
        snapshot = {
            "sha256": "snapshot", "head_sha": "head", "base_sha": "base",
            "rollup": [], "workflow_runs": {},
            "decision": {"decision": "waiting", "detail": "checks running"},
        }
        preflight = {"check_snapshot": snapshot}
        with (
            mock.patch.object(MODULE, "agent_task_preflight", return_value=preflight) as observe,
            mock.patch.object(MODULE, "require_live_check_snapshot") as confirm,
            mock.patch.object(MODULE, "command_agent_task") as task,
        ):
            MODULE.command_bounded_pipeline(self.args)
            self.assertEqual("waiting", self.output[-1]["result"])
            self.assertEqual("checks_running", self.output[-1]["reason"])
            task.assert_not_called()
            snapshot["decision"]["decision"] = "green"
            with mock.patch.object(MODULE, "processed_ci_snapshot_ids", return_value=set()):
                task.side_effect = lambda _: MODULE.emit({
                    "result": "green", "state": str(self.path),
                })
                MODULE.command_bounded_pipeline(self.args)
            self.assertEqual("green", self.output[-1]["result"])
            self.assertEqual("green", self.state["bounded_step"]["terminal"]["result"])
            self.assertEqual(
                [False, False, True],
                [call.kwargs.get("collect_failure_logs", True) for call in observe.call_args_list],
            )
            confirm.assert_called_once()

    def test_stability_debounce_waits_across_calls_without_sleeping(self):
        self.args.debounce_seconds = 3600
        self.args.stability_polls = 2
        snapshot = {
            "sha256": "ready", "head_sha": "head", "base_sha": "base",
            "rollup": [], "workflow_runs": {},
            "decision": {"decision": "green", "detail": "green"},
        }
        with (
            mock.patch.object(MODULE, "agent_task_preflight",
                              return_value={"check_snapshot": snapshot}),
            mock.patch.object(MODULE, "command_agent_task") as task,
            mock.patch.object(MODULE.time, "sleep") as sleep,
        ):
            MODULE.command_bounded_pipeline(self.args)
            MODULE.command_bounded_pipeline(self.args)
        self.assertEqual(["waiting", "waiting"], [item["result"] for item in self.output])
        task.assert_not_called()
        sleep.assert_not_called()

    def test_changed_checks_after_log_collection_defer_dispatch(self):
        snapshot = {
            "sha256": "ready", "head_sha": "head", "base_sha": "base",
            "rollup": [], "workflow_runs": {},
            "decision": {"decision": "green", "detail": "green"},
        }
        changed = MODULE.WorkflowError(
            "CI attempt changed", details={"reason": "ci_observation_changed"}
        )
        with (
            mock.patch.object(
                MODULE, "agent_task_preflight",
                return_value={"check_snapshot": snapshot},
            ) as observe,
            mock.patch.object(MODULE, "require_live_check_snapshot", side_effect=changed),
            mock.patch.object(MODULE, "command_agent_task") as task,
        ):
            MODULE.command_bounded_pipeline(self.args)
        self.assertEqual("waiting", self.output[-1]["result"])
        self.assertEqual("checks_running", self.output[-1]["reason"])
        self.assertEqual(
            [False, True],
            [call.kwargs.get("collect_failure_logs", True) for call in observe.call_args_list],
        )
        task.assert_not_called()

    def test_pending_hosted_task_is_observed_without_dispatching_again(self):
        self.state = {
            "version": MODULE.STATE_VERSION, "iterations": 1, "history": [],
            "agent_task": {"status": "bounded_pending", "preflight": {"check_snapshot": {}}},
        }
        with (
            mock.patch.object(MODULE, "command_agent_task",
                              side_effect=lambda _: MODULE.emit({
                                  "result": "waiting", "reason": "hosted_fix",
                                  "state": str(self.path),
                              })) as task,
            mock.patch.object(MODULE, "agent_task_preflight") as preflight,
        ):
            MODULE.command_bounded_pipeline(self.args)
            MODULE.command_bounded_pipeline(self.args)
            self.assertEqual(2, task.call_count)
            self.assertTrue(all(call.args[0]._bounded_resume for call in task.call_args_list))
            preflight.assert_not_called()
            self.assertEqual(["waiting", "waiting"], [item["result"] for item in self.output])

    def test_pipeline_validates_final_result_but_not_waiting(self):
        self.args.github_mutation_policy = "allow"
        with (
            mock.patch.object(MODULE, "command_bounded_pipeline",
                              side_effect=lambda _: MODULE.emit({"result": "waiting"})),
            mock.patch.object(MODULE, "validate_terminal_ci_fix_state") as validate,
        ):
            MODULE.command_pipeline(self.args)
            validate.assert_not_called()
        with (
            mock.patch.object(MODULE, "command_bounded_pipeline",
                              side_effect=lambda _: MODULE.emit({"result": "green"})),
            mock.patch.object(MODULE, "validate_terminal_ci_fix_state") as validate,
        ):
            MODULE.command_pipeline(self.args)
            validate.assert_called_once()

    def test_owner_rejects_different_session_and_run(self):
        snapshot = {"sha256": "x", "head_sha": "head", "base_sha": "base",
                    "rollup": [], "workflow_runs": {},
                    "decision": {"decision": "waiting", "detail": "pending"}}
        with mock.patch.object(MODULE, "agent_task_preflight", return_value={"check_snapshot": snapshot}):
            MODULE.command_bounded_pipeline(self.args)
            self.args.pipeline_run = "b" * 32
            with self.assertRaisesRegex(MODULE.WorkflowError, "another session or run"):
                MODULE.command_bounded_pipeline(self.args)
            self.args.pipeline_run = "a" * 32
            with mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": "fedcba98-7654-3210-fedc-ba9876543210"}):
                with self.assertRaisesRegex(MODULE.WorkflowError, "another session or run"):
                    MODULE.command_bounded_pipeline(self.args)

    def test_later_sweep_reobserves_a_processed_snapshot_and_keeps_receipts(self):
        snapshot = {
            "sha256": "ready", "head_sha": "head", "base_sha": "base",
            "rollup": [], "workflow_runs": {},
            "decision": {"decision": "waiting", "detail": "checks running"},
        }
        preflight = {"pr": {"head_sha": "head"}, "check_snapshot": snapshot}
        with (
            mock.patch.object(MODULE, "agent_task_preflight", return_value=preflight) as observe,
            mock.patch.object(MODULE, "require_live_check_snapshot") as confirm,
            mock.patch.object(MODULE, "command_agent_task",
                              side_effect=lambda _: MODULE.emit({
                                  "result": "green", "state": str(self.path),
                              })) as task,
        ):
            MODULE.command_bounded_pipeline(self.args)
            snapshot["decision"] = {"decision": "green", "detail": "green"}
            self.state["bounded_step"]["terminal"] = {
                "result": "green", "state": str(self.path),
            }
            self.state["agent_task"] = {"status": "completed"}
            self.state["budget_scope"] = "pipeline"
            self.state["coordinator"] = {
                "processed_snapshots": [{
                    "snapshot_sha256": "ready",
                    "stability_sha256": MODULE.ci_stability_sha256(snapshot),
                    "task_id": "first-task",
                }],
                "status": "waiting_for_checks",
                "stability_sha256": MODULE.ci_stability_sha256(snapshot),
                "stable_polls": 9,
            }
            self.args.pipeline_iteration = 2
            MODULE.command_bounded_pipeline(self.args)

        self.assertEqual("green", self.output[-1]["result"])
        self.assertEqual(3, observe.call_count)
        confirm.assert_called_once()
        task.assert_called_once()
        self.assertEqual(1, self.state["bounded_processed_snapshot_baseline"])
        self.assertEqual(1, self.state["coordinator"]["stable_polls"])
        self.assertEqual(set(), MODULE.processed_ci_snapshot_ids(self.state))
        MODULE.record_processed_ci_snapshot(
            self.path, preflight, {"result": "published", "task": {"id": "second-task"}},
        )
        self.assertEqual(
            ["first-task", "second-task"],
            [entry["task_id"] for entry in self.state["coordinator"]["processed_snapshots"]],
        )
        self.assertIn("ready", MODULE.processed_ci_snapshot_ids(self.state))

    def test_later_sweep_keeps_owner_fields_and_requires_completed_work(self):
        snapshot = {
            "sha256": "ready", "head_sha": "head", "base_sha": "base",
            "rollup": [], "workflow_runs": {},
            "decision": {"decision": "waiting", "detail": "checks running"},
        }
        with mock.patch.object(MODULE, "agent_task_preflight",
                               return_value={"check_snapshot": snapshot}) as observe:
            MODULE.command_bounded_pipeline(self.args)
            self.state["bounded_step"]["terminal"] = {"result": "green"}
            self.args.pipeline_iteration = 2
            for field, value in (
                ("model", "terra"),
                ("github_mutation_policy", "source-only"),
                ("max_iterations", 6),
                ("pipeline_max_iterations", 3),
            ):
                with self.subTest(field=field):
                    original = getattr(self.args, field)
                    setattr(self.args, field, value)
                    with self.assertRaisesRegex(MODULE.WorkflowError, "another session or run"):
                        MODULE.command_bounded_pipeline(self.args)
                    setattr(self.args, field, original)
            with mock.patch.object(
                MODULE, "resolve_target",
                return_value=MODULE.parse_target("owner/repo#8"),
            ):
                with self.assertRaisesRegex(MODULE.WorkflowError, "another session or run"):
                    MODULE.command_bounded_pipeline(self.args)
            self.state["agent_task"] = {"status": "bounded_pending"}
            with self.assertRaisesRegex(MODULE.WorkflowError, "another session or run"):
                MODULE.command_bounded_pipeline(self.args)
            self.state["agent_task"] = {"status": "completed"}
            self.state["coordinator"] = {"pending_rerun": {"check": "build"}}
            with self.assertRaisesRegex(MODULE.WorkflowError, "active workflow ownership"):
                MODULE.command_bounded_pipeline(self.args)
            self.state["coordinator"] = {"status": "waiting_for_checks"}
            for task_field, value in (
                ("retry_command", "retry hosted task"),
                ("dispatch_monitor", {"status": "starting"}),
                ("dispatch_monitor", {"status": "running"}),
            ):
                with self.subTest(task_field=task_field, value=value):
                    self.state["agent_task"][task_field] = value
                    with self.assertRaisesRegex(MODULE.WorkflowError, "active workflow ownership"):
                        MODULE.command_bounded_pipeline(self.args)
                    self.state["agent_task"].pop(task_field)
            self.state["pending_stack_push"] = {"head": "head"}
            with self.assertRaisesRegex(MODULE.WorkflowError, "another session or run"):
                MODULE.command_bounded_pipeline(self.args)
            self.state.pop("pending_stack_push")
            self.args.pipeline_iteration = 3
            with self.assertRaisesRegex(MODULE.WorkflowError, "valid sweep position"):
                MODULE.command_bounded_pipeline(self.args)
        observe.assert_called_once()

    def test_helper_observes_repeated_pending_without_a_deadline(self):
        calls = []
        responses = iter(["pending", "pending", "completed"])

        def invoke(*args, **kwargs):
            calls.append(kwargs)
            status = next(responses)
            return subprocess.CompletedProcess(
                args[0], 0, json.dumps({
                    "status": status, "task": {"id": "task"},
                    "schema": MODULE.CANDIDATE_AGENT_TASK_RESULT_SCHEMA,
                    "pipeline": {
                        "run_id": "a" * 32, "session_id": SESSION,
                        "request_id": "request",
                    },
                    "candidate": None, "completion": None,
                }), "",
            )

        with (
            mock.patch.object(MODULE.subprocess, "run", side_effect=invoke),
            mock.patch.object(MODULE, "windows_no_window_options",
                              return_value={"creationflags": 0x08000000}),
        ):
            for status in ("pending", "pending", "completed"):
                self.assertEqual(
                    status, MODULE.run_bounded_cloud_helper(
                        ["python", "helper", "--pipeline-run", "a" * 32],
                        Path.cwd(), self.path
                    )["status"],
                )
        self.assertTrue(all("timeout" not in call for call in calls))
        self.assertTrue(all(call["creationflags"] == 0x08000000 for call in calls))

    def test_helper_rejects_pending_result_for_another_run(self):
        payload = {
            "schema": MODULE.CANDIDATE_AGENT_TASK_RESULT_SCHEMA,
            "status": "pending", "task": {"id": "task"},
            "pipeline": {
                "run_id": "b" * 32, "session_id": SESSION,
                "request_id": "request",
            },
            "candidate": None, "completion": None,
        }
        with mock.patch.object(
            MODULE.subprocess, "run",
            return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(payload), "",
            ),
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "matching dispatch identity"):
                MODULE.run_bounded_cloud_helper(
                    ["python", "helper", "--pipeline-run", "a" * 32],
                    Path.cwd(), self.path,
                )

    def test_managed_helper_requires_sealed_child_observation(self):
        payload = {
            "schema": MODULE.CANDIDATE_AGENT_TASK_RESULT_SCHEMA,
            "status": "pending", "task": {"id": "task"},
            "pipeline": {
                "run_id": "a" * 32, "session_id": SESSION,
                "request_id": "request",
            },
            "candidate": None, "completion": None,
        }
        execution = SimpleNamespace(children=[])
        options = []

        def run(command, **kwargs):
            options.append(kwargs)
            execution.children.append(SimpleNamespace(terminal_result={
                "exit_code": 0, "local_status": "finished",
                "workflow_result": payload,
            }))
            return subprocess.CompletedProcess(command, 0, "not-json", "")

        execution.run = run
        with mock.patch.object(MODULE, "_EXECUTION", execution):
            self.assertEqual("pending", MODULE.run_bounded_cloud_helper(
                ["python", "helper", "--pipeline-run", "a" * 32],
                Path.cwd(), self.path,
            )["status"])
        self.assertIs(options[0]["require_execution"], True)
        self.assertNotIn("timeout", options[0])

    def test_managed_final_observation_reads_result_after_sealed_child(self):
        execution = SimpleNamespace(children=[])

        def run(command, **kwargs):
            execution.children.append(SimpleNamespace(terminal_result={
                "exit_code": 0, "local_status": "finished",
                "workflow_result": {"status": "success"},
            }))
            return subprocess.CompletedProcess(command, 0, "", "")

        execution.run = run
        with (
            mock.patch.object(MODULE, "_EXECUTION", execution),
            mock.patch.object(MODULE.Path, "is_file", return_value=True),
            mock.patch.object(MODULE.Path, "read_text", return_value='{"status":"success"}'),
        ):
            self.assertEqual("success", MODULE.run_bounded_cloud_helper(
                ["python", "helper", "--pipeline-run", "a" * 32],
                Path.cwd(), self.path,
            )["status"])

    def test_managed_final_observation_requires_matching_sealed_result(self):
        execution = SimpleNamespace(children=[])
        current = {"result": None}

        def run(command, **kwargs):
            execution.children.append(SimpleNamespace(terminal_result={
                "exit_code": 0, "local_status": "finished",
                "workflow_result": current["result"],
            }))
            return subprocess.CompletedProcess(command, 0, "", "")

        execution.run = run
        with (
            mock.patch.object(MODULE, "_EXECUTION", execution),
            mock.patch.object(MODULE.Path, "is_file", return_value=True),
            mock.patch.object(MODULE.Path, "read_text", return_value='{"status":"success"}'),
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "no sealed execution result"):
                MODULE.run_bounded_cloud_helper(
                    ["python", "helper", "--pipeline-run", "a" * 32],
                    Path.cwd(), self.path,
                )
            execution.children.clear()
            current["result"] = {"status": "error"}
            with self.assertRaisesRegex(MODULE.WorkflowError, "differs from sealed child"):
                MODULE.run_bounded_cloud_helper(
                    ["python", "helper", "--pipeline-run", "a" * 32],
                    Path.cwd(), self.path,
                )


if __name__ == "__main__":
    unittest.main()
