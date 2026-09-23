import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from test_self_review_loop import MODULE


class HostedOutcomeTest(unittest.TestCase):
    def setUp(self):
        self.remote = {"commits": [], "candidate_manifest": {"artifact_commit": {
            "sha": "a" * 40, "changed_paths": [MODULE.AGENT_TASK_OUTPUT_RESULT],
        }}}

    def outcome(self, outcome, used):
        with mock.patch.object(MODULE, "git", return_value=json.dumps(
            {"outcome": outcome, "iterations_used": used}
        )) as read:
            result = MODULE.candidate_review_outcome(
                Path("repo"), self.remote, allowed_iterations=5
            )
        self.assertEqual(
            (Path("repo"), "show", f"{'a' * 40}:{MODULE.AGENT_TASK_OUTPUT_RESULT}"),
            read.call_args.args,
        )
        return result

    def test_clean_requires_one_review_pass(self):
        self.assertEqual({"outcome": "cleared", "iterations_used": 1}, self.outcome("clean", 1))
        self.assertEqual({"outcome": "incomplete", "iterations_used": 1}, self.outcome("incomplete", 1))

    def test_invalid_counts_never_supply_more_allowance(self):
        for outcome, used in (
            ("clean", 0), ("clean", True), ("clean", 2),
            ("exhausted", 1), ("incomplete", 0), ("unknown", 1),
        ):
            with self.subTest(outcome=outcome, used=used), self.assertRaises(MODULE.WorkflowError):
                self.outcome(outcome, used)

    def test_missing_outcome_is_not_clean_even_without_code(self):
        self.remote["candidate_manifest"]["artifact_commit"] = None
        with self.assertRaisesRegex(MODULE.WorkflowError, "no clean outcome or code"):
            MODULE.candidate_review_outcome(Path("repo"), self.remote, allowed_iterations=5)

    def test_verified_code_counts_as_one_pass_without_outcome(self):
        self.remote["commits"] = ["b" * 40, "c" * 40]
        self.remote["candidate_manifest"]["artifact_commit"] = None
        self.assertEqual(
            {"outcome": "continue", "iterations_used": 1},
            MODULE.candidate_review_outcome(Path("repo"), self.remote, allowed_iterations=5),
        )

    def test_code_cannot_claim_clean_in_the_same_pass(self):
        self.remote["commits"] = ["b" * 40]
        with self.assertRaisesRegex(MODULE.WorkflowError, "includes code commits"):
            self.outcome("clean", 1)

    def test_pipeline_calls_one_bounded_hosted_loop(self):
        args = SimpleNamespace(
            target="owner/repo#7", state="state.json", pipeline_run="run",
            pipeline_iteration=1, pipeline_max_iterations=2, max_iterations=5,
        )
        result = {"task": {"id": "task-1"}, "outcome": "max_iterations_reached"}
        with (
            mock.patch.object(MODULE, "command_agent_task", return_value=result) as run,
            mock.patch.object(MODULE, "emit") as emit,
        ):
            MODULE.command_pipeline(args)
        run.assert_called_once_with(args)
        self.assertEqual([result["task"]], emit.call_args.args[0]["tasks"])


if __name__ == "__main__":
    unittest.main()
