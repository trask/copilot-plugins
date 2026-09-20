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

    def logs(self, texts):
        source = self.preflight["check_snapshot"]["failures"][0]
        failures, runs = [], {}
        for index, text in enumerate(texts, start=1):
            path = self.fixture.root / f"log-{index}.txt"
            path.write_text(text, encoding="utf-8")
            failures.append({
                **source, "key": f"check:CI/test-{index}",
                "url": f"https://github.com/owner/repo/actions/runs/{index}/job/{index + 10}",
                "log_path": str(path), "log_sha256": MODULE.sha256_text(text),
            })
            runs[str(index)] = {
                "id": index, "workflow_id": index, "name": "CI",
                "head_sha": self.fixture.head, "run_attempt": 2,
                "status": "completed", "conclusion": "failure",
            }
        self.preflight["check_snapshot"].update(failures=failures, workflow_runs=runs)

    def build(self, *, history=None):
        return MODULE.bounded_worker_prompt(
            self.preflight, helper=self.helper, iteration_allowance=1,
            prior_history=history or [], requested_model="gpt-5.6-sol",
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

    def test_ascii_and_multibyte_logs_fit_both_complete_payload_caps(self):
        for text in ("x" * 30000, "\u754c" * 10000):
            with self.subTest(bytes=len(text.encode("utf-8"))):
                self.logs([text])
                prompt, evidence = self.build()
                submitted = self.submitted(prompt)
                self.assertLessEqual(len(submitted), 28000)
                self.assertLessEqual(len(submitted.encode("utf-8")), 28000)
                record = json.loads(evidence)["logs"][0]
                self.assertTrue(record["retrieve_full_log"])
                self.assertEqual(30000, record["omitted_utf8_bytes"])
                self.assertEqual(MODULE.sha256_text(text), record["log_sha256"])
                self.assertEqual(2, record["run"]["run_attempt"])
                self.assertEqual(11, record["job"]["job_id"])
                self.assertNotIn(str(self.fixture.root), submitted)

    def test_runtime_policy_overhead_is_included_before_inline_decision(self):
        self.logs(["x" * 20000])
        evidence = MODULE.controller_ci_evidence(self.preflight)
        consumer = MODULE.build_worker_prompt(
            self.preflight, iteration_allowance=1, prior_history=[],
            requested_model="gpt-5.6-sol", ci_evidence=evidence,
        )
        self.assertLess(len(consumer.encode("utf-8")), 28000)
        self.assertGreater(len(self.submitted(consumer).encode("utf-8")), 28000)
        prompt, bounded = self.build()
        self.assertTrue(json.loads(bounded)["logs"][0]["retrieve_full_log"])
        self.assertLessEqual(len(self.submitted(prompt).encode("utf-8")), 28000)

    def test_multiple_logs_keep_complete_small_errors_and_reference_omitted_logs(self):
        texts = ["first error\n" + "x" * 30000, "small actual error\n", "\u754c" * 10000 + "\nlast error"]
        self.logs(texts)
        prompt, evidence = self.build()
        submitted = self.submitted(prompt)
        self.assertLessEqual(len(submitted), 28000)
        self.assertLessEqual(len(submitted.encode("utf-8")), 28000)
        records = json.loads(evidence)["logs"]
        self.assertEqual(texts[1], records[1]["text"])
        for index in (0, 2):
            self.assertTrue(records[index]["retrieve_full_log"])
            self.assertEqual(MODULE.sha256_text(texts[index]), records[index]["log_sha256"])
            self.assertEqual(len(texts[index].encode("utf-8")), records[index]["omitted_utf8_bytes"])

    def test_identity_only_overflow_fails_without_truncation(self):
        self.logs(["actual error"])
        self.preflight["pr"]["head_branch"] = "x" * 28000
        with self.assertRaisesRegex(MODULE.WorkflowError, "identities exceed"):
            self.build()

    def test_active_route_rejects_identity_overflow_before_dispatch(self):
        self.logs(["actual error"])
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
            self.assertRaisesRegex(MODULE.WorkflowError, "identities exceed"),
        ):
            MODULE.command_agent_task(args)
        dispatch.assert_not_called()
        state = MODULE.load_state(state_path)
        self.assertEqual("not_created", state["agent_task"]["task_id_status"])
        self.assertEqual("failed", state["agent_task"]["status"])
