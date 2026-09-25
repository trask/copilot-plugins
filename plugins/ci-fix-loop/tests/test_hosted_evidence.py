import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "ci_fix_loop.py"
SPEC = importlib.util.spec_from_file_location("ci_hosted_evidence", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LocalCiBriefingTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "job.log"

    def preflight(self, text):
        self.path.write_text(text, encoding="utf-8", newline="\n")
        return {"check_snapshot": {
            "failures": [{
                "key": "check:Build/tests",
                "url": "https://github.com/owner/repo/actions/runs/11/job/22",
                "log_path": str(self.path), "log_sha256": MODULE.sha256_text(text),
            }],
            "workflow_runs": {"11": {"run_attempt": 3}},
            "head_sha": "a" * 40,
        }}

    def test_local_agent_uses_read_only_search_without_receiving_log_text(self):
        preflight = self.preflight("Error in a 100MB log\n")
        briefing = "The widget failed.\nReproduce on Linux: ./gradlew test"
        with mock.patch.object(
            MODULE, "run",
            return_value=subprocess.CompletedProcess([], 0, briefing, ""),
        ) as run:
            self.assertEqual(
                briefing, MODULE.local_ci_briefing(preflight, model="gpt-5.6-sol")
            )
        command = run.call_args.args[0]
        self.assertEqual("copilot", command[0])
        self.assertEqual("gpt-5.6-sol", command[command.index("--model") + 1])
        self.assertIn("--available-tools=view,rg,glob", command)
        self.assertIn("--disallow-temp-dir", command)
        self.assertIn("--disable-builtin-mcps", command)
        self.assertIn("--no-custom-instructions", command)
        self.assertNotIn("--max-ai-credits", command)
        self.assertNotIn("Error in a 100MB log", run.call_args.kwargs["input_text"])
        self.assertIn(self.path.name, run.call_args.kwargs["input_text"])
        self.assertEqual(self.path.parent, run.call_args.kwargs["cwd"])
        self.assertEqual(MODULE.LOCAL_CI_TRIAGE_TIMEOUT_SECONDS, run.call_args.kwargs["timeout"])
        self.assertEqual("false", run.call_args.kwargs["env"]["COPILOT_ALLOW_ALL"])

    def test_rejects_missing_drifted_or_changed_logs_and_bad_briefings(self):
        preflight = self.preflight("Error in job\n")
        with mock.patch.object(MODULE, "run") as run:
            self.path.write_text("different log\n", encoding="utf-8")
            with self.assertRaisesRegex(MODULE.WorkflowError, "identity changed"):
                MODULE.local_ci_briefing(preflight, model="gpt-5.6-sol")
            run.assert_not_called()
        self.path.write_text("Error in job\n", encoding="utf-8")
        for response in ("", "x" * (MODULE.LOCAL_CI_BRIEFING_MAX_BYTES + 1),
                         str(self.path), "\ud800", "failure\x1b[31m"):
            with self.subTest(response=response[:20]), mock.patch.object(
                MODULE, "run",
                return_value=subprocess.CompletedProcess([], 0, response, ""),
            ), self.assertRaises(MODULE.WorkflowError):
                MODULE.local_ci_briefing(preflight, model="gpt-5.6-sol")

        def change_log(*_args, **_kwargs):
            self.path.write_text("changed after investigation\n", encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "A plausible test failure", "")

        with mock.patch.object(MODULE, "run", side_effect=change_log), self.assertRaisesRegex(
            MODULE.WorkflowError, "identity changed"
        ):
            MODULE.local_ci_briefing(preflight, model="gpt-5.6-sol")

        self.path.unlink()
        with mock.patch.object(MODULE, "run") as run, self.assertRaisesRegex(
            MODULE.WorkflowError, "missing"
        ):
            MODULE.local_ci_briefing(preflight, model="gpt-5.6-sol")
        run.assert_not_called()

    def test_nonzero_exit_and_timeout_fail_before_dispatch(self):
        preflight = self.preflight("Error in job\n")
        with mock.patch.object(
            MODULE, "run", return_value=subprocess.CompletedProcess([], 2, "", "failure")
        ), self.assertRaisesRegex(MODULE.WorkflowError, "exited with code 2"):
            MODULE.local_ci_briefing(preflight, model="gpt-5.6-sol")
        with mock.patch.object(
            MODULE, "run", side_effect=subprocess.TimeoutExpired("copilot", 10)
        ), self.assertRaisesRegex(MODULE.WorkflowError, "timed out"):
            MODULE.local_ci_briefing(preflight, model="gpt-5.6-sol")

    def test_evidence_keeps_all_checks_distinct_without_interpreting_briefing(self):
        preflight = self.preflight("Widget failed\n")
        preflight["check_snapshot"]["failures"].append({
            **preflight["check_snapshot"]["failures"][0],
            "key": "check:Other",
        })
        briefing = "Maybe both checks share a root cause, but I only inspected one."
        evidence = json.loads(MODULE.controller_ci_evidence(preflight, briefing=briefing))
        self.assertEqual(2, evidence["total_failed_checks"])
        self.assertEqual(2, evidence["included_checks"])
        self.assertEqual(0, evidence["omitted_checks"])
        self.assertEqual(["check:Build/tests", "check:Other"],
                         [check["key"] for check in evidence["checks"]])
        self.assertEqual(briefing, evidence["local_briefing"])
        self.assertEqual({"11": 3}, evidence["run_attempts"])
        self.assertNotIn(str(self.path), json.dumps(evidence))
