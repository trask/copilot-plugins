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


class HostedRepairAttemptTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.path = self.root / "state.json"
        self.helper = self.root / "helper.py"
        self.helper.write_text("helper", encoding="utf-8")
        self.preflight = {
            "pr": {"pr_url": "https://github.com/owner/repo/pull/7"},
            "check_snapshot": {"failures": []},
        }
        self.args = SimpleNamespace(model="sol", bounded_step=True)

    def attempt(self, *, resume=False):
        return MODULE.HostedRepairAttempt(
            self.args, self.repo, self.path, self.preflight, "gpt-6-sol",
            bounded_resume=resume,
        )

    def test_pending_attempt_resumes_and_attests_result_through_one_boundary(self):
        state = {"version": MODULE.STATE_VERSION, "history": [], "outcome": "warning"}
        first = self.attempt()
        first.prepare(state)
        result = {
            "status": "success",
            "task": {"id": "task"},
            "candidate": {"task": {"session_id": "session"}},
            "generated": {},
            "report": {},
        }

        def observe(_command, _repo, result_path):
            result_path.write_text(json.dumps(result), encoding="utf-8")
            return {"status": "completed"}

        responses = iter((lambda *_: {"status": "pending"}, observe))
        with (
            mock.patch.object(MODULE, "discover_cloud_task", return_value=self.helper),
            mock.patch.object(MODULE, "load_candidate_runtime"),
            mock.patch.object(MODULE, "local_ci_briefing", return_value="briefing"),
            mock.patch.object(MODULE, "require_live_check_snapshot"),
            mock.patch.object(MODULE, "bounded_worker_prompt", return_value=("prompt", "evidence")),
            mock.patch.object(
                MODULE, "REQUIRED_CLOUD_TASK_SHA256", MODULE.sha256_file(self.helper)
            ),
            mock.patch.object(
                MODULE, "run_bounded_cloud_helper",
                side_effect=lambda *args: next(responses)(*args),
            ) as dispatch,
            mock.patch.object(MODULE, "load_agent_task_result", return_value=result),
            mock.patch.object(
                MODULE, "verify_runtime_candidate", return_value={"task_id": "task"}
            ) as verify,
        ):
            self.assertFalse(first.dispatch())
            pending = MODULE.load_state(self.path)
            self.assertEqual("bounded_pending", pending["agent_task"]["status"])
            self.assertIsNone(pending["outcome"])
            pending["agent_task"]["dispatch_identity"] = {
                "task_id": "task", "session_id": "session",
            }
            MODULE.save_state(self.path, pending)
            resumed = self.attempt(resume=True)
            resumed.prepare(MODULE.load_state(self.path))
            self.assertTrue(resumed.dispatch())
            self.assertEqual(
                MODULE.HostedRepairResult(
                    {"task_id": "task"}, MODULE.sha256_file(resumed.result_path),
                ),
                resumed.verify_result(),
            )
            self.assertEqual(2, dispatch.call_count)
            self.assertIn("--pipeline-observe", dispatch.call_args.args[0])
            verify.assert_called_once()

    def test_resume_rejects_changed_dispatch_identity_before_candidate_validation(self):
        state = {"version": MODULE.STATE_VERSION, "history": []}
        first = self.attempt()
        first.prepare(state)
        saved = MODULE.load_state(self.path)
        saved["agent_task"].update({
            "status": "bounded_pending",
            "helper": str(self.helper),
            "prompt_sha256": MODULE.sha256_text("prompt"),
            "dispatch_identity": {"task_id": "original", "session_id": "session"},
        })
        MODULE.save_state(self.path, saved)
        first.prompt_path.write_text("prompt", encoding="utf-8")
        result = {
            "status": "success",
            "task": {"id": "different"},
            "candidate": {"task": {"session_id": "session"}},
        }
        first.result_path.write_text(json.dumps(result), encoding="utf-8")
        resumed = self.attempt(resume=True)
        resumed.prepare(MODULE.load_state(self.path))
        with (
            mock.patch.object(
                MODULE, "REQUIRED_CLOUD_TASK_SHA256", MODULE.sha256_file(self.helper)
            ),
            mock.patch.object(MODULE, "load_agent_task_result", return_value=result),
            mock.patch.object(MODULE, "verify_runtime_candidate") as verify,
            self.assertRaisesRegex(MODULE.WorkflowError, "dispatch identity"),
        ):
            self.assertTrue(resumed.dispatch())
            resumed.verify_result()
        verify.assert_not_called()

    def test_foreground_attempt_verifies_before_import_and_owns_cleanup(self):
        self.args.bounded_step = False
        self.args.hosted_timeout = 60
        self.args.hosted_discovery_interval = 1
        state = {"version": MODULE.STATE_VERSION, "history": []}
        attempt = self.attempt()
        attempt.prepare(state)
        result = {
            "status": "success",
            "task": {"id": "task"},
            "generated": {},
            "report": {},
        }

        def complete(_command, **_kwargs):
            attempt.result_path.write_text(json.dumps(result), encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "", "")

        with (
            mock.patch.object(MODULE, "discover_cloud_task", return_value=self.helper),
            mock.patch.object(MODULE, "load_candidate_runtime") as load_runtime,
            mock.patch.object(MODULE, "local_ci_briefing", return_value="briefing"),
            mock.patch.object(MODULE, "require_live_check_snapshot"),
            mock.patch.object(MODULE, "bounded_worker_prompt", return_value=("prompt", "evidence")),
            mock.patch.object(MODULE, "run_hosted_helper", side_effect=complete) as dispatch,
            mock.patch.object(MODULE, "load_agent_task_result", return_value=result),
            mock.patch.object(
                MODULE, "verify_runtime_candidate", return_value={"task_id": "task"}
            ),
            mock.patch.object(
                MODULE, "apply_verified_candidate_import", return_value=True
            ) as import_candidate,
            mock.patch.object(MODULE, "finalize_agent_task_artifacts") as finalize,
        ):
            self.assertTrue(attempt.dispatch())
            self.assertEqual(
                {"task_id": "task"}, attempt.verify_result().remote
            )
            self.assertTrue(attempt.import_candidate({"task_id": "task"}))
            attempt.finalize_artifacts(MODULE.load_state(self.path), preserve=False)

        dispatch.assert_called_once()
        import_candidate.assert_called_once()
        self.assertEqual(self.helper, import_candidate.call_args.kwargs["helper"])
        self.assertEqual("prompt", import_candidate.call_args.kwargs["prompt"])
        load_runtime.assert_called_once_with(self.helper)
        self.assertIs(
            load_runtime.return_value, import_candidate.call_args.kwargs["runtime"],
        )
        self.assertIn(attempt.result_path, finalize.call_args.args[2])
        self.assertIn(attempt.briefing_path, finalize.call_args.args[2])


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


class ProcessedCISnapshotsTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "sha256": "same", "head_sha": "head", "base_sha": "base",
            "rollup": [], "workflow_runs": {},
            "decision": {"decision": "green"},
        }
        self.preflight = {"pr": {"head_sha": "head"}, "check_snapshot": self.snapshot}
        self.state = {
            "budget_scope": "pipeline", "bounded_step": {"owner": {"pipeline_iteration": 1}},
            "coordinator": {
                "processed_snapshots": [{
                    "snapshot_sha256": "same", "stability_sha256": "old-stability",
                    "task_id": "first",
                }],
            },
        }

    def test_later_sweep_reuses_snapshot_once_without_losing_earlier_receipts(self):
        ledger = MODULE.ProcessedCISnapshots(self.state)
        self.assertEqual({"same", "old-stability"}, ledger.identities())
        ledger.start_sweep()
        self.state["bounded_step"]["owner"]["pipeline_iteration"] = 2
        self.assertEqual(1, self.state["bounded_processed_snapshot_baseline"])
        self.assertEqual(set(), ledger.identities())
        result = {"result": "published", "task": {"id": "second"}}
        ledger.record(self.preflight, result)
        ledger.record(self.preflight, result)
        self.assertEqual(
            ["first", "second"],
            [entry["task_id"] for entry in self.state["coordinator"]["processed_snapshots"]],
        )
        self.assertEqual(
            {"same", MODULE.ci_stability_sha256(self.snapshot)},
            ledger.identities(),
        )

    def test_standalone_receipts_ignore_bounded_sweep_baseline(self):
        ledger = MODULE.ProcessedCISnapshots(self.state)
        ledger.start_sweep()
        self.state.pop("bounded_step")
        ledger.record(self.preflight, {"result": "published", "task": {"id": "later"}})
        self.assertEqual(1, len(self.state["coordinator"]["processed_snapshots"]))
        self.assertIn("same", ledger.identities())

    def test_malformed_receipts_fail_at_each_caller_boundary(self):
        for malformed in (None, {}, "bad"):
            with self.subTest(malformed=malformed):
                self.state["coordinator"]["processed_snapshots"] = malformed
                ledger = MODULE.ProcessedCISnapshots(self.state)
                with self.assertRaisesRegex(MODULE.WorkflowError, "receipts are malformed"):
                    ledger.start_sweep()
                with self.assertRaisesRegex(MODULE.WorkflowError, "receipts are malformed"):
                    ledger.identities()
                with self.assertRaisesRegex(MODULE.WorkflowError, "receipts are malformed"):
                    ledger.record(self.preflight, {"result": "published"})

    def test_invalid_bounded_baseline_cannot_replay_earlier_receipts(self):
        ledger = MODULE.ProcessedCISnapshots(self.state)
        for baseline in (-1, 2, True, "0"):
            with self.subTest(baseline=baseline):
                self.state["bounded_processed_snapshot_baseline"] = baseline
                with self.assertRaisesRegex(MODULE.WorkflowError, "baseline is malformed"):
                    ledger.identities()
                with self.assertRaisesRegex(MODULE.WorkflowError, "baseline is malformed"):
                    ledger.record(self.preflight, {"result": "published"})
                self.assertEqual(1, len(self.state["coordinator"]["processed_snapshots"]))


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

    def test_observation_transition_records_actual_status_in_both_modes(self):
        pending = copy.deepcopy(self.preflight)
        pending["check_snapshot"]["decision"] = {
            "decision": "waiting", "detail": "checks running",
        }
        for bounded in (False, True):
            with self.subTest(bounded=bounded), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.json"
                MODULE.save_state(path, {
                    "version": MODULE.STATE_VERSION, "history": [], "iterations": 0,
                    "bounded_step": {"owner": {}},
                })
                gate = MODULE.CIStabilityGate(
                    state_path=path, bounded_checkpoint=bounded,
                )
                self.assertEqual(
                    ("waiting_for_checks", False),
                    gate.observe_once(
                        pending, processed=set(), now=self.now,
                        polls=1, debounce_seconds=0,
                    ),
                )
                state = MODULE.load_state(path)
                self.assertEqual("waiting_for_checks", state["coordinator"]["status"])
                if bounded:
                    self.assertEqual(
                        self.now.isoformat(), state["bounded_step"]["stable_since"],
                    )

    def test_confirmation_transition_restarts_changed_observation_in_both_modes(self):
        for bounded in (False, True):
            with self.subTest(bounded=bounded), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.json"
                MODULE.save_state(path, {
                    "version": MODULE.STATE_VERSION, "history": [], "iterations": 0,
                    "bounded_step": {"owner": {}},
                })
                gate = MODULE.CIStabilityGate(
                    state_path=path, bounded_checkpoint=bounded,
                )
                self.assertTrue(gate.observe_once(
                    self.preflight, processed=set(), now=self.now,
                    polls=1, debounce_seconds=0,
                )[1])
                error = MODULE.WorkflowError(
                    "CI attempt changed",
                    details={"reason": "ci_observation_changed"},
                )
                with mock.patch.object(
                    MODULE, "require_live_check_snapshot", side_effect=error,
                ):
                    self.assertIsNone(gate.confirm_once(lambda: self.preflight))
                state = MODULE.load_state(path)
                self.assertEqual("waiting_for_checks", state["coordinator"]["status"])
                self.assertEqual(0, gate.polls)
                self.assertEqual("CI attempt changed", state["coordinator"]["detail"])
                if bounded:
                    self.assertEqual(0, state["coordinator"]["stable_polls"])
                    self.assertIsNone(state["bounded_step"]["stable_since"])


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
            self.assertEqual(self.result, state["coordinator"]["pending_rerun"]["result"])
            self.assertEqual(self.preflight, state["coordinator"]["pending_rerun"]["preflight"])
            self.assertNotIn("pending_rerun", state["bounded_step"])
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
                state["coordinator"]["pending_rerun"]["result"],
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

        with (
            mock.patch.object(MODULE, "command_rerun", side_effect=reject),
            mock.patch.object(MODULE, "save_state", wraps=MODULE.save_state) as save,
        ):
            terminal = MODULE.advance_ci_rerun(
                self.path, self.preflight, self.result, bounded=True,
            )
        self.assertEqual(2, save.call_count)
        self.assertNotIn("pending_rerun", save.call_args.args[1]["coordinator"])
        self.assertEqual(terminal, save.call_args.args[1]["bounded_step"]["terminal"])
        state = MODULE.load_state(self.path)
        self.assertEqual("no_rerun_support", terminal["result"])
        self.assertEqual(terminal, state["bounded_step"]["terminal"])
        self.assertNotIn("pending_rerun", state["coordinator"])
        self.assertEqual(1, len(state["coordinator"]["processed_snapshots"]))

    def test_legacy_bounded_checkpoint_migrates_without_repeating_a_check(self):
        requested = []

        def rerun(args):
            requested.append(args.check)
            MODULE.emit({"result": "rerun_requested"})

        with mock.patch.object(MODULE, "command_rerun", side_effect=rerun):
            MODULE.advance_ci_rerun(self.path, self.preflight, self.result, bounded=True)
            state = MODULE.load_state(self.path)
            pending = state["coordinator"]["pending_rerun"]
            state["bounded_step"]["pending_rerun"] = {
                "preflight": pending.pop("preflight"),
                "result": pending.pop("result"),
            }
            MODULE.save_state(self.path, state)
            preflight, result = MODULE.resumable_ci_rerun(MODULE.load_state(self.path))
            MODULE.advance_ci_rerun(self.path, preflight, result, bounded=True)

        self.assertEqual(["check:a", "check:b"], requested)
        migrated = MODULE.load_state(self.path)
        self.assertEqual(self.result, migrated["coordinator"]["pending_rerun"]["result"])
        self.assertEqual(self.preflight, migrated["coordinator"]["pending_rerun"]["preflight"])

    def test_disagreeing_legacy_checkpoint_fails_before_requesting_rerun(self):
        with mock.patch.object(MODULE, "command_rerun",
                               side_effect=lambda _: MODULE.emit({"result": "rerun_requested"})):
            MODULE.advance_ci_rerun(self.path, self.preflight, self.result, bounded=True)
        state = MODULE.load_state(self.path)
        state["bounded_step"]["pending_rerun"] = {
            "preflight": self.preflight,
            "result": {**self.result, "action_checks": ["check:b"]},
        }
        MODULE.save_state(self.path, state)
        with self.assertRaisesRegex(MODULE.WorkflowError, "disagrees"):
            MODULE.resumable_ci_rerun(MODULE.load_state(self.path))

    def test_malformed_completed_checks_cannot_restart_a_rerun(self):
        with mock.patch.object(MODULE, "command_rerun",
                               side_effect=lambda _: MODULE.emit({"result": "rerun_requested"})):
            MODULE.advance_ci_rerun(self.path, self.preflight, self.result, bounded=True)
        state = MODULE.load_state(self.path)
        state["coordinator"]["pending_rerun"]["completed_checks"] = ["unknown"]
        MODULE.save_state(self.path, state)
        with self.assertRaisesRegex(MODULE.WorkflowError, "progress is malformed"):
            MODULE.resumable_ci_rerun(MODULE.load_state(self.path))


class BoundedSweepAdmissionTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.json"
        self.owner = {
            "session_id": SESSION, "pipeline_run": "a" * 32,
            "pipeline_iteration": 1, "pipeline_max_iterations": 3,
            "model": "sol", "github_mutation_policy": "allow",
            "max_iterations": 5, "target": {"number": 7},
            "repo_root": directory.name, "state": str(self.path),
        }

    def test_admission_resumes_and_reopens_only_completed_sweeps(self):
        first = MODULE.begin_bounded_ci_sweep(self.path, self.owner)
        self.assertEqual(self.owner, first["bounded_step"]["owner"])
        self.assertEqual({}, first["reruns"])
        self.assertIsNone(first["escalation"])
        self.assertEqual(first, MODULE.begin_bounded_ci_sweep(self.path, self.owner))

        first["budget_scope"] = "pipeline"
        first["bounded_step"]["terminal"] = {"result": "green"}
        first["coordinator"] = {
            "status": "waiting_for_checks",
            "stability_sha256": "old", "stable_polls": 8,
            "check_snapshot": {"sha256": "old"},
            "processed_snapshots": [{"snapshot_sha256": "old"}],
        }
        MODULE.save_state(self.path, first)
        next_owner = {**self.owner, "pipeline_iteration": 2}
        reopened = MODULE.begin_bounded_ci_sweep(self.path, next_owner)
        self.assertEqual({"owner": next_owner}, reopened["bounded_step"])
        self.assertEqual(1, reopened["bounded_processed_snapshot_baseline"])
        self.assertEqual(
            [{"snapshot_sha256": "old"}], reopened["coordinator"]["processed_snapshots"],
        )
        self.assertNotIn("stability_sha256", reopened["coordinator"])
        self.assertNotIn("stable_polls", reopened["coordinator"])
        self.assertNotIn("check_snapshot", reopened["coordinator"])
        self.assertEqual(set(), MODULE.ProcessedCISnapshots(reopened).identities())

    def test_admission_rejects_owner_drift_and_active_work_without_writing(self):
        state = MODULE.begin_bounded_ci_sweep(self.path, self.owner)
        state["bounded_step"]["terminal"] = {"result": "green"}
        MODULE.save_state(self.path, state)
        next_owner = {**self.owner, "pipeline_iteration": 2}

        for change in (
            {"pipeline_run": "b" * 32},
            {"model": "terra"},
        ):
            with self.subTest(change=change):
                before = self.path.read_bytes()
                with self.assertRaises(MODULE.WorkflowError):
                    MODULE.begin_bounded_ci_sweep(
                        self.path, {**next_owner, **change},
                    )
                self.assertEqual(before, self.path.read_bytes())

        for active in (
            {"agent_task": {"status": "bounded_pending"}},
            {"agent_task": {"status": "completed", "retry_command": "pending"}},
            {"agent_task": {"status": "completed", "dispatch_monitor": {"status": "running"}}},
            {"coordinator": {"pending_rerun": {"check": "build"}}},
            {"pending_stack_push": {"head": "head"}},
        ):
            with self.subTest(active=active):
                saved = copy.deepcopy(state)
                saved.update(active)
                MODULE.save_state(self.path, saved)
                before = self.path.read_bytes()
                with self.assertRaises(MODULE.WorkflowError):
                    MODULE.begin_bounded_ci_sweep(self.path, next_owner)
                self.assertEqual(before, self.path.read_bytes())

        MODULE.save_state(self.path, state)
        MODULE.begin_bounded_ci_sweep(self.path, next_owner)
        before = self.path.read_bytes()
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.begin_bounded_ci_sweep(self.path, self.owner)
        self.assertEqual(before, self.path.read_bytes())


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
            with mock.patch.object(MODULE.ProcessedCISnapshots, "identities", return_value=set()):
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
        self.assertNotIn("pending_rerun", self.state["coordinator"])

    def test_resume_uses_coordinator_or_legacy_checkpoint_after_restart(self):
        snapshot = {
            "sha256": "snapshot", "head_sha": "head", "base_sha": "base",
            "rollup": [], "workflow_runs": {},
            "decision": {"decision": "failures", "detail": "failed"},
        }
        preflight = {"pr": {"head_sha": "head"}, "check_snapshot": snapshot}
        requests = []

        def rerun(args):
            requests.append(args.check)
            MODULE.emit({"result": "rerun_requested"})

        with (
            mock.patch.object(MODULE, "agent_task_preflight", return_value=preflight) as observe,
            mock.patch.object(MODULE, "require_live_check_snapshot"),
            mock.patch.object(
                MODULE, "command_agent_task",
                side_effect=lambda _: MODULE.emit({
                    "result": "rerun", "task": {"id": "task"},
                    "action_checks": ["check:a", "check:b"],
                }),
            ) as task,
            mock.patch.object(MODULE, "command_rerun", side_effect=rerun),
        ):
            for legacy in (False, True):
                with self.subTest(legacy=legacy):
                    self.state = {}
                    self.output = []
                    requests.clear()
                    task.reset_mock()
                    observe.reset_mock()
                    MODULE.command_bounded_pipeline(self.args)
                    self.assertIn("preflight", self.state["coordinator"]["pending_rerun"])
                    self.assertNotIn("pending_rerun", self.state["bounded_step"])
                    saved = copy.deepcopy(self.state)
                    if legacy:
                        pending = saved["coordinator"]["pending_rerun"]
                        saved["bounded_step"]["pending_rerun"] = {
                            "preflight": pending.pop("preflight"),
                            "result": pending.pop("result"),
                        }
                    self.state = saved
                    MODULE.command_bounded_pipeline(self.args)
                    MODULE.command_bounded_pipeline(self.args)

                    self.assertEqual(["check:a", "check:b"], requests)
                    task.assert_called_once()
                    self.assertEqual(2, observe.call_count)
                    self.assertEqual(["rerun", "rerun", "checks_running"], [
                        item["reason"] for item in self.output
                    ])
                    self.assertNotIn("pending_rerun", self.state["coordinator"])
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
        self.assertEqual(set(), MODULE.ProcessedCISnapshots(self.state).identities())
        MODULE.record_processed_ci_snapshot(
            self.path, preflight, {"result": "published", "task": {"id": "second-task"}},
        )
        self.assertEqual(
            ["first-task", "second-task"],
            [entry["task_id"] for entry in self.state["coordinator"]["processed_snapshots"]],
        )
        self.assertIn("ready", MODULE.ProcessedCISnapshots(self.state).identities())

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
