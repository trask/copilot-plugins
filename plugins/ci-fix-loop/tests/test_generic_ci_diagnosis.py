import contextlib
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "generic_ci_diagnosis", Path(__file__).parents[1] / "scripts" / "ci_fix_loop.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class GenericCiDiagnosisTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.path = self.root / "state.json"
        self.pr = {
            "repo_name": "owner/repo", "upstream_owner": "owner", "upstream_repo": "repo",
            "head_sha": "a" * 40, "base_sha": "b" * 40,
            "pr_url": "https://github.com/owner/repo/pull/7",
            "number": 7, "title": "Example", "head_branch": "topic", "base_branch": "main",
        }
        self.run = {
            "id": 11, "workflow_id": 22, "name": "Build",
            "head_sha": self.pr["head_sha"], "run_attempt": 1,
            "status": "completed", "conclusion": "failure",
        }
        self.checks = [{
            "key": "check:Build/test", "name": "test", "workflow": "Build",
            "workflow_run_id": 11, "class": "failed",
        }, {
            "key": "check:Build/lint", "name": "lint", "workflow": "Build",
            "workflow_run_id": 11, "class": "failed",
        }]
        self.preflight = {"pr": self.pr, "check_snapshot": {
            "failures": self.checks,
            "rollup": self.checks,
            "workflow_runs": {"11": self.run},
        }}
        self.remote = {
            "commits": [],
            "candidate_manifest": {"artifact_commit": {
                "sha": "c" * 40, "changed_paths": [MODULE.CI_DIAGNOSIS_PATH],
            }},
        }
        self.entries = [{
            "check_key": check["key"], "diagnosis": "transient",
            "reason": "Artifact service returned HTTP 503",
            "evidence": ["Download failed with HTTP status 503 in the supplied log"],
        } for check in self.checks]
        MODULE.save_state(self.path, {
            "version": MODULE.STATE_VERSION, "pr": self.pr, "history": [],
            "iterations": 1, "reruns": {},
        })

    def diagnosis(self, entries=None):
        with mock.patch.object(
            MODULE, "fetch_committed_text",
            return_value=json.dumps({"diagnoses": self.entries if entries is None else entries}),
        ) as read:
            result = MODULE.read_ci_diagnosis(self.preflight, self.remote)
        return result, read

    def retry(self, *, live=None, permissions=None, error=None, policy="allow", metadata=None):
        with (
            mock.patch.object(MODULE, "publication_lock", return_value=contextlib.nullcontext()),
            mock.patch.object(MODULE, "metadata_for", side_effect=metadata, return_value=self.pr),
            mock.patch.object(
                MODULE, "ci_run_identity", side_effect=live,
                return_value=self.run,
            ),
            mock.patch.object(
                MODULE, "gh_json",
                return_value={"permissions": permissions or {"push": True}},
            ) as api,
            mock.patch.object(MODULE, "rerun_failed_jobs", side_effect=error) as rerun,
            mock.patch.object(MODULE, "publish_empty_rerun_commit") as empty,
            mock.patch.object(MODULE, "ACTIVE_GITHUB_MUTATION_POLICY", policy),
        ):
            try:
                result = MODULE.retry_diagnosed_ci(
                    self.path, self.preflight, [entry["key"] for entry in self.checks],
                )
            finally:
                empty.assert_not_called()
        return result, rerun, api

    def test_recommendations_are_bound_to_verified_output_commit_and_frozen_failures(self):
        entries, read = self.diagnosis()
        self.assertEqual(["test", "lint"], [entry["name"] for entry in entries])
        read.assert_called_once_with(
            "owner/repo", MODULE.CI_DIAGNOSIS_PATH, "c" * 40,
            description="hosted CI diagnosis",
        )

    def test_diagnosis_includes_aggregate_failures_even_when_fix_selection_omits_them(self):
        repository = self.root / "repo"
        repository.mkdir()
        pr = {**self.pr, "state": "OPEN", "head_owner": "owner", "head_repo": "repo"}
        checks = [
            {**self.checks[0], "kind": "check_run", "conclusion": "FAILURE"},
            {
                **self.checks[0], "key": "check:Build/required-status-check",
                "name": "required-status-check", "kind": "check_run", "conclusion": "FAILURE",
            },
        ]
        with (
            mock.patch.object(MODULE, "git", return_value=""),
            mock.patch.object(MODULE, "metadata_for", return_value=pr),
            mock.patch.object(MODULE, "checkout_pr"),
            mock.patch.object(MODULE, "local_identity", return_value={
                "branch": "topic", "head": self.pr["head_sha"], "status": "",
            }),
            mock.patch.object(MODULE, "require_fork_head"),
            mock.patch.object(MODULE, "find_push_remote"),
            mock.patch.object(MODULE, "gh_json", side_effect=[
                {"permissions": dict.fromkeys(("admin", "maintain", "push", "triage", "pull"), True)},
                {"login": "viewer"},
            ]),
            mock.patch.object(MODULE, "fetch_rollup", return_value=(self.pr["head_sha"], checks)),
            mock.patch.object(MODULE, "decide", return_value={
                "decision": "failures", "checks": [checks[0]["key"]], "detail": "failed",
            }),
            mock.patch.object(MODULE, "baseline_conclusions", return_value={}),
            mock.patch.object(MODULE, "ci_run_identity", return_value=self.run),
            mock.patch.object(MODULE, "fetch_failed_check_log", return_value="failure") as log,
        ):
            preflight = MODULE.agent_task_preflight(
                repository, MODULE.parse_target(self.pr["pr_url"]), state_path=self.path,
            )
            MODULE.require_live_check_snapshot(preflight)
        self.assertEqual([check["key"] for check in checks], [
            check["key"] for check in preflight["check_snapshot"]["failures"]
        ])
        self.assertEqual(2, log.call_count)

    def test_optional_advice_cannot_invalidate_candidate_code(self):
        self.remote["commits"] = ["d" * 40]
        with mock.patch.object(MODULE, "fetch_committed_text") as read:
            self.assertIsNone(MODULE.read_ci_diagnosis(self.preflight, self.remote))
        read.assert_not_called()

    def test_missing_advice_does_not_authorize_any_action(self):
        for artifact in (None, {"changed_paths": [MODULE.AGENT_TASK_OUTPUT_REPORT]}):
            with self.subTest(artifact=artifact):
                self.remote["candidate_manifest"]["artifact_commit"] = artifact
                with mock.patch.object(MODULE, "fetch_committed_text") as read:
                    self.assertIsNone(MODULE.read_ci_diagnosis(self.preflight, self.remote))
                read.assert_not_called()

    def test_incomplete_duplicate_unknown_or_empty_recommendations_fail_closed(self):
        malformed = [
            [], self.entries[:1], [self.entries[0], self.entries[0]],
            [{**self.entries[0], "check_key": "unknown"}, self.entries[1]],
            [{**self.entries[0], "diagnosis": "green"}, self.entries[1]],
            [{**self.entries[0], "reason": " "}, self.entries[1]],
            [{**self.entries[0], "evidence": []}, self.entries[1]],
            [{**self.entries[0], "evidence": [" "]}, self.entries[1]],
            [{**self.entries[0], "command": "gh run rerun"}, self.entries[1]],
        ]
        for entries in malformed:
            with self.subTest(entries=entries), self.assertRaises(MODULE.WorkflowError):
                self.diagnosis(entries)
        self.preflight["check_snapshot"]["failures"] = []
        with self.assertRaises(MODULE.WorkflowError):
            self.diagnosis([])

    def test_invalid_json_or_oversized_advice_cannot_authorize_a_retry(self):
        for content in ("{", "x" * 32769, '{"diagnoses":[],"diagnoses":[]}'):
            with (
                self.subTest(content=content[:20]),
                mock.patch.object(MODULE, "fetch_committed_text", return_value=content),
                self.assertRaises(MODULE.WorkflowError),
            ):
                MODULE.read_ci_diagnosis(self.preflight, self.remote)

    def test_one_workflow_request_covers_multiple_failed_jobs(self):
        result, rerun, _ = self.retry()
        self.assertEqual("rerun_requested", result)
        rerun.assert_called_once_with(self.pr, 11)
        record = MODULE.load_state(self.path)["ci_retries"][f"{self.pr['head_sha']}:11"]
        self.assertEqual("requested", record["status"])
        self.assertEqual(1, record["source_attempt"])
        result, rerun, _ = self.retry()
        self.assertEqual("observing_retry", result)
        rerun.assert_not_called()

    def test_request_is_recorded_before_the_side_effect(self):
        def request(_pr, _run):
            record = MODULE.load_state(self.path)["ci_retries"][f"{self.pr['head_sha']}:11"]
            self.assertEqual("requesting", record["status"])
        self.retry(error=request)

    def test_external_retry_advancement_or_running_status_is_observed_without_post(self):
        for changed in (
            {**self.run, "run_attempt": 2},
            {**self.run, "status": "in_progress", "conclusion": None},
            {**self.run, "status": "queued", "conclusion": None},
            {**self.run, "conclusion": "success"},
        ):
            with self.subTest(changed=changed):
                result, rerun, _ = self.retry(live=[changed])
                self.assertEqual("observing_retry", result)
                rerun.assert_not_called()

    def test_external_attempts_consume_the_same_retry_allowance(self):
        self.run["run_attempt"] = 2
        with self.assertRaisesRegex(MODULE.WorkflowError, "including external attempts"):
            self.retry()
        self.assertFalse(MODULE.load_state(self.path).get("ci_retries"))

    def test_completed_requested_retry_cannot_wait_forever_or_request_again(self):
        self.retry()
        self.run["run_attempt"] = 2
        with self.assertRaisesRegex(MODULE.WorkflowError, "retry completed without clearing"):
            self.retry()
        record = MODULE.load_state(self.path)["ci_retries"][f"{self.pr['head_sha']}:11"]
        self.assertEqual("completed", record["status"])
        self.assertEqual(2, record["completed_attempt"])

    def test_source_only_or_missing_write_permission_blocks_without_empty_commit(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "source-only"):
            self.retry(policy="source-only")
        with self.assertRaisesRegex(MODULE.WorkflowError, "write permission"):
            self.retry(permissions={"push": False, "maintain": False, "admin": False})
        self.assertFalse(MODULE.load_state(self.path).get("ci_retries"))

    def test_changed_workflow_or_regressed_attempt_fails_closed(self):
        for changed in (
            {**self.run, "workflow_id": 99},
            {**self.run, "run_attempt": 0},
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                MODULE.WorkflowError, "identity changed",
            ):
                self.retry(live=[changed])

    def test_head_or_base_drift_immediately_before_request_blocks_it(self):
        for field in ("head_sha", "base_sha"):
            with self.subTest(field=field), self.assertRaisesRegex(
                MODULE.WorkflowError, "drifted",
            ):
                self.retry(metadata=[self.pr, {**self.pr, field: "f" * 40}])
        self.assertFalse(MODULE.load_state(self.path).get("ci_retries"))

    def test_last_moment_retry_race_does_not_send_another_request(self):
        result, rerun, _ = self.retry(live=[
            self.run, {**self.run, "status": "in_progress"},
        ])
        self.assertEqual("observing_retry", result)
        rerun.assert_not_called()

    def test_permission_denial_is_reported_without_a_source_workaround(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "no source commit"):
            self.retry(error=MODULE.RerunPermissionDenied("HTTP 403"))
        record = MODULE.load_state(self.path)["ci_retries"][f"{self.pr['head_sha']}:11"]
        self.assertEqual("request_failed", record["status"])
        with self.assertRaisesRegex(MODULE.WorkflowError, "already ended"):
            self.retry()

    def test_lost_response_observes_attempt_advancement_without_duplicate_post(self):
        result, rerun, _ = self.retry(
            live=[self.run, self.run, {**self.run, "run_attempt": 2}],
            error=MODULE.WorkflowError("connection lost"),
        )
        self.assertEqual("observing_retry", result)
        rerun.assert_called_once()
        record = MODULE.load_state(self.path)["ci_retries"][f"{self.pr['head_sha']}:11"]
        self.assertEqual("retry_observed", record["status"])

    def test_run_identity_requires_repository_head_workflow_and_attempt(self):
        payload = {**self.run, "repository": {"full_name": "owner/repo"}}
        for field, value in (
            ("head_sha", "f" * 40), ("run_attempt", None), ("run_attempt", True),
            ("workflow_id", None), ("id", 99), ("repository", {"full_name": "other/repo"}),
            ("status", "surprise"),
        ):
            with (
                self.subTest(field=field, value=value),
                mock.patch.object(
                    MODULE, "fetch_workflow_run", return_value={**payload, field: value},
                ),
                self.assertRaises(MODULE.WorkflowError),
            ):
                MODULE.ci_run_identity(self.pr, 11)
        with mock.patch.object(MODULE, "fetch_workflow_run", return_value=payload):
            self.assertEqual(self.run, MODULE.ci_run_identity(self.pr, 11))

    def test_run_attempt_is_part_of_the_failed_snapshot_identity(self):
        changed = copy.deepcopy(self.preflight["check_snapshot"])
        changed["workflow_runs"]["11"]["run_attempt"] = 2
        self.assertNotEqual(
            MODULE.check_snapshot_sha256(self.preflight["check_snapshot"]),
            MODULE.check_snapshot_sha256(changed),
        )

    def warning_state(self):
        state = MODULE.load_state(self.path)
        state.update({
            "outcome": "warning", "clean_at_head_sha": None,
            "warning_at_head_sha": self.pr["head_sha"],
            "warning_at_base_sha": self.pr["base_sha"],
            "warning_snapshot_sha256": MODULE.ci_warning_snapshot_sha256(
                self.pr, self.checks, {"11": self.run},
            ),
            "ci_warnings": [{**self.entries[0], "diagnosis": "unrelated", "name": "test"}],
        })
        return state

    def test_warning_verification_ignores_order_and_description_edits(self):
        state = self.warning_state()
        with (
            mock.patch.object(MODULE, "gh_json", return_value=[{"workflow_runs": []}]),
            mock.patch.object(
                MODULE, "metadata_for",
                return_value={**self.pr, "title": "Updated description", "body": "New body"},
            ),
            mock.patch.object(MODULE, "fetch_rollup", return_value=(self.pr["head_sha"], self.checks[::-1])),
            mock.patch.object(MODULE, "ci_run_identity", return_value=self.run),
        ):
            fields = MODULE.verify_ci_warning_snapshot(state)
        self.assertEqual("current", fields["warning_verification"]["result"])
        self.assertNotIn("stage_outcome", fields)

    def test_new_changed_pending_or_removed_checks_invalidate_warnings(self):
        state = self.warning_state()
        snapshots = [
            [*self.checks, {**self.checks[0], "name": "new failure"}],
            self.checks[:1],
            [{**self.checks[0], "class": "pending", "status": "IN_PROGRESS"}, self.checks[1]],
            [{**self.checks[0], "url": "https://github.com/owner/repo/actions/runs/11/job/99"}, self.checks[1]],
            [{**check, "class": "passed", "conclusion": "SUCCESS"} for check in self.checks],
        ]
        for checks in snapshots:
            with (
                mock.patch.object(MODULE, "gh_json", return_value=[{"workflow_runs": []}]),
                self.subTest(checks=checks),
                mock.patch.object(MODULE, "metadata_for", return_value=self.pr),
                mock.patch.object(MODULE, "fetch_rollup", return_value=(self.pr["head_sha"], checks)),
                mock.patch.object(MODULE, "ci_run_identity", return_value=self.run),
            ):
                fields = MODULE.verify_ci_warning_snapshot(state)
            self.assertEqual("stale", fields["warning_verification"]["result"])
            self.assertEqual("pending", fields["stage_outcome"])
            self.assertIsNone(fields["clean_at_head_sha"])
            self.assertIsNone(fields["warning_at_head_sha"])
            self.assertEqual([], fields["ci_warnings"])
            self.assertEqual("warning", state["outcome"])

    def test_new_attempt_invalidates_warning_even_with_identical_check_rollup(self):
        state = self.warning_state()
        for live in (
            {**self.run, "run_attempt": 2},
            {**self.run, "status": "queued", "conclusion": None},
        ):
            with (
                mock.patch.object(MODULE, "gh_json", return_value=[{"workflow_runs": []}]),
                self.subTest(live=live),
                mock.patch.object(MODULE, "metadata_for", return_value=self.pr),
                mock.patch.object(MODULE, "fetch_rollup", return_value=(self.pr["head_sha"], self.checks)),
                mock.patch.object(MODULE, "ci_run_identity", return_value=live),
            ):
                self.assertEqual(
                    "stale", MODULE.verify_ci_warning_snapshot(state)["warning_verification"]["result"],
                )

    def test_warning_verification_read_errors_never_become_current(self):
        state = self.warning_state()
        with (
            mock.patch.object(MODULE, "metadata_for", side_effect=MODULE.WorkflowError("API unavailable")),
            self.assertRaisesRegex(MODULE.WorkflowError, "API unavailable"),
        ):
            MODULE.verify_ci_warning_snapshot(state)
        state.pop("warning_snapshot_sha256")
        with self.assertRaisesRegex(MODULE.WorkflowError, "no frozen check snapshot"):
            MODULE.verify_ci_warning_snapshot(state)

    def test_verified_status_reports_same_stale_result_without_mutating_saved_state(self):
        MODULE.save_state(self.path, self.warning_state())
        original = self.path.read_bytes()
        args = MODULE.build_parser().parse_args([
            "status", "--state", str(self.path), "--verify-warning-snapshot",
        ])
        with (
            mock.patch.object(MODULE, "gh_json", return_value=[{"workflow_runs": []}]),
            mock.patch.object(MODULE, "metadata_for", return_value=self.pr),
            mock.patch.object(MODULE, "fetch_rollup", return_value=(self.pr["head_sha"], [])),
            mock.patch.object(MODULE, "emit") as emit,
        ):
            MODULE.command_status(args)
        self.assertEqual(original, self.path.read_bytes())
        compact = emit.call_args.args[0]
        report = json.loads(MODULE.status_path_for(self.path).read_text(encoding="utf-8"))
        self.assertEqual("pending", compact["stage_outcome"])
        self.assertEqual("pending", report["stage_outcome"])
        self.assertEqual(compact["warning_verification"], report["warning_verification"])
        self.assertEqual([], compact["ci_warnings"])
        self.assertEqual([], report["ci_warnings"])

    def test_unrelated_warning_never_claims_a_clean_head_or_green(self):
        state = {
            "pr": self.pr, "outcome": "warning", "clean_at_head_sha": None,
            "warning_at_head_sha": self.pr["head_sha"],
            "warning_at_base_sha": self.pr["base_sha"],
            "ci_warnings": [{**self.entries[0], "diagnosis": "unrelated", "name": "test"}],
        }
        fields = MODULE.stage_outcome_fields(state)
        self.assertEqual("warning", fields["stage_outcome"])
        self.assertFalse(fields["all_ci_passed"])
        for field in ("warning_at_head_sha", "warning_at_base_sha"):
            changed = {**state, field: "f" * 40}
            self.assertIsNone(MODULE.stage_outcome(changed))
        self.assertIsNone(MODULE.stage_outcome({**state, "ci_warnings": []}))

    def test_ci_churn_during_readonly_summary_is_not_a_mutation_claim(self):
        before = {"pull_request": "same", "checks": "old", "reviews": "same"}
        self.assertTrue(MODULE.same_triage_github_state(before, {**before, "checks": "new"}))
        self.assertFalse(MODULE.same_triage_github_state(before, {**before, "reviews": "new"}))
        self.assertFalse(MODULE.same_triage_github_state(before, None))

    def test_controller_observes_after_recommendation_and_keeps_one_wait_budget(self):
        repository = self.root / "repo"
        repository.mkdir()
        args = MODULE.build_parser().parse_args([
            "loop", self.pr["pr_url"], "--repo-root", str(repository),
            "--state", str(self.path), "--wait-timeout", "60",
        ])
        results = [
            {
                "result": "rerun", "task": {"id": "task-1"},
                "attestation": "dispatcher_candidate",
                "action_checks": [entry["key"] for entry in self.checks],
            },
            {"result": "ci_changed", "task": {"id": "task-2"}},
            {"result": "green", "state": str(self.path)},
        ]

        def iteration(_args):
            MODULE.emit(results.pop(0))

        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=repository),
            mock.patch.object(MODULE, "resolve_target", return_value=self.pr),
            mock.patch.object(
                MODULE, "wait_for_stable_ci_preflight", return_value=self.preflight,
            ) as observe,
            mock.patch.object(MODULE, "command_agent_task", side_effect=iteration),
            mock.patch.object(MODULE, "record_processed_ci_snapshot") as record,
            mock.patch.object(
                MODULE, "retry_diagnosed_ci", return_value="observing_retry",
            ) as retry,
            mock.patch.object(MODULE.time, "monotonic", side_effect=[0, 10, 10, 30, 30, 35]),
            mock.patch.object(MODULE, "emit") as emit,
        ):
            # capture_command requires emit to append to its active capture.
            emit.side_effect = lambda value: (
                MODULE._EMIT_CAPTURE_STACK[-1].append(value)
                if MODULE._EMIT_CAPTURE_STACK else None
            )
            MODULE.command_loop(args)
        self.assertEqual([60, 50, 30], [
            call.args[0].wait_timeout for call in observe.call_args_list
        ])
        self.assertEqual(2, record.call_count)
        retry.assert_called_once_with(
            self.path, self.preflight, [entry["key"] for entry in self.checks],
        )
        self.assertEqual("green", emit.call_args.args[0]["result"])


if __name__ == "__main__":
    unittest.main()
