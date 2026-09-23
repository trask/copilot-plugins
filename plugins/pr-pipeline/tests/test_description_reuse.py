import copy
import json
from types import SimpleNamespace
from unittest import mock

from test_pr_stack_pipeline import BASE, COMMON, MODULE, StackFixture, kickoff, stack


class DescriptionReuseTest(StackFixture):
    def setUp(self):
        super().setUp()
        self.stack = stack(members=(11, 12))
        self.receipts = {}
        self.launcher.on_start = self.complete
        self.controller = self.pipeline(kickoff(numbers=(11, 12)), inspect=self.inspect_receipt)

    def complete(self, request):
        self.receipts[request["number"]] = {
            "stage_outcome": "cleared",
            "run_id": f"description-{request['number']}-{request['pass']}",
            "pipeline_run": "run-1",
            "pipeline_iteration": request["pass"],
            "pipeline_max_iterations": 2,
            "validated_head_sha": request["head_sha"],
            "pr": {"base": {"sha": request["base_sha"]}},
            "agent_task": {
                "status": "completed",
                "task": {"state": "completed", "id": request["nonce"]},
                "model": "gpt-5.6-sol",
                "github_mutation_policy": "allow",
            },
            "clearance_verification": {
                "result": "current",
                "reason": "description_snapshot_current",
                "expected_snapshot_sha256": "a" * 64,
                "observed_snapshot_sha256": "a" * 64,
            },
        }

    def inspect_receipt(self, entry, target, head, base):
        if entry["stage"] != MODULE.STAGE_DESCRIPTION:
            return self.inspect(entry, target, head, base)
        payload = copy.deepcopy(self.receipts.get(target["number"]))
        return COMMON.inspect_stage(
            entry, target, head, base, pipeline_run=self.controller.run_id,
            read_status=lambda *_: {
                "ok": payload is not None, "installed": True, "state": "state.json",
                "payload": payload, "reason": None if payload else "no_state",
            },
        )

    def phase(self, iteration):
        return self.controller.run_parallel_phase(
            MODULE.STAGE_DESCRIPTION, iteration, self.stack["members"]
        )

    def test_parent_reused_while_changed_child_gets_its_normal_second_pass(self):
        self.assertEqual(2, self.phase(1)["dispatches"])
        parent_receipt = copy.deepcopy(self.receipts[11])
        self.stack["members"][1]["head_sha"] = "9" * 40
        second = self.phase(2)
        self.assertIsNone(second["stopped"])
        self.assertEqual(1, second["dispatches"])
        self.assertEqual([12], [item["number"] for item in second["completions"]])
        self.assertEqual([11], [item["number"] for item in second["reused"]])
        summary = MODULE.summarize_phase(second)
        self.assertTrue(summary["clear"])
        self.assertEqual([12], summary["accepted"])
        self.assertEqual([11], summary["reused"])
        self.assertEqual(parent_receipt, self.receipts[11])
        self.assertEqual([1, 1, 2], [request["pass"] for request in self.launcher.started])
        parent = self.controller.state["pull_requests"]["11"]["stages"][MODULE.STAGE_DESCRIPTION]
        self.assertEqual(1, parent["pass"])
        self.assertEqual(2, parent["reused_in_pass"])
        terminal = MODULE.compact_terminal_result({"result": "complete", "phases": [summary]})
        self.assertEqual([11], terminal["phases"][0]["reused"])
        self.assertEqual(1, terminal["phases"][0]["dispatches"])

    def test_all_reused_is_clear_without_any_new_worker_or_completion(self):
        self.phase(1)
        before = list(self.launcher.calls)
        second = self.phase(2)
        self.assertEqual(before, self.launcher.calls)
        self.assertEqual([], second["completions"])
        summary = MODULE.summarize_phase(second)
        self.assertTrue(summary["clear"])
        self.assertEqual(0, summary["dispatches"])
        self.assertEqual([], summary["accepted"])
        self.assertEqual([11, 12], summary["reused"])
        event = self.events_named("phase_finished")[-1]
        self.assertEqual([11, 12], event["reused"])
        update = MODULE.progress_transition(event)
        self.assertIn("reused current Description clearance", update["message"])
        started = self.events_named("phase_started")[-1]
        self.assertEqual([], started["dispatch_numbers"])
        self.assertFalse(MODULE.progress_transition(started)["waiting"])

    def test_missing_clearance_runs_required_workers_instead_of_claiming_reuse(self):
        first = self.phase(1)
        self.assertEqual([], first["reused"])
        self.assertEqual(2, first["dispatches"])

    def test_status_read_requests_description_freshness_at_the_exact_state_path(self):
        script = self.root / "pr_description.py"
        state = self.root / "description-state.json"
        script.write_text("", encoding="utf-8")
        state.write_text("{}", encoding="utf-8")
        target = COMMON.target_for("owner/repo", 11)
        payload = {"result": "ready", "state": str(state), "pr": target}
        with mock.patch.object(COMMON, "run", return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps(payload), stderr="",
        )) as run:
            result = COMMON.read_stage_status(
                MODULE.STAGE_BY_NAME[MODULE.STAGE_DESCRIPTION], target,
                script_for=lambda _: script, state_for=lambda *_: state,
            )
        self.assertTrue(result["ok"])
        command = run.call_args.args[0]
        self.assertIn("--verify-clearance-snapshot", command)
        self.assertEqual(str(state), command[command.index("--state") + 1])

    def test_invalid_same_head_provenance_or_freshness_blocks_without_repeat(self):
        self.phase(1)
        state = copy.deepcopy(self.controller.state)
        receipts = copy.deepcopy(self.receipts)
        mutations = {
            "missing receipt": lambda prior, receipt: self.receipts.pop(11),
            "failed exit": lambda prior, receipt: prior.update(returncode=1),
            "missing exit": lambda prior, receipt: prior.pop("returncode"),
            "unaccepted": lambda prior, receipt: prior.update(accepted=False),
            "foreign run": lambda prior, receipt: receipt.update(pipeline_run="other"),
            "foreign invocation": lambda prior, receipt: receipt.update(run_id="other"),
            "future invocation": lambda prior, receipt: receipt.update(pipeline_iteration=3),
            "unknown iteration": lambda prior, receipt: receipt.update(pipeline_iteration=True),
            "same iteration": lambda prior, receipt: prior.update({"pass": 2}),
            "wrong cap": lambda prior, receipt: receipt.update(pipeline_max_iterations=3),
            "missing prior identity": lambda prior, receipt: prior["stage_result"].pop("run_id"),
            "same-head invalidated completion": lambda prior, receipt: (
                prior.update(clear=False),
                receipt["clearance_verification"].update(result="stale"),
            ),
            "failed task": lambda prior, receipt: receipt["agent_task"].update(status="failed"),
            "model changed": lambda prior, receipt: receipt["agent_task"].update(model="gpt-6-astra"),
            "local model": lambda prior, receipt: receipt["agent_task"].update(model="gpt-6-sol"),
            "policy changed": lambda prior, receipt: receipt["agent_task"].update(github_mutation_policy="source-only"),
            "unknown base": lambda prior, receipt: receipt["pr"]["base"].pop("sha"),
            "changed metadata": lambda prior, receipt: receipt["clearance_verification"].update(result="stale"),
            "unknown metadata": lambda prior, receipt: receipt.pop("clearance_verification"),
            "mismatched snapshot": lambda prior, receipt: receipt["clearance_verification"].update(observed_snapshot_sha256="b" * 64),
        }
        before = list(self.launcher.calls)
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.controller.state = copy.deepcopy(state)
                self.receipts = copy.deepcopy(receipts)
                prior = self.controller.state["pull_requests"]["11"]["stages"][MODULE.STAGE_DESCRIPTION]
                mutate(prior, self.receipts[11])
                second = self.phase(2)
                self.assertEqual("description_clearance_not_reusable", second["stopped"]["reason"])
                self.assertEqual(0, second["dispatches"])
                self.assertEqual([], second["reused"])
                self.assertFalse(MODULE.summarize_phase(second)["clear"])
                self.assertEqual(before, self.launcher.calls)

    def test_stale_clear_cannot_override_new_worker_failure_with_mixed_reuse(self):
        self.phase(1)
        self.stack["members"][1]["head_sha"] = "9" * 40
        with mock.patch.object(self.launcher, "wait", return_value={"returncode": 1}):
            second = self.phase(2)
        self.assertEqual([11], [item["number"] for item in second["reused"]])
        self.assertEqual("stage_execution_failed", second["stopped"]["reason"])
        self.assertTrue(second["completions"][0]["clear"])
        self.assertFalse(MODULE.summarize_phase(second)["clear"])

    def test_fresh_run_does_not_adopt_existing_successful_clearance(self):
        self.phase(1)
        self.controller.run_id = "fresh-run"
        self.controller.state = MODULE.new_state(
            self.controller.kickoff, "fresh-run", MODULE.topology_fingerprint(self.stack)
        )
        self.launcher.on_start = None
        current = self.inspect_receipt(
            MODULE.STAGE_BY_NAME[MODULE.STAGE_DESCRIPTION],
            {"number": 11}, self.stack["members"][0]["head_sha"], BASE,
        )
        self.assertFalse(current["clear"])
        first = self.phase(1)
        self.assertEqual([], first["reused"])
        self.assertFalse(MODULE.summarize_phase(first)["clear"])
