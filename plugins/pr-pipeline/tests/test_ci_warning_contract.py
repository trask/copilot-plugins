from contextlib import redirect_stdout
import copy
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


sys.dont_write_bytecode = True
PLUGIN = Path(__file__).parents[1]
CI_SCRIPT = Path(
    os.environ.get(
        "PR_PIPELINE_CI_CONTRACT_SCRIPT",
        str(PLUGIN.parent / "ci-fix-loop" / "scripts" / "ci_fix_loop.py"),
    )
)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CI = load("ci_warning_contract_producer", CI_SCRIPT)
PIPELINE = load("ci_warning_contract_consumer", PLUGIN / "scripts" / "pr_pipeline.py")


class CiWarningProducerContractTest(unittest.TestCase):
    def test_green_status_revalidates_same_head_attempts_without_hosted_work(self):
        pr = {
            "repo_name": "owner/repo", "upstream_owner": "owner", "upstream_repo": "repo",
            "number": 7, "pr_url": "https://github.com/owner/repo/pull/7",
            "head_sha": "a" * 40, "base_sha": "b" * 40, "head_branch": "topic",
            "base_branch": "main", "title": "Example",
        }
        checks = [{"key": "check:Build/test", "name": "test", "workflow": "Build",
                   "workflow_run_id": 11, "class": "passed"}]
        run = {"id": 11, "workflow_id": 22, "name": "Build", "head_sha": pr["head_sha"],
               "run_attempt": 1, "status": "completed", "conclusion": "success"}
        state = {
            "version": CI.STATE_VERSION, "pr": pr, "history": [], "iterations": 2,
            "reruns": {}, "outcome": "green", "clean_at_head_sha": pr["head_sha"],
            "clean_at_base_sha": pr["base_sha"],
            "green_snapshot_sha256": CI.ci_warning_snapshot_sha256(pr, checks, {"11": run}),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            original = path.read_bytes()
            for status, conclusion, attempt, current in (
                ("completed", "success", 1, True),
                ("in_progress", None, 2, False),
                ("completed", "failure", 2, False),
                ("completed", "success", 2, False),
            ):
                with self.subTest(status=status, attempt=attempt, conclusion=conclusion):
                    output = StringIO()
                    with (
                        mock.patch.object(CI, "metadata_for", return_value=pr),
                        mock.patch.object(CI, "fetch_rollup", return_value=(pr["head_sha"], checks)),
                        mock.patch.object(CI, "ci_snapshot_runs", return_value={
                            "11": {**run, "status": status, "conclusion": conclusion, "run_attempt": attempt},
                        }),
                        mock.patch.object(CI, "run", side_effect=AssertionError("hosted or executable work forbidden")),
                        redirect_stdout(output),
                    ):
                        CI.command_status(CI.build_parser().parse_args([
                            "status", "--state", str(path), "--verify-clearance-snapshot",
                        ]))
                    payload = json.loads(output.getvalue())
                    observed = PIPELINE.common.inspect_stage(
                        PIPELINE.STAGE_BY_NAME[PIPELINE.STAGE_CI], pr,
                        pr["head_sha"], pr["base_sha"], read_status=lambda *_: {
                            "ok": True, "installed": True, "state": str(path), "payload": payload,
                        },
                    )
                    self.assertEqual(current, observed["clear"])
                    self.assertEqual(original, path.read_bytes())
                    self.assertEqual(2, payload["iterations"])

    def test_snapshot_sees_new_runs_before_they_appear_in_check_rollup(self):
        pr = {"repo_name": "owner/repo", "head_sha": "a" * 40}
        old = {"id": 11, "workflow_id": 22, "event": "pull_request", "head_sha": pr["head_sha"]}
        latest = {**old, "id": 12}
        with (
            mock.patch.object(CI, "gh_json", return_value=[{"workflow_runs": [old, latest]}]),
            mock.patch.object(CI, "ci_check_runs", return_value={}),
            mock.patch.object(CI, "ci_run_identity", return_value={**latest, "status": "in_progress"}) as observe,
        ):
            runs = CI.ci_snapshot_runs(pr, [])
        self.assertEqual({"12"}, set(runs))
        observe.assert_called_once_with(pr, 12)

    def test_actual_status_outputs_clear_only_the_unchanged_snapshot(self):
        pr = {
            "repo_name": "owner/repo",
            "upstream_owner": "owner",
            "upstream_repo": "repo",
            "head_sha": "a" * 40,
            "base_sha": "b" * 40,
            "pr_url": "https://github.com/owner/repo/pull/7",
            "number": 7,
            "title": "Example",
            "head_branch": "topic",
            "base_branch": "main",
        }
        checks = [{
            "key": "check:Build/test",
            "name": "test",
            "workflow": "Build",
            "workflow_run_id": 11,
            "class": "failed",
        }]
        runs = {"11": {
            "id": 11,
            "workflow_id": 22,
            "name": "Build",
            "head_sha": pr["head_sha"],
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "failure",
        }}
        pipeline_run = "1" * 32
        state = {
            "version": CI.STATE_VERSION,
            "pr": pr,
            "history": [],
            "iterations": 1,
            "reruns": {},
            "outcome": "warning",
            "clean_at_head_sha": None,
            "warning_at_head_sha": pr["head_sha"],
            "warning_at_base_sha": pr["base_sha"],
            "warning_snapshot_sha256": CI.ci_warning_snapshot_sha256(pr, checks, runs),
            "pipeline_budget": {"run": pipeline_run, "max_iterations": 2},
            "ci_warnings": [{
                "check_key": "check:Build/test",
                "name": "test",
                "diagnosis": "unrelated",
                "reason": "Pinned base has the same failure",
                "evidence": ["same assertion in base log"],
            }],
        }
        cases = (
            ("current", pr, checks, runs, True),
            (
                "description edit",
                {**pr, "title": "Revised title", "body": "Revised description"},
                checks, runs, True,
            ),
            (
                "new failed attempt", pr, checks,
                {"11": {**runs["11"], "run_attempt": 2}}, False,
            ),
            (
                "new failed check", pr,
                checks + [{
                    "key": "status:external",
                    "name": "external",
                    "class": "failed",
                    "state": "failure",
                    "created_at": "2026-09-19T17:00:00Z",
                }],
                runs, False,
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invocation-state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            original = path.read_bytes()
            for name, live_pr, live_checks, live_runs, current in cases:
                with self.subTest(case=name):
                    output = StringIO()
                    args = CI.build_parser().parse_args([
                        "status", "--state", str(path), "--verify-warning-snapshot",
                    ])
                    with (
                        mock.patch.object(
                            CI, "metadata_for", return_value=copy.deepcopy(live_pr)
                        ),
                        mock.patch.object(
                            CI, "fetch_rollup",
                            return_value=(pr["head_sha"], copy.deepcopy(live_checks)),
                        ),
                        mock.patch.object(
                            CI, "ci_snapshot_runs", return_value=copy.deepcopy(live_runs)
                        ),
                        mock.patch.object(
                            CI, "run", side_effect=AssertionError("external command forbidden")
                        ),
                        redirect_stdout(output),
                    ):
                        CI.command_status(args)
                    stdout = json.loads(output.getvalue())
                    canonical = json.loads(
                        CI.status_path_for(path).read_text(encoding="utf-8")
                    )
                    self.assertEqual(original, path.read_bytes())
                    self.assertEqual(
                        stdout["warning_verification"], canonical["warning_verification"]
                    )
                    for channel, payload in (("stdout", stdout), ("canonical", canonical)):
                        with self.subTest(channel=channel):
                            self.assertEqual(
                                "current" if current else "stale",
                                payload["warning_verification"]["result"],
                            )
                            self.assertEqual(
                                state["warning_snapshot_sha256"],
                                payload["warning_verification"]["expected_snapshot_sha256"],
                            )
                            self.assertIsNone(payload["clean_at_head_sha"])
                            self.assertFalse(payload["all_ci_passed"])
                            completed = subprocess.CompletedProcess(
                                [], 0, json.dumps(payload), ""
                            )
                            with mock.patch.object(
                                PIPELINE.common, "run", return_value=completed
                            ) as status_command:
                                result = PIPELINE.common.inspect_stage(
                                    PIPELINE.STAGE_BY_NAME[PIPELINE.STAGE_CI],
                                    PIPELINE.build_target("owner", "repo", 7),
                                    pr["head_sha"], pr["base_sha"],
                                    pipeline_run=pipeline_run,
                                    read_status=lambda entry, target: (
                                        PIPELINE.common.read_stage_status(
                                            entry, target,
                                            script_for=lambda _: CI_SCRIPT,
                                            state_for=lambda *_: path,
                                        )
                                    ),
                                )
                            self.assertIn(
                                "--verify-clearance-snapshot",
                                status_command.call_args.args[0],
                            )
                            self.assertEqual(current, result["clear"])
                            if current:
                                self.assertEqual("warning", payload["stage_outcome"])
                                self.assertEqual(pr["head_sha"], payload["warning_at_head_sha"])
                                self.assertEqual(pr["base_sha"], payload["warning_at_base_sha"])
                                self.assertEqual("ci_warning", result["clearance_kind"])
                                self.assertEqual(state["ci_warnings"], result["ci_warnings"])
                                self.assertFalse(result["all_ci_passed"])
                            else:
                                self.assertEqual("pending", payload["stage_outcome"])
                                self.assertIsNone(payload["outcome"])
                                self.assertIsNone(payload["warning_at_head_sha"])
                                self.assertIsNone(payload["warning_at_base_sha"])
                                self.assertEqual([], payload["ci_warnings"])
                                self.assertEqual("ci_warning_snapshot_changed", result["reason"])
                                self.assertNotIn("ci_warnings", result)
