"""Regressions for terminal summaries derived from canonical results."""

import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "pr_pipeline.py"
SPEC = importlib.util.spec_from_file_location("terminal_reporting_pipeline", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
RUN_ID = "1" * 32
HEAD = "a" * 40
BASE = "b" * 40


def observed_clean_result():
    pr = {
        **MODULE.build_target("owner", "repo", 7),
        "base_branch": "main", "base_sha": BASE, "head_branch": "feature",
        "head_sha": HEAD, "is_draft": True, "state": "OPEN", "title": "Configure targets",
    }
    stages = [
        {
            "stage": stage, "clear": True, "clear_at_head_sha": HEAD,
            "clear_at_base_sha": BASE,
            "clearance_kind": "stage_result", "installed": True,
            "outcome": "cleared", "reason": None, "status_state": "run-bound-state.json",
            "status": {
                "agent_task": None, "history": [{"detail": "diagnostic " * 2500}],
                "last_helper_activity": "2026-09-19T20:11:49Z",
            },
        }
        for stage in MODULE.STAGE_NAMES
    ]
    stages[3]["status"] = {
        "clean_at_head_sha": HEAD, "clean_at_base_sha": BASE,
        "clearance_verification": {
            "result": "current", "reason": "ci_snapshot_current",
            "expected_snapshot_sha256": "c" * 64, "observed_snapshot_sha256": "c" * 64,
        },
        "budget_scope": "pipeline", "iterations": 0, "escalation": None,
        "outcome": "green", "progress": None, "skip_note": None,
        "coordinator": {
            "base_sha": BASE, "head_sha": HEAD, "status": "ready",
            "detail": "all 102 check(s) finished without a failure",
            "snapshot_sha256": "c" * 64, "stable_polls": 2,
        },
        "run": {
            "action": None, "batch_statuses": {}, "decision": "green",
            "head_sha": HEAD, "id": "pr-7-agent-task-example",
            "iteration": 1, "outcome": "green", "reason": "all_checks_passed",
            "status": "active",
        },
    }
    runs = [
        {
            **copy.deepcopy(stage), "sweep": 1, "action": "launched",
            "stage_reason": None, "started_head_sha": HEAD, "ended_head_sha": HEAD,
            "published_commits": [], "returncode": 0, "log_path": "stage.log",
        }
        for stage in stages
    ]
    return {
        "event": "pipeline_finished", "number": 7, "result": "complete",
        "run_id": RUN_ID, "head_sha": HEAD, "pr": pr,
        "stages": stages, "runs": runs, "sweeps": 1,
    }


def warning():
    return {
        "check_key": "check:77", "name": "Integration tests", "diagnosis": "pre_existing",
        "reason": "Same failure at the base revision.",
        "evidence": ["Base run 123 failed with the same error."],
    }


class TerminalReportingTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patch = mock.patch.object(MODULE, "copilot_home", return_value=self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.target = MODULE.build_target("owner", "repo", 7)
        self.path = MODULE.run_result_path(self.target, RUN_ID)

    def summarize(self, original):
        result = MODULE.persist_terminal_result(original, self.path)
        self.assertEqual(original, json.loads(self.path.read_text(encoding="utf-8")))
        self.assertEqual(str(self.path.resolve()), result["artifacts"]["result"])
        self.assertEqual(
            hashlib.sha256(self.path.read_bytes()).hexdigest(),
            result["artifacts"]["result_sha256"],
        )
        output = StringIO()
        with redirect_stdout(output):
            MODULE.emit(result)
        self.assertLessEqual(
            len(output.getvalue().encode("utf-8")), MODULE.TERMINAL_RESULT_MAX_BYTES,
        )
        return result

    def watch(self, original):
        summary = self.summarize(original)
        result = {"final_event": summary}
        output = StringIO()
        with redirect_stdout(output):
            MODULE.emit(result)
        self.assertLessEqual(
            len(output.getvalue().encode("utf-8")), MODULE.TERMINAL_RESULT_MAX_BYTES,
        )
        return result

    def test_clean_large_controller_shape_has_direct_terminal_result(self):
        original = observed_clean_result()
        self.assertGreater(MODULE.serialized_size(original), 64886)
        result = self.watch(original)
        final = result["final_event"]
        self.assertEqual("complete", final["result"])
        self.assertTrue(final["all_ci_passed"])
        self.assertEqual(HEAD, final["head_sha"])
        self.assertEqual(BASE, final["base_sha"])
        self.assertEqual(BASE, final["pr"]["base_sha"])
        self.assertEqual(1, final["sweeps"])
        self.assertEqual([], final["published_commits"])
        self.assertEqual([], final["retained_commits"])
        self.assertEqual([], final["commit_tracking_errors"])
        self.assertEqual(5, len(final["stages"]))
        self.assertNotIn("diagnostic diagnostic", json.dumps(result))

    def test_completion_alone_stale_or_unknown_ci_never_claims_green(self):
        for kind in ("missing", "old_head", "old_base", "unknown", "warning", "revalidation"):
            with self.subTest(kind=kind):
                original = observed_clean_result()
                ci = original["stages"][3]
                if kind == "missing":
                    ci["status"] = {}
                elif kind == "old_head":
                    ci["clear_at_head_sha"] = "d" * 40
                elif kind == "old_base":
                    ci["status"]["coordinator"]["base_sha"] = "d" * 40
                elif kind == "unknown":
                    ci["status"]["run"]["decision"] = "pending"
                elif kind == "warning":
                    ci["clearance_kind"] = "ci_warning"
                else:
                    original["ci_warning_revalidation_error"] = "status_timeout"
                result = MODULE.compact_terminal_result(
                    original, result_path=self.path, result_sha256="0" * 64,
                )
                self.assertNotIn("all_ci_passed", result)

    def test_published_replacement_retained_commits_and_tracking_errors(self):
        original = observed_clean_result()
        published = {"sha": "d" * 40, "title": "Fix targets", "url": "https://example/commit/d"}
        retained = {"sha": "e" * 40, "title": "Local fix"}
        original["runs"][0].update({
            "published_commits": [published], "history_rewritten": True,
            "commit_tracking_errors": ["after snapshot unreadable"],
        })
        original["runs"][1]["retained_commits"] = [retained]
        original["retained_commits"] = [retained]
        original["local_head_sha"] = retained["sha"]
        final = self.watch(original)["final_event"]
        self.assertEqual([published], final["published_commits"])
        self.assertEqual([retained], final["retained_commits"])
        self.assertEqual(["after snapshot unreadable"], final["commit_tracking_errors"])
        self.assertTrue(final["history_rewritten"])
        self.assertEqual(retained["sha"], final["local_head_sha"])

    def test_published_review_sequence_reports_both_code_commits_not_output_commit(self):
        code_shas = [
            "1111111111111111111111111111111111111111",
            "2222222222222222222222222222222222222222",
        ]
        output_sha = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        target = MODULE.build_target("owner", "repo", 7)
        code_commits = MODULE.common.read_pr_commits(
            target,
            api=mock.Mock(return_value={"commits": [
                {"oid": code_shas[0], "messageHeadline": "First review fix"},
                {"oid": code_shas[1], "messageHeadline": "Second review fix"},
            ]}),
        )
        published, errors, rewritten = MODULE.commits_added(
            {"commits": [{"sha": HEAD}]},
            {"commits": [{"sha": HEAD}, *code_commits]},
        )
        self.assertEqual([], errors)
        self.assertFalse(rewritten)
        original = observed_clean_result()
        original["runs"][1].update({
            "published_commits": published,
            "status": {"agent_task": {
                "remote": {"ordered_commits": code_shas},
                "diagnostics": {"output_commit_sha": output_sha},
            }},
        })
        final = self.watch(original)["final_event"]
        self.assertEqual(code_commits, final["published_commits"])
        self.assertEqual(code_shas, [commit["sha"] for commit in final["published_commits"]])
        self.assertNotIn(output_sha, [commit["sha"] for commit in final["published_commits"]])
        self.assertEqual([], final["retained_commits"])
        saved = json.loads(self.path.read_bytes())
        self.assertEqual(published, saved["runs"][1]["published_commits"])
        self.assertEqual(
            code_shas, saved["runs"][1]["status"]["agent_task"]["remote"]["ordered_commits"],
        )

    def test_retained_review_sequence_reports_both_code_commits_not_output_commit(self):
        code_shas = [
            "1111111111111111111111111111111111111111",
            "2222222222222222222222222222222222222222",
        ]
        output_sha = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        repo = self.root / "synthetic-source"
        with (
            mock.patch.object(MODULE.common, "git_succeeds", return_value=True),
            mock.patch.object(MODULE.common, "git_or_none", return_value=(
                f"{code_shas[0]}\tFirst review fix\n{code_shas[1]}\tSecond review fix\n"
            )) as git,
        ):
            retained = MODULE.local_commits_between(repo, HEAD, code_shas[-1])
        git.assert_called_once_with(
            repo, "log", "--reverse", "--first-parent", "--format=%H%x09%s",
            f"{HEAD}..{code_shas[-1]}",
        )
        original = observed_clean_result()
        original.update({
            "result": "blocked", "reason": "stage_left_unpublished_commits",
            "detail": "Review fixes remain local.", "local_head_sha": code_shas[-1],
            "retained_commits": retained,
        })
        original["runs"][1].update({
            "retained_commits": retained,
            "status": {"agent_task": {
                "remote": {"ordered_commits": code_shas},
                "diagnostics": {"output_commit_sha": output_sha},
            }},
        })
        final = self.watch(original)["final_event"]
        self.assertEqual("blocked", final["result"])
        self.assertEqual("stage_left_unpublished_commits", final["reason"])
        self.assertEqual(code_shas, [commit["sha"] for commit in final["retained_commits"]])
        self.assertNotIn(output_sha, [commit["sha"] for commit in final["retained_commits"]])
        self.assertEqual([], final["published_commits"])
        saved = json.loads(self.path.read_bytes())
        self.assertEqual(retained, saved["retained_commits"])
        self.assertEqual(retained, saved["runs"][1]["retained_commits"])
        self.assertEqual(
            code_shas, saved["runs"][1]["status"]["agent_task"]["remote"]["ordered_commits"],
        )

    def test_verified_warning_is_complete_but_not_green(self):
        original = observed_clean_result()
        original.update({"ci_warnings": [warning()], "all_ci_passed": False})
        original["stages"][3]["clearance_kind"] = "ci_warning"
        original["stages"][3]["status"]["outcome"] = "warning"
        final = self.watch(original)["final_event"]
        self.assertEqual("complete", final["result"])
        self.assertFalse(final["all_ci_passed"])
        self.assertEqual([warning()], final["ci_warnings"])
        self.assertIn("WITH CI WARNINGS", MODULE.progress_transition(final)["message"])

    def test_blocked_revalidation_failure_does_not_promote_historical_warnings(self):
        original = observed_clean_result()
        original.update({
            "result": "blocked", "reason": "stage_execution_failed",
            "detail": "pr-description exited with code 1; see stage.log",
            "stage": MODULE.STAGE_DESCRIPTION,
            "ci_warning_revalidation_error": "status_timeout",
            "stage_result": {
                "stage": MODULE.STAGE_DESCRIPTION, "clear": False, "outcome": "blocked",
                "reason": "pending_validation",
                "status": {"escalation": {"reason": "review_required", "detail": "Needs review"}},
            },
        })
        original["runs"][3].update({"ci_warnings": [warning()], "all_ci_passed": False})
        final = self.watch(original)["final_event"]
        for key in ("result", "reason", "detail", "ci_warning_revalidation_error"):
            self.assertEqual(original[key], final[key])
        self.assertEqual(original["stage_result"], final["stage_result"])
        self.assertNotIn("ci_warnings", final)
        self.assertNotIn("all_ci_passed", final)

    def test_ci_timeout_keeps_observed_and_frozen_context_separate(self):
        old_head = "d" * 40
        escalation = {
            "reason": "timeout", "detail": "timed out waiting for stable terminal CI",
            "head_sha": HEAD, "base_sha": BASE, "check_context": "last_observed",
            "checks": ["check:CI/test"], "pending_checks": ["check:CI/running"],
            "aggregate_checks": ["check:CI/aggregate"],
            "check_snapshot": {
                "head_sha": HEAD, "observed_at": "2026-09-20T07:48:00Z",
                "sha256": "e" * 64, "decision": {"decision": "failures"},
                "workflow_runs": {"42": {"run_attempt": 2, "status": "in_progress"}},
            },
            "frozen_run": {
                "head_sha": old_head, "published_head_sha": HEAD,
                "decision": {"decision": "failures", "checks": ["check:CI/spotless"]},
            },
        }
        for context in ("last_observed", "unavailable"):
            with self.subTest(context=context), mock.patch.object(
                MODULE, "copilot_home", return_value=self.root / context,
            ):
                self.path = MODULE.run_result_path(self.target, RUN_ID)
                diagnostic = copy.deepcopy(escalation)
                if context == "unavailable":
                    diagnostic.update({
                        "check_context": context, "check_snapshot": None,
                        "checks": [], "pending_checks": [], "aggregate_checks": [],
                    })
                status = MODULE.common.stage_status_summary({
                    "escalation": diagnostic, "outcome": None,
                    "coordinator": {"head_sha": HEAD, "status": "blocked"},
                    "run": {"head_sha": old_head, "status": "published"},
                })
                original = observed_clean_result()
                original.update({
                    "result": "blocked", "reason": "stage_execution_failed",
                    "detail": "ci-fix-loop exited with code 1", "stage": MODULE.STAGE_CI,
                    "stage_result": {
                        "stage": MODULE.STAGE_CI, "clear": False, "outcome": "escalated",
                        "status": status,
                    },
                })
                original["stages"][3] = original["stage_result"]
                final = self.watch(original)["final_event"]
                self.assertEqual("blocked", final["result"])
                self.assertFalse(final["stage_result"]["clear"])
                preview = final["stage_result"]["status"]["escalation"]
                for field in (
                    "head_sha", "base_sha", "check_context", "checks",
                    "pending_checks", "aggregate_checks",
                ):
                    self.assertEqual(diagnostic[field], preview[field])
                self.assertEqual(old_head, preview["frozen_run"]["head_sha"])
                self.assertTrue(final["stage_result_details_truncated"])
                saved = json.loads(self.path.read_bytes())
                self.assertEqual(diagnostic, saved["stage_result"]["status"]["escalation"])
                self.assertNotIn("all_ci_passed", final)

    def test_many_long_warning_check_and_commit_details_have_explicit_flags(self):
        original = observed_clean_result()
        long_text = "\u96ea\U0001f680" * 2000
        original.update({
            "ci_warnings": [
                {**warning(), "name": long_text, "reason": long_text,
                 "evidence": [long_text] * 30}
                for _ in range(80)
            ],
            "all_ci_passed": False,
            "published_commits": [{"sha": str(i), "title": long_text} for i in range(80)],
            "retained_commits": [{"sha": str(i), "title": long_text} for i in range(90)],
            "commit_tracking_errors": [long_text] * 30,
        })
        original["stages"][3]["status"]["run"]["checks"] = [
            {"name": long_text, "required": False, "aggregate": True, "conclusion": "failure"}
            for _ in range(80)
        ]
        final = self.watch(original)["final_event"]
        self.assertEqual("complete", final["result"])
        self.assertFalse(final["all_ci_passed"])
        for key in ("ci_status", "ci_warnings", "published_commits", "retained_commits",
                    "commit_tracking_errors"):
            self.assertTrue(final.get(f"{key}_omitted") or final.get(f"{key}_details_truncated"))
        self.assertEqual(80, len(json.loads(self.path.read_text())["ci_warnings"]))

    def test_blocked_long_checks_keep_safety_and_underlying_reason_or_retrieval_flags(self):
        original = observed_clean_result()
        original.update({
            "result": "incomplete", "reason": "stages_not_clear",
            "stage_result": {
                "stage": MODULE.STAGE_CI, "clear": False, "reason": "unknown_failures",
                "outcome": "blocked",
                "status": {"run": {"checks": [{"name": "x" * 4000, "required": False,
                                             "aggregate": True, "conclusion": "failure"}] * 50}},
            },
        })
        original["stages"][3] = original["stage_result"]
        final = self.watch(original)["final_event"]
        self.assertEqual("incomplete", final["result"])
        self.assertEqual("stages_not_clear", final["reason"])
        self.assertNotIn("all_ci_passed", final)
        self.assertTrue(final.get("stage_result_omitted") or final.get("stage_result_details_truncated"))

    def test_error_and_interruption_are_saved_before_terminal_emission(self):
        for message in ("broken", "interrupted"):
            with self.subTest(message=message):
                path = self.root / message / "result.json"
                args = MODULE.build_parser().parse_args(["run"])
                args.run_id = RUN_ID
                args.result_path = path
                output = StringIO()
                with redirect_stdout(output):
                    MODULE.report_run_error(args, message)
                final = json.loads(output.getvalue())
                self.assertEqual(message, final["error"])
                self.assertEqual("error", final["result"])
                self.assertEqual(message, json.loads(path.read_text())["error"])

    def test_long_blocked_error_and_revalidation_text_requires_exact_artifact(self):
        original = observed_clean_result()
        original.update({
            "result": "blocked", "reason": "unknown_failure",
            "detail": "\u96ea" * 10000, "error": "\U0001f680" * 10000,
            "ci_warning_revalidation_error": "snapshot unavailable " * 10000,
        })
        final = self.watch(original)["final_event"]
        self.assertEqual("blocked", final["result"])
        self.assertEqual("unknown_failure", final["reason"])
        self.assertNotIn("all_ci_passed", final)
        for key in ("detail", "error", "ci_warning_revalidation_error"):
            self.assertTrue(final.get(f"{key}_omitted") or final.get(f"{key}_details_truncated"))

    def test_small_nonrequired_aggregate_failure_remains_visible(self):
        original = observed_clean_result()
        check = {"name": "All checks", "required": False, "aggregate": True, "conclusion": "failure"}
        stage = {
            "stage": MODULE.STAGE_CI, "clear": False, "reason": "unknown_failures",
            "outcome": "blocked", "status": {"run": {"checks": [check]}},
        }
        original.update({
            "result": "blocked", "reason": "stage_execution_failed", "detail": "CI exited 1",
            "stage_result": stage,
        })
        original["stages"][3] = stage
        final = self.watch(original)["final_event"]
        self.assertEqual([check], final["stage_result"]["status"]["run"]["checks"])
        self.assertEqual("unknown_failures", final["stage_result"]["reason"])

    def test_native_cli_emission_includes_platform_newline_in_byte_bound(self):
        result = self.watch(observed_clean_result())
        path = self.root / "watch.json"
        path.write_text(json.dumps(result), encoding="utf-8")
        process = subprocess.run(
            [
                sys.executable, "-c",
                "import json,sys; from pathlib import Path; "
                "print(json.dumps(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')), "
                "sort_keys=True), flush=True)",
                str(path),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(MODULE.serialized_size(result), len(process.stdout))
        self.assertLessEqual(len(process.stdout), MODULE.TERMINAL_RESULT_MAX_BYTES)

    def test_artifact_failure_never_emits_success(self):
        reporter = MODULE.ProgressReporter(
            target=self.target, result_path=self.path,
        )
        with mock.patch.object(MODULE.common, "write_json_atomically",
                               side_effect=OSError("disk full")), mock.patch.object(
                                   reporter, "output") as output:
            with self.assertRaisesRegex(OSError, "disk full"):
                reporter(observed_clean_result())
        output.assert_not_called()

    def test_existing_result_is_immutable(self):
        original = observed_clean_result()
        first = self.summarize(original)
        self.assertEqual(first, self.summarize(original))
        changed = {**original, "result": "blocked"}
        with self.assertRaisesRegex(MODULE.WorkflowError, "different content"):
            MODULE.persist_terminal_result(changed, self.path)
        self.assertEqual(original, json.loads(self.path.read_text()))
