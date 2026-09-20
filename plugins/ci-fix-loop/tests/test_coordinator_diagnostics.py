import argparse
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "ci_fix_loop.py"
SPEC = importlib.util.spec_from_file_location("ci_diagnostic_tests", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
OLD_HEAD = "a" * 40
NEW_HEAD = "b" * 40
BASE = "c" * 40


def snapshot(head, *, attempt=1, pending=True):
    checks = [
        {
            "key": "check:CI/test", "name": "test", "class": "failed",
            "workflow_run_id": 42, "status": "COMPLETED", "conclusion": "FAILURE",
        },
        {
            "key": "check:CI/aggregate", "name": "aggregate", "class": "failed",
            "workflow_run_id": 42, "status": "COMPLETED", "conclusion": "FAILURE",
        },
    ]
    if pending:
        checks.append({
            "key": "check:CI/running", "name": "running", "class": "running",
            "workflow_run_id": 42, "status": "IN_PROGRESS", "conclusion": None,
        })
    rollup = MODULE.check_rollup_identity(checks)
    result = {
        "head_sha": head, "base_sha": BASE, "observed_at": "2026-09-20T07:48:00Z",
        "rollup": rollup, "rollup_sha256": MODULE.canonical_json_sha256(rollup),
        "decision": {
            "decision": "failures", "reason": "checks_failed",
            "checks": ["check:CI/test"], "aggregate_checks": ["check:CI/aggregate"],
            "pending_checks": ["check:CI/running"] if pending else [],
            "detail": "test failed; aggregate failed; other checks are still pending",
        },
        "failures": [] if pending else [{
            "key": "check:CI/test", "kind": "check_run", "name": "test",
            "workflow": "CI", "url": None, "description": None,
            "conclusion": "FAILURE", "baseline_conclusion": None,
            "baseline_verdict": "unknown", "log_sha256": "1" * 64, "log_path": None,
        }],
        "workflow_runs": {} if pending else {
            "42": {
                "id": 42, "head_sha": head, "run_attempt": attempt,
                "status": "in_progress" if pending else "completed",
                "conclusion": None if pending else "failure",
            }
        },
    }
    result["sha256"] = MODULE.check_snapshot_sha256(result)
    return result


class CoordinatorDiagnosticTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "state.json"
        for name in ("run", "run_bytes", "gh_json"):
            patch = mock.patch.object(
                MODULE, name, side_effect=AssertionError("external work is forbidden")
            )
            patch.start()
            self.addCleanup(patch.stop)
        self.state = {
            "version": MODULE.STATE_VERSION, "iterations": 1, "history": [],
            "pr": {
                "number": 7, "title": "Change", "pr_url": "https://github.com/owner/repo/pull/7",
                "repo_name": "owner/repo", "head_branch": "feature", "base_branch": "main",
                "head_sha": NEW_HEAD, "base_sha": BASE,
            },
            "coordinator": {
                "head_sha": NEW_HEAD, "base_sha": BASE, "snapshot_sha256": "d" * 64,
                "stable_polls": 0, "status": "waiting_for_checks",
            },
            "run": {
                "id": "frozen-candidate", "head_sha": OLD_HEAD, "base_sha": BASE,
                "status": "published", "published_head_sha": NEW_HEAD,
                "decision": {
                    "decision": "failures", "reason": "checks_failed",
                    "checks": ["check:CI/spotless", "check:CI/old-test", "check:CI/aggregate"],
                    "pending_checks": [], "aggregate_checks": [], "detail": "frozen failures",
                },
                "attributions": {"check:CI/old-test": {"verdict": "unknown"}},
                "charged": True, "budget_identity": "e" * 64,
            },
            "agent_task": {
                "status": "completed", "iteration_allowance": 1,
                "preflight": {"check_snapshot": snapshot(OLD_HEAD, attempt=3, pending=False)},
                "candidate": {"source_head_sha": OLD_HEAD, "head_sha": NEW_HEAD},
            },
            "pipeline_budget": {"run": "f" * 32, "iteration": 1, "baseline": 0, "run_baseline": 0},
            "budget_scope": "pipeline", "budget_charges": {"frozen": 1},
            "accepted_pushes": [{"head_sha": NEW_HEAD}],
            "reruns": {}, "auto_retries": {}, "ci_retries": {},
        }
        self.error = MODULE.WorkflowError(
            "local coordinator timed out waiting for a stable terminal CI check set",
            details={"reason": "timeout"},
        )

    def save(self):
        MODULE.save_state(self.path, self.state)

    def observe(self, observation):
        MODULE.update_coordinator_state(
            self.path, status="waiting_for_checks",
            head_sha=observation["head_sha"], snapshot_sha256=observation["sha256"],
            stable_polls=0, detail=observation["decision"]["detail"],
            check_snapshot=observation,
        )

    def fail(self):
        before = MODULE.load_state(self.path)
        MODULE.record_coordinator_failure(self.path, self.error)
        after = MODULE.load_state(self.path)
        for key in before:
            if key not in {"coordinator", "escalation", "updated_at"}:
                self.assertEqual(before[key], after[key], key)
        self.assertEqual("timeout", after["escalation"]["reason"])
        self.assertEqual(str(self.error), after["escalation"]["detail"])
        self.assertEqual("blocked", after["coordinator"]["status"])
        self.assertEqual("escalated", MODULE.stage_outcome(after))
        self.assertEqual(
            before["run"]["decision"], after["escalation"]["frozen_run"]["decision"]
        )
        return after["escalation"]

    def test_published_run_with_only_retained_identity_reports_unknown_checks(self):
        """The retained timeout has a new head/hash, but only frozen check names."""
        self.save()
        escalation = self.fail()
        self.assertEqual(NEW_HEAD, escalation["head_sha"])
        self.assertEqual("unavailable", escalation["check_context"])
        self.assertEqual([], escalation["checks"])
        self.assertIsNone(escalation["check_snapshot"])
        self.assertEqual(OLD_HEAD, escalation["frozen_run"]["head_sha"])
        self.assertEqual(NEW_HEAD, escalation["frozen_run"]["published_head_sha"])

    def test_before_publication_uses_the_observed_snapshot_not_candidate_decision(self):
        self.state["pr"]["head_sha"] = OLD_HEAD
        self.state["run"]["status"] = "active"
        self.state["run"].pop("published_head_sha")
        self.save()
        observation = snapshot(OLD_HEAD, attempt=3, pending=False)
        self.observe(observation)
        escalation = self.fail()
        self.assertEqual(OLD_HEAD, escalation["head_sha"])
        self.assertEqual("last_observed", escalation["check_context"])
        self.assertEqual(observation, escalation["check_snapshot"])
        self.assertEqual(["check:CI/test"], escalation["checks"])
        self.assertEqual(["check:CI/aggregate"], escalation["aggregate_checks"])
        self.assertEqual(3, escalation["check_snapshot"]["workflow_runs"]["42"]["run_attempt"])

    def test_after_publication_keeps_failures_aggregate_and_pending_checks(self):
        self.save()
        observation = snapshot(NEW_HEAD)
        self.observe(observation)
        escalation = self.fail()
        self.assertEqual(NEW_HEAD, escalation["head_sha"])
        self.assertEqual("last_observed", escalation["check_context"])
        self.assertEqual(observation, escalation["check_snapshot"])
        self.assertEqual(["check:CI/test"], escalation["checks"])
        self.assertEqual(["check:CI/aggregate"], escalation["aggregate_checks"])
        self.assertEqual(["check:CI/running"], escalation["pending_checks"])
        self.assertEqual(3, len(escalation["check_snapshot"]["rollup"]))
        self.assertEqual({}, escalation["check_snapshot"]["workflow_runs"])
        self.assertEqual(0, MODULE.load_state(self.path)["coordinator"]["stable_polls"])

    def test_terminal_observation_retains_current_attempt_without_changing_frozen_attempt(self):
        self.save()
        self.observe(snapshot(NEW_HEAD, attempt=2, pending=False))
        escalation = self.fail()
        self.assertEqual(2, escalation["check_snapshot"]["workflow_runs"]["42"]["run_attempt"])
        frozen = MODULE.load_state(self.path)["agent_task"]["preflight"]["check_snapshot"]
        self.assertEqual(3, frozen["workflow_runs"]["42"]["run_attempt"])

    def test_stale_head_base_hash_or_same_head_attempt_cannot_label_checks_current(self):
        for change in ("head", "base", "snapshot", "attempt", "pr"):
            with self.subTest(change=change):
                self.save()
                self.observe(snapshot(NEW_HEAD, pending=False))
                state = MODULE.load_state(self.path)
                coordinator = state["coordinator"]
                if change == "head":
                    coordinator["head_sha"] = "1" * 40
                elif change == "base":
                    coordinator["base_sha"] = "2" * 40
                elif change == "snapshot":
                    coordinator["snapshot_sha256"] = "3" * 64
                elif change == "attempt":
                    coordinator["check_snapshot"]["workflow_runs"]["42"]["run_attempt"] = 2
                else:
                    state["pr"]["head_sha"] = "4" * 40
                MODULE.save_state(self.path, state)
                escalation = self.fail()
                self.assertEqual("unavailable", escalation["check_context"])
                self.assertIsNone(escalation["check_snapshot"])
                self.assertEqual([], escalation["checks"])

    def test_incomplete_next_observation_invalidates_even_same_head_context(self):
        self.save()
        self.observe(snapshot(NEW_HEAD))
        MODULE.record_coordinator_identity(
            self.path, self.root, self.state["pr"],
            {"head": NEW_HEAD, "branch": "feature", "status": ""},
        )
        escalation = self.fail()
        self.assertEqual(NEW_HEAD, escalation["head_sha"])
        self.assertEqual("unavailable", escalation["check_context"])

    def test_processed_candidate_does_not_reuse_its_snapshot_for_followup_wait(self):
        self.state["pr"]["head_sha"] = OLD_HEAD
        self.save()
        observation = snapshot(OLD_HEAD, attempt=3, pending=False)
        self.observe(observation)
        MODULE.record_processed_ci_snapshot(
            self.path, {"pr": self.state["pr"], "check_snapshot": observation},
            {"result": "published", "task": {"id": "frozen-task"}},
        )
        escalation = self.fail()
        self.assertEqual("unavailable", escalation["check_context"])
        self.assertEqual([], escalation["checks"])
        processed = MODULE.load_state(self.path)["coordinator"]["processed_snapshots"]
        self.assertEqual(observation["sha256"], processed[0]["snapshot_sha256"])

    def test_frozen_context_is_labeled_without_any_observed_identity(self):
        self.state.pop("coordinator")
        self.state.pop("pr")
        self.save()
        escalation = self.fail()
        self.assertIsNone(escalation["head_sha"])
        self.assertEqual("unavailable", escalation["check_context"])
        self.assertEqual(OLD_HEAD, escalation["frozen_run"]["head_sha"])

    def test_observation_copy_does_not_alias_candidate_or_caller_data(self):
        self.save()
        observation = snapshot(NEW_HEAD, pending=False)
        self.observe(observation)
        observation["decision"]["checks"].append("check:not-observed")
        state = MODULE.load_state(self.path)
        before = copy.deepcopy(state)
        context = MODULE.coordinator_failure_context(state)
        context["check_snapshot"]["workflow_runs"]["42"]["run_attempt"] = 99
        context["frozen_run"]["decision"]["checks"].clear()
        self.assertEqual(before, state)
        self.assertEqual(["check:CI/test"], context["checks"])

    def test_status_outputs_preserve_observation_and_frozen_labels(self):
        self.save()
        self.observe(snapshot(NEW_HEAD, attempt=2, pending=False))
        escalation = self.fail()
        full = MODULE.status_payload(MODULE.load_state(self.path), self.path)
        with mock.patch.object(MODULE, "emit") as emit:
            MODULE.command_status(argparse.Namespace(current=False, state=str(self.path)))
        compact = emit.call_args.args[0]
        for payload in (full, compact):
            self.assertEqual(escalation, payload["escalation"])
            self.assertEqual(OLD_HEAD, payload["run"]["head_sha"])
            self.assertEqual(NEW_HEAD, payload["coordinator"]["head_sha"])
            self.assertEqual("escalated", payload["stage_outcome"])

    def test_wait_retains_pending_observation_without_extending_timeout(self):
        self.save()
        observation = snapshot(NEW_HEAD)
        clock = [0]
        args = argparse.Namespace(
            wait_timeout=1, poll_interval=1, poll_max_interval=1, poll_jitter=0,
            stability_polls=2, debounce_seconds=0, stack_state=None,
        )

        def sleep(seconds):
            clock[0] += seconds

        with (
            mock.patch.object(MODULE, "agent_task_preflight", return_value={
                "pr": self.state["pr"], "check_snapshot": observation,
            }) as preflight,
            mock.patch.object(MODULE.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(MODULE.time, "sleep", side_effect=sleep),
            self.assertRaisesRegex(MODULE.WorkflowError, "timed out"),
        ):
            MODULE.wait_for_stable_ci_preflight(
                args, repo_root=self.root, target=self.state["pr"], state_path=self.path,
            )
        self.assertEqual(1, clock[0])
        self.assertEqual(1, preflight.call_count)
        self.assertEqual(observation, self.fail()["check_snapshot"])

    def test_debounce_changed_attempt_is_the_last_observation_at_timeout(self):
        self.save()
        first = snapshot(NEW_HEAD, pending=False)
        changed = snapshot(NEW_HEAD, attempt=2, pending=False)
        clock = [0]
        args = argparse.Namespace(
            wait_timeout=1, poll_interval=0, poll_max_interval=0, poll_jitter=0,
            stability_polls=1, debounce_seconds=0.5, stack_state=None,
        )

        def read_preflight(*_args, **_kwargs):
            observation = first if clock[0] == 0 else changed
            clock[0] += 0.25
            return {"pr": self.state["pr"], "check_snapshot": observation}

        def sleep(seconds):
            clock[0] += seconds

        with (
            mock.patch.object(MODULE, "agent_task_preflight", side_effect=read_preflight) as preflight,
            mock.patch.object(MODULE.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(MODULE.time, "sleep", side_effect=sleep),
            self.assertRaisesRegex(MODULE.WorkflowError, "timed out"),
        ):
            MODULE.wait_for_stable_ci_preflight(
                args, repo_root=self.root, target=self.state["pr"], state_path=self.path,
            )
        self.assertEqual(1, clock[0])
        self.assertEqual(2, preflight.call_count)
        self.assertEqual(changed, self.fail()["check_snapshot"])
        self.assertEqual(0, MODULE.load_state(self.path)["coordinator"]["stable_polls"])
