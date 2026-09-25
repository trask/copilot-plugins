import copy
import json
from pathlib import Path
import unittest
from unittest import mock

import test_ci_fix_loop as fixtures


MODULE = fixtures.MODULE


class PromptBudgetTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagedAgentTaskContractTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.preflight = copy.deepcopy(self.fixture.preflight)
        self.runtime = self.fixture.runtime
        self.helper = self.fixture.root / "cloud_task.py"

    def failures(self, count):
        source = self.preflight["check_snapshot"]["failures"][0]
        self.preflight["check_snapshot"]["failures"] = [
            {
                **source,
                "key": f"check:Build pull request/build / common / test{index}"
                       " (25-deny-unsafe, hotspot, indy true)",
                "url": f"https://github.com/owner/repo/actions/runs/{index + 1}/job/{index + 11}",
            }
            for index in range(count)
        ]
        self.preflight["check_snapshot"]["workflow_runs"] = {
            str(index + 1): {"id": index + 1, "run_attempt": 2}
            for index in range(count)
        }

    def build(self, briefing):
        return MODULE.bounded_worker_prompt(
            self.preflight, helper=self.helper, iteration_allowance=1,
            prior_history=[], requested_model="gpt-5.6-sol", briefing=briefing,
        )

    def submitted(self, prompt):
        source = MODULE.expected_cloud_pull_request(self.preflight)
        snapshot = self.runtime.PullRequestSnapshot(
            **source, state="OPEN", cross_repository=False,
        )
        options = self.runtime.Options(
            report=False, apply_with_report=True, model="gpt-5.6-sol",
            policy=MODULE.AGENT_TASK_POLICY, prompt=prompt,
        )
        return self.runtime.task_payload(
            options, self.runtime.OUTPUT_REPORT_PATH, snapshot,
        )["prompt"]

    def test_thirty_nine_and_hundred_failed_checks_fit_full_hosted_payload(self):
        briefing = (
            "The first three checked jobs failed at WidgetTest:42 with AssertionError.\n"
            "The remaining jobs have not been inspected; do not assume they share this cause.\n"
            "Try ./gradlew :widget:test on Linux after checking build.gradle.\n"
        ) * 4
        for count in (39, 100):
            with self.subTest(count=count):
                self.failures(count)
                prompt, evidence = self.build(briefing)
                submitted = self.submitted(prompt)
                data = json.loads(evidence)
                self.assertEqual(count, data["total_failed_checks"])
                self.assertEqual(count, data["included_checks"])
                self.assertEqual(0, data["omitted_checks"])
                self.assertEqual(
                    [failure["key"] for failure in self.preflight["check_snapshot"]["failures"]],
                    [check["key"] for check in data["checks"]],
                )
                self.assertEqual(briefing, data["local_briefing"])
                self.assertLessEqual(len(submitted), MODULE.AGENT_TASK_PROMPT_MAX_CHARACTERS)
                self.assertLessEqual(
                    len(submitted.encode("utf-8")), MODULE.AGENT_TASK_PROMPT_MAX_UTF8_BYTES,
                )
                self.assertNotIn(self.preflight["check_snapshot"]["failures"][0]["log_path"], submitted)

    def test_long_briefing_keeps_some_checks_and_reports_omissions(self):
        self.failures(100)
        prompt, evidence = self.build("界" * 3900)
        data = json.loads(evidence)
        self.assertGreater(data["included_checks"], 0)
        self.assertGreater(data["omitted_checks"], 0)
        self.assertEqual(100, data["included_checks"] + data["omitted_checks"])
        self.assertEqual(
            list(range(data["included_checks"])),
            [check["id"] for check in data["checks"]],
        )
        self.assertLessEqual(len(self.submitted(prompt).encode("utf-8")), 28000)

    def test_briefing_plus_pinned_identity_overflow_fails_without_truncating_briefing(self):
        self.failures(1)
        self.preflight["pr"]["head_branch"] = "x" * 28000
        with self.assertRaisesRegex(MODULE.WorkflowError, "hosted prompt limit"):
            self.build("actual CI error")

    def test_active_route_rejects_overflow_before_dispatch(self):
        self.failures(1)
        self.preflight["pr"]["head_branch"] = "x" * 28000
        repo = self.fixture.root / "repo"
        repo.mkdir()
        self.preflight["repository_root"] = str(repo)
        state_path = self.fixture.root / "state.json"
        args = MODULE.build_parser().parse_args([
            "agent-task", self.preflight["pr"]["pr_url"],
            "--repo-root", str(repo), "--state", str(state_path),
        ])
        with (
            mock.patch.object(MODULE.subprocess, "Popen", side_effect=AssertionError("external execution")),
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=repo),
            mock.patch.object(MODULE, "resolve_target", return_value={"repo_name": "owner/repo", "number": 7}),
            mock.patch.object(MODULE, "agent_task_preflight", return_value=self.preflight),
            mock.patch.object(MODULE, "discover_cloud_task", return_value=self.helper),
            mock.patch.object(MODULE, "run_hosted_helper") as dispatch,
            mock.patch.object(MODULE, "require_live_check_snapshot"),
            self.assertRaisesRegex(MODULE.WorkflowError, "hosted prompt limit"),
        ):
            MODULE.command_agent_task(args)
        dispatch.assert_not_called()
        state = MODULE.load_state(state_path)
        self.assertEqual("not_created", state["agent_task"]["task_id_status"])
        self.assertEqual("failed", state["agent_task"]["status"])

    def test_changed_checks_after_briefing_prevent_hosted_dispatch(self):
        repo = self.fixture.root / "repo"
        repo.mkdir()
        self.preflight["repository_root"] = str(repo)
        state_path = self.fixture.root / "state.json"
        args = MODULE.build_parser().parse_args([
            "agent-task", self.preflight["pr"]["pr_url"],
            "--repo-root", str(repo), "--state", str(state_path),
        ])
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=repo),
            mock.patch.object(MODULE, "resolve_target", return_value={"repo_name": "owner/repo", "number": 7}),
            mock.patch.object(MODULE, "agent_task_preflight", return_value=self.preflight),
            mock.patch.object(MODULE, "discover_cloud_task", return_value=self.helper),
            mock.patch.object(MODULE, "run_hosted_helper") as dispatch,
            mock.patch.object(
                MODULE, "require_live_check_snapshot",
                side_effect=MODULE.WorkflowError(
                    "check set changed", details={"reason": "ci_observation_changed"}
                ),
            ),
            self.assertRaisesRegex(MODULE.WorkflowError, "check set changed"),
        ):
            MODULE.command_agent_task(args)
        self.fixture.local_briefing_mock.assert_called_once()
        dispatch.assert_not_called()
        state = MODULE.load_state(state_path)
        self.assertEqual("not_created", state["agent_task"]["task_id_status"])
        self.assertEqual("failed", state["agent_task"]["status"])
