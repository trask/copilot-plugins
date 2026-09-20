import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from test_copilot_review_loop import MODULE, EMPTY_ACTIVE_REVIEW_REQUIRED_STATE


class TerminalExhaustionTest(unittest.TestCase):
    def setUp(self):
        self.state = json.loads(EMPTY_ACTIVE_REVIEW_REQUIRED_STATE.read_text(encoding="utf-8"))
        self.target = MODULE.parse_target(
            f"{self.state['pr']['repo_name']}#{self.state['pr']['number']}"
        )
        self.state.update(
            iterations=5, max_iterations=5, last_result="max_iterations_reached",
            clean_at_head_sha=None, budget_scope="pipeline", repo_root="repo",
            github_mutation_policy="allow",
            pipeline_budget={"run": "run-1", "iteration": 1, "baseline": 0, "run_baseline": 0},
        )
        self.state["queue"].update(
            status="active", comments=[{"id": 19204, "status": "pending"}], batches=[]
        )

    def validate(self, state):
        return MODULE.terminal_agent_task_clearance_error(
            state, self.target, allow_exhausted=True
        )

    def test_spent_cap_is_terminal_but_never_clear(self):
        before = copy.deepcopy(self.state)
        self.assertIsNone(self.validate(self.state))
        self.assertEqual("carried", MODULE.stage_outcome(self.state))
        self.assertIsNotNone(MODULE.terminal_agent_task_clearance_error(self.state, self.target))
        self.assertEqual(before, self.state)

    def test_forged_exhaustion_and_unfinished_ownership_are_rejected(self):
        mutations = (
            {"iterations": 4},
            {"max_iterations": True},
            {"agent_task": {"status": "running"}},
            {"monitoring": {"status": "running"}},
            {"terminal_exit": {"status": "nonzero"}},
            {"coordinator": {"status": "blocked"}},
            {"clean_at_head_sha": self.state["pr"]["head_sha"]},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertIsNotNone(self.validate({**self.state, **mutation}))

    def test_later_sweep_admits_exhaustion_without_consuming_or_losing_feedback(self):
        before = copy.deepcopy(self.state)
        args = SimpleNamespace(
            pipeline_run="run-1", pipeline_iteration=2, max_iterations=5, model="sol"
        )
        with mock.patch.object(MODULE, "ACTIVE_GITHUB_MUTATION_POLICY", "allow"):
            MODULE.require_completed_pipeline_sweep(
                self.state, args, self.target, Path("repo")
            )
        self.assertEqual(before, self.state)


if __name__ == "__main__":
    unittest.main()
