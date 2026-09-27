import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "ci_fix_loop.py"
SPEC = importlib.util.spec_from_file_location("ci_fix_bounded_test", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SESSION = "01234567-89ab-cdef-0123-456789abcdef"


class CIIterationOutcomeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.json"
        self.preflight = {
            "pr": {"head_sha": "head"},
            "check_snapshot": {
                "sha256": "snapshot", "head_sha": "head", "base_sha": "base",
                "rollup": [], "workflow_runs": {},
                "decision": {"decision": "green"},
            },
        }

    def reset_state(self):
        MODULE.save_state(self.path, {
            "version": MODULE.STATE_VERSION, "history": [], "iterations": 0,
            "bounded_step": {"owner": {}},
        })

    def test_both_coordinators_share_receipt_and_terminal_decisions(self):
        for bounded in (False, True):
            for outcome in (
                "waiting", "source_changed", "ci_changed", "published",
                "snapshot_already_processed", "green", "warning",
            ):
                with self.subTest(bounded=bounded, outcome=outcome):
                    self.reset_state()
                    result = {"result": outcome, "task": {"id": "task"}}
                    actual = MODULE.advance_ci_iteration(
                        self.path, self.preflight, result,
                        bounded=bounded, stack_state=True,
                    )
                    if bounded:
                        expected = (
                            result if outcome in {"waiting", "green", "warning"}
                            else {"result": "waiting", "state": str(self.path),
                                  "reason": "checks_running"}
                        )
                    else:
                        expected = (
                            result if outcome in {"published", "green", "warning"}
                            else None
                        )
                    self.assertEqual(expected, actual)
                    state = MODULE.load_state(self.path)
                    receipts = state.get("coordinator", {}).get("processed_snapshots", [])
                    self.assertEqual(
                        [] if outcome == "waiting" else ["task"],
                        [entry["task_id"] for entry in receipts],
                    )
                    self.assertEqual(
                        result if bounded and outcome in {"green", "warning"} else None,
                        state["bounded_step"].get("terminal"),
                    )

    def test_standalone_publication_without_a_stack_reobserves_ci(self):
        self.reset_state()
        result = {"result": "published", "task": {"id": "task"}}
        self.assertIsNone(MODULE.advance_ci_iteration(
            self.path, self.preflight, result, bounded=False,
        ))
        self.assertEqual(
            ["task"],
            [
                entry["task_id"]
                for entry in MODULE.load_state(self.path)["coordinator"]["processed_snapshots"]
            ],
        )


class CIStabilityGateTest(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        self.snapshot = {
            "sha256": "snapshot", "head_sha": "head", "base_sha": "base",
            "rollup": [], "workflow_runs": {},
            "decision": {"decision": "green", "detail": "passed"},
        }
        self.preflight = {"check_snapshot": self.snapshot}

    def test_requires_matching_polls_and_debounce_before_confirmation(self):
        gate = MODULE.CIStabilityGate()
        self.assertEqual(
            "stabilizing",
            gate.observe(self.preflight, processed=set(), now=self.now),
        )
        self.assertFalse(gate.ready(polls=2, debounce_seconds=10, now=self.now))
        gate.observe(self.preflight, processed=set(), now=self.now + dt.timedelta(seconds=2))
        self.assertFalse(gate.ready(
            polls=2, debounce_seconds=10, now=self.now + dt.timedelta(seconds=9),
        ))
        self.assertTrue(gate.ready(
            polls=2, debounce_seconds=10, now=self.now + dt.timedelta(seconds=10),
        ))
        changed = copy.deepcopy(self.preflight)
        changed["check_snapshot"]["workflow_runs"] = {"42": {"run_attempt": 2}}
        with mock.patch.object(MODULE, "require_live_check_snapshot") as verify:
            self.assertFalse(gate.confirm(changed))
            verify.assert_not_called()
            self.assertTrue(gate.confirm(self.preflight))
            verify.assert_called_once_with(self.preflight)
        gate.observe(changed, processed=set(), now=self.now + dt.timedelta(seconds=11))
        self.assertEqual(1, gate.polls)
        self.assertFalse(gate.ready(
            polls=2, debounce_seconds=0, now=self.now + dt.timedelta(seconds=11),
        ))

    def test_processed_failure_blocks_both_modes_but_terminal_can_be_reobserved(self):
        gate = MODULE.CIStabilityGate()
        identity = MODULE.ci_stability_sha256(self.snapshot)
        self.assertEqual(
            "waiting_for_change",
            gate.observe(self.preflight, processed={identity}, now=self.now),
        )
        self.assertFalse(gate.ready(polls=1, debounce_seconds=0, now=self.now))
        self.assertEqual(
            "stabilizing",
            gate.observe(
                self.preflight, processed={identity}, now=self.now,
                allow_processed_terminal=True,
            ),
        )
        self.snapshot["decision"]["decision"] = "failures"
        self.assertEqual(
            "waiting_for_change",
            gate.observe(
                self.preflight,
                processed={MODULE.ci_stability_sha256(self.snapshot)},
                now=self.now, allow_processed_terminal=True,
            ),
        )

    def test_gate_owns_logs_across_observations_and_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            old_directory = Path(directory) / "state--ci-fix-logs--old"
            old_directory.mkdir()
            old_log = old_directory / "001.log"
            old_log.write_text("old", encoding="utf-8")
            gate = MODULE.CIStabilityGate(
                state_path=state_path, active_log_paths=[old_log],
            )
            gate.observe(self.preflight, processed=set(), now=self.now)
            self.assertFalse(old_directory.exists())

            new_directory = Path(directory) / "state--ci-fix-logs--new"
            new_directory.mkdir()
            new_log = new_directory / "001.log"
            new_log.write_text("new", encoding="utf-8")
            complete = copy.deepcopy(self.preflight)
            complete["check_snapshot"]["failures"] = [{"log_path": str(new_log)}]
            with mock.patch.object(MODULE, "require_live_check_snapshot") as verify:
                self.assertEqual(
                    (complete, True),
                    gate.collect_confirmed(lambda: complete),
                )
                verify.assert_called_once_with(complete)
            self.assertEqual({new_log}, gate.active_log_paths)
            self.assertTrue(new_log.is_file())
            gate.discard_logs()
            self.assertFalse(new_directory.exists())
            self.assertEqual(set(), gate.active_log_paths)

    def test_gate_discards_downloaded_logs_when_confirmation_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = MODULE.CIStabilityGate(state_path=Path(directory) / "state.json")
            gate.observe(self.preflight, processed=set(), now=self.now)
            for change in ("different_attempt", "live_race"):
                with self.subTest(change=change):
                    log_directory = Path(directory) / f"state--ci-fix-logs--{change}"
                    log_directory.mkdir()
                    log = log_directory / "001.log"
                    log.write_text("failure", encoding="utf-8")
                    complete = copy.deepcopy(self.preflight)
                    complete["check_snapshot"]["failures"] = [{"log_path": str(log)}]
                    if change == "different_attempt":
                        complete["check_snapshot"]["workflow_runs"] = {"42": {"run_attempt": 2}}
                        self.assertEqual(
                            (complete, False), gate.collect_confirmed(lambda: complete),
                        )
                    else:
                        error = MODULE.WorkflowError(
                            "snapshot changed", details={"reason": "ci_observation_changed"},
                        )
                        with (
                            mock.patch.object(
                                MODULE, "require_live_check_snapshot", side_effect=error,
                            ),
                            self.assertRaises(MODULE.WorkflowError),
                        ):
                            gate.collect_confirmed(lambda: complete)
                    self.assertFalse(log_directory.exists())
                    self.assertEqual(set(), gate.active_log_paths)


class CIRerunProgressTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.json"
        self.preflight = {
            "pr": {"head_sha": "head"},
            "check_snapshot": {
                "sha256": "snapshot", "head_sha": "head", "base_sha": "base",
                "rollup": [], "workflow_runs": {},
                "decision": {"decision": "failures"},
            },
        }
        self.result = {
            "result": "rerun", "task": {"id": "task"},
            "action_checks": ["check:a", "check:b"],
        }
        MODULE.save_state(self.path, {
            "version": MODULE.STATE_VERSION, "history": [], "iterations": 0,
            "bounded_step": {"owner": {}},
        })

    def test_iteration_routes_rerun_through_the_shared_progress(self):
        requested = []

        def rerun(args):
            requested.append(args.check)
            MODULE.emit({"result": "rerun_requested"})

        self.result["action_checks"] = ["check:a"]
        with mock.patch.object(MODULE, "command_rerun", side_effect=rerun):
            bounded = MODULE.advance_ci_iteration(
                self.path, self.preflight, self.result, bounded=True,
            )
        self.assertEqual(
            {"result": "waiting", "state": str(self.path), "reason": "rerun"},
            bounded,
        )
        self.assertEqual(["check:a"], requested)
        self.assertEqual(
            [], MODULE.load_state(self.path)["coordinator"].get("processed_snapshots", []),
        )
        with mock.patch.object(MODULE, "command_rerun") as duplicate:
            finished = MODULE.advance_ci_iteration(
                self.path, self.preflight, self.result, bounded=True,
            )
        duplicate.assert_not_called()
        self.assertEqual("checks_running", finished["reason"])
        self.assertEqual(
            1, len(MODULE.load_state(self.path)["coordinator"]["processed_snapshots"]),
        )

        MODULE.save_state(self.path, {
            "version": MODULE.STATE_VERSION, "history": [], "iterations": 0,
            "bounded_step": {"owner": {}},
        })
        with mock.patch.object(MODULE, "command_rerun", side_effect=rerun):
            self.assertIsNone(MODULE.advance_ci_iteration(
                self.path, self.preflight, self.result, bounded=False,
            ))
        self.assertEqual(["check:a", "check:a"], requested)
        self.assertEqual(
            1, len(MODULE.load_state(self.path)["coordinator"]["processed_snapshots"]),
        )

    def test_bounded_rerun_resumes_each_check_without_repeating_requests(self):
        requested = []

        def rerun(args):
            requested.append(args.check)
            MODULE.emit({"result": "rerun_requested"})

        with mock.patch.object(MODULE, "command_rerun", side_effect=rerun):
            first = MODULE.advance_ci_rerun(
                self.path, self.preflight, self.result, bounded=True,
            )
            state = MODULE.load_state(self.path)
            self.assertEqual(["check:a"], requested)
            self.assertEqual(["check:a"], state["coordinator"]["pending_rerun"]["completed_checks"])
            self.assertEqual(self.result, state["bounded_step"]["pending_rerun"]["result"])
            self.assertNotIn("processed_snapshots", state["coordinator"])

            second = MODULE.advance_ci_rerun(
                self.path, self.preflight, self.result, bounded=True,
            )
            final = MODULE.advance_ci_rerun(
                self.path, self.preflight, self.result, bounded=True,
            )
        self.assertEqual(["check:a", "check:b"], requested)
        self.assertEqual(["rerun", "rerun", "checks_running"], [
            first["reason"], second["reason"], final["reason"],
        ])
        state = MODULE.load_state(self.path)
        self.assertNotIn("pending_rerun", state["bounded_step"])
        self.assertNotIn("pending_rerun", state["coordinator"])
        self.assertEqual("task", state["coordinator"]["processed_snapshots"][0]["task_id"])

    def test_standalone_rerun_completes_all_checks_in_one_call(self):
        requested = []

        def rerun(args):
            requested.append(args.check)
            MODULE.emit({"result": "rerun_requested"})

        with mock.patch.object(MODULE, "command_rerun", side_effect=rerun):
            self.assertIsNone(MODULE.advance_ci_rerun(
                self.path, self.preflight, self.result, bounded=False,
            ))
        self.assertEqual(["check:a", "check:b"], requested)
        state = MODULE.load_state(self.path)
        self.assertNotIn("pending_rerun", state["coordinator"])
        self.assertEqual(1, len(state["coordinator"]["processed_snapshots"]))

    def test_bounded_dispatcher_checkpoint_precedes_retry(self):
        self.result["attestation"] = "dispatcher_candidate"
        observed = []

        def retry(_path, _preflight, keys):
            state = MODULE.load_state(self.path)
            observed.append((
                keys, state["coordinator"]["pending_rerun"]["completed_checks"],
                state["bounded_step"]["pending_rerun"]["result"],
            ))
            return "rerun_requested"

        with mock.patch.object(MODULE, "retry_diagnosed_ci", side_effect=retry):
            first = MODULE.advance_ci_rerun(
                self.path, self.preflight, self.result, bounded=True,
            )
            second = MODULE.advance_ci_rerun(
                self.path, self.preflight, self.result, bounded=True,
            )
            final = MODULE.advance_ci_rerun(
                self.path, self.preflight, self.result, bounded=True,
            )
        self.assertEqual(["rerun", "rerun", "checks_running"], [
            first["reason"], second["reason"], final["reason"],
        ])
        self.assertEqual(
            [(["check:a"], [], self.result), (["check:b"], ["check:a"], self.result)],
            observed,
        )

    def test_unsupported_rerun_returns_terminal_and_closes_bounded_checkpoint(self):
        def reject(_args):
            MODULE.emit({"result": "no_rerun_support"})

        with mock.patch.object(MODULE, "command_rerun", side_effect=reject):
            terminal = MODULE.advance_ci_rerun(
                self.path, self.preflight, self.result, bounded=True,
            )
        state = MODULE.load_state(self.path)
        self.assertEqual("no_rerun_support", terminal["result"])
        self.assertEqual(terminal, state["bounded_step"]["terminal"])
        self.assertNotIn("pending_rerun", state["bounded_step"])
        self.assertEqual(1, len(state["coordinator"]["processed_snapshots"]))


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

    def test_bounded_step_restores_checkpoint_and_replaces_owned_logs(self):
        self.args.stability_polls = 2
        snapshot = {
            "sha256": "ready", "head_sha": "head", "base_sha": "base",
            "rollup": [], "workflow_runs": {},
            "decision": {"decision": "failures", "detail": "failed"},
        }
        preflight = {"check_snapshot": snapshot}
        with tempfile.TemporaryDirectory() as directory:
            self.path = Path(directory) / "state.json"
            self.args.state = str(self.path)
            old_directory = self.path.with_name("state--ci-fix-logs--old")
            old_directory.mkdir()
            old_log = old_directory / "001.log"
            old_log.write_text("old", encoding="utf-8")
            new_directory = self.path.with_name("state--ci-fix-logs--new")
            new_directory.mkdir()
            new_log = new_directory / "001.log"
            new_log.write_text("new", encoding="utf-8")
            complete = copy.deepcopy(preflight)
            complete["check_snapshot"]["failures"] = [{"log_path": str(new_log)}]
            with (
                mock.patch.object(
                    MODULE, "agent_task_preflight",
                    side_effect=[preflight, preflight, complete],
                ) as observe,
                mock.patch.object(MODULE, "require_live_check_snapshot") as confirm,
                mock.patch.object(
                    MODULE, "command_agent_task",
                    side_effect=lambda _: MODULE.emit({
                        "result": "warning", "state": str(self.path),
                    }),
                ) as task,
            ):
                MODULE.command_bounded_pipeline(self.args)
                first_since = self.state["bounded_step"]["stable_since"]
                self.assertEqual(1, self.state["coordinator"]["stable_polls"])
                self.state["bounded_step"]["active_log_paths"] = [str(old_log)]
                MODULE.command_bounded_pipeline(self.args)

            self.assertEqual(3, observe.call_count)
            confirm.assert_called_once_with(complete)
            task.assert_called_once()
            self.assertEqual("warning", self.output[-1]["result"])
            self.assertEqual(2, self.state["coordinator"]["stable_polls"])
            self.assertEqual(first_since, self.state["bounded_step"]["stable_since"])
            self.assertEqual([str(new_log)], self.state["bounded_step"]["active_log_paths"])
            self.assertFalse(old_directory.exists())
            self.assertTrue(new_log.is_file())

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

    def test_bounded_step_resumes_rerun_without_reinvoking_hosted_task(self):
        snapshot = {
            "sha256": "snapshot", "head_sha": "head", "base_sha": "base",
            "rollup": [], "workflow_runs": {},
            "decision": {"decision": "failures", "detail": "failed"},
        }
        preflight = {"pr": {"head_sha": "head"}, "check_snapshot": snapshot}
        requested = []

        def hosted(_args):
            MODULE.emit({
                "result": "rerun", "task": {"id": "task"},
                "action_checks": ["check:a", "check:b"],
            })

        def rerun(args):
            requested.append(args.check)
            MODULE.emit({"result": "rerun_requested"})

        with (
            mock.patch.object(MODULE, "agent_task_preflight", return_value=preflight),
            mock.patch.object(MODULE, "require_live_check_snapshot"),
            mock.patch.object(MODULE, "command_agent_task", side_effect=hosted) as task,
            mock.patch.object(MODULE, "command_rerun", side_effect=rerun),
        ):
            for _ in range(3):
                MODULE.command_bounded_pipeline(self.args)
        self.assertEqual(["check:a", "check:b"], requested)
        self.assertEqual(1, task.call_count)
        self.assertEqual(["rerun", "rerun", "checks_running"], [
            item["reason"] for item in self.output
        ])
        self.assertEqual(1, len(self.state["coordinator"]["processed_snapshots"]))
        self.assertNotIn("pending_rerun", self.state["bounded_step"])

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
