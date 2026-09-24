import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import test_pr_conflict_resolver as existing
import test_pipeline_sweeps as sweeps
import test_sequential_stack as stack_tests
import test_stack_publication as stack_auth


MODULE = existing.MODULE
CLOUD = existing.CLOUD_MODULE


class BoundedPipelineTest(unittest.TestCase):
    def setUp(self):
        fixture = sweeps.ConflictPipelineSweepTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.args = fixture.args
        self.root = fixture.root
        self.path = fixture.path
        self.metadata = fixture.metadata
        self.calls = fixture.calls
        self.args.bounded_step = True
        self.session = mock.patch.dict(
            os.environ, {"COPILOT_AGENT_SESSION_ID": "session-1"},
        )
        self.session.start()
        self.addCleanup(self.session.stop)
        self.request = existing.ManagedConflictCoordinatorTest().request()
        self.metadata.update(mergeable="CONFLICTING", head_sha="b" * 40, base_sha="a" * 40)
        self.calls["conflict_preflight"].side_effect = lambda *a, **kw: {
            "already_mergeable": False, "pr": copy.deepcopy(self.metadata),
            "strategy": "merge", "request": copy.deepcopy(self.request),
        }
        self.patch("build_conflict_prompt", return_value="Resolve the conflict")
        self.patch("discover_conflict_task", return_value=self.root / "cloud_conflict_task.py")
        self.patch("verify_quarantined_result")
        self.patch("require_live_conflict_guards")
        self.published = self.patch("publish_conflict_result", side_effect=self.publish)
        self.dispatches = 0
        self.observations = 0
        self.patch("run", side_effect=self.helper)

    def patch(self, name, **kwargs):
        patch = mock.patch.object(MODULE, name, **kwargs)
        value = patch.start()
        self.addCleanup(patch.stop)
        return value

    def publish(self, path, state):
        state["agent_task"]["status"] = "completed"
        state["last_result"] = "published"
        MODULE.save_state(path, state)
        return {"result": "published", "state": str(path), "stage_outcome": "completed"}

    def helper(self, command, **kwargs):
        phase = command[command.index("--bounded-phase") + 1]
        self.assertNotIn("--bounded-deadline", command)
        result_path = Path(command[command.index("--result-file") + 1])
        receipt_path = result_path.with_name(result_path.name + ".bounded-receipt.json")
        result = CLOUD.Result(
            status="waiting", model=self.request["model"],
            repository=self.request["repository"], strategy=self.request["strategy"],
            request_id=self.request["request_id"],
            request_sha256=self.request["request_sha256"],
            pull_request=self.request["pull_request"],
            task_id="task-1", task_state="queued",
            task_url="https://github.com/owner/repo/tasks/task-1",
            task_base_ref="b" * 40, task_base_sha="b" * 40,
        ).as_dict()
        if phase == "dispatch":
            self.dispatches += 1
        elif phase == "observe":
            self.observations += 1
            result["task"]["state"] = (
                "in_progress" if self.observations == 1 else "completed"
            )
        else:
            result = existing.ManagedConflictCoordinatorTest().success_result(self.request)
        receipt = {
            "session": "session-1", "request_id": self.request["request_id"],
            "request_sha256": self.request["request_sha256"],
            "repository": self.request["repository"], "model": self.request["model"],
            "strategy": self.request["strategy"],
            "status": "completed" if result["task"]["state"] == "completed" else "active",
            "task": {"id": "task-1", "state": result["task"]["state"]},
        }
        if self.request["strategy"] == "native-stack":
            receipt["member_index"] = 0
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        result_path.write_text(json.dumps(result), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_remote_work_across_long_intervals_uses_one_dispatch(self):
        for elapsed in (0, 65, 130, 3600):
            with self.subTest(elapsed=elapsed):
                with mock.patch.object(MODULE.time, "monotonic", return_value=elapsed):
                    self.assertEqual(0, MODULE.command_pipeline(self.args))
                state = MODULE.load_state(self.path)
                if elapsed < 3600:
                    self.assertEqual("running", state["agent_task"]["status"])
                    self.assertEqual(1, state["managed_attempts"])
        self.assertEqual("completed", MODULE.load_state(self.path)["agent_task"]["status"])
        self.assertEqual(1, self.dispatches)
        self.assertEqual(2, self.observations)
        self.published.assert_called_once()
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        self.assertEqual(1, self.dispatches)

    def test_native_stack_preparation_finishes_before_dispatch(self):
        self.request["strategy"] = "native-stack"
        self.request["native_stack"] = {"members": [{"pr_number": 6}]}
        self.request["request_sha256"] = MODULE.request_digest(self.request)
        self.calls["conflict_preflight"].side_effect = lambda *a, **kw: {
            "already_mergeable": False, "pr": copy.deepcopy(self.metadata),
            "strategy": "native-stack", "request": copy.deepcopy(self.request),
        }
        self.patch(
            "authorize_resolver_native_stack",
            return_value={"request_id": "stack-1"},
        )
        guard = self.patch("require_live_conflict_guards")
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        state = MODULE.load_state(self.path)
        self.assertTrue(state["bounded_pipeline"]["prepared_dispatch"])
        self.assertFalse(state["bounded_pipeline"]["inflight"])
        self.assertEqual("dispatch", state["bounded_pipeline"]["phase"])
        self.assertEqual(0, self.dispatches)
        guard.assert_not_called()
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        self.assertEqual(1, self.dispatches)
        guard.assert_called_once()
        self.assertEqual(1, self.calls["conflict_preflight"].call_count)
        self.assertFalse(MODULE.load_state(self.path)["bounded_pipeline"]["prepared_dispatch"])

    def test_native_stack_preparation_can_finish_after_an_hour(self):
        self.request["strategy"] = "native-stack"
        self.request["native_stack"] = {"members": [{"pr_number": 6}]}
        self.request["request_sha256"] = MODULE.request_digest(self.request)
        clock = [10.0]

        def preflight(*_args, **_kwargs):
            clock[0] += 3600
            return {
                "already_mergeable": False,
                "pr": copy.deepcopy(self.metadata),
                "strategy": "native-stack",
                "request": copy.deepcopy(self.request),
            }

        self.calls["conflict_preflight"].side_effect = preflight
        self.patch(
            "authorize_resolver_native_stack", return_value={"request_id": "stack-1"},
        )
        with mock.patch.object(MODULE.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(0, MODULE.command_pipeline(self.args))
        state = MODULE.load_state(self.path)
        self.assertTrue(state["bounded_pipeline"]["prepared_dispatch"])
        self.assertEqual("running", state["agent_task"]["status"])
        self.assertEqual(0, self.dispatches)

    def test_unprepared_stack_dispatch_cannot_be_adopted(self):
        self.request["strategy"] = "native-stack"
        self.request["native_stack"] = {"members": [{"pr_number": 6}]}
        self.request["request_sha256"] = MODULE.request_digest(self.request)
        self.calls["conflict_preflight"].side_effect = lambda *a, **kw: {
            "already_mergeable": False, "pr": copy.deepcopy(self.metadata),
            "strategy": "native-stack", "request": copy.deepcopy(self.request),
        }
        self.patch(
            "authorize_resolver_native_stack",
            return_value={"request_id": "stack-1"},
        )
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        state = MODULE.load_state(self.path)
        state["bounded_pipeline"]["prepared_dispatch"] = False
        MODULE.save_state(self.path, state)
        with self.assertRaisesRegex(MODULE.WorkflowError, "cannot be adopted"):
            MODULE.command_pipeline(self.args)
        self.assertEqual(0, self.dispatches)

    def test_wrong_session_run_and_options_do_not_observe_or_dispatch(self):
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        before = self.path.read_bytes()
        for key, value in (
            ("pipeline_run", "another-run"), ("strategy", "rebase"),
            ("max_iterations", 4), ("pipeline_iteration", 2),
        ):
            old = getattr(self.args, key)
            setattr(self.args, key, value)
            with self.subTest(option=key), self.assertRaises(MODULE.WorkflowError):
                MODULE.command_pipeline(self.args)
            setattr(self.args, key, old)
        with mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": "session-2"}):
            with self.assertRaises(MODULE.WorkflowError):
                MODULE.command_pipeline(self.args)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(1, self.dispatches)
        self.assertEqual(0, self.observations)

    def test_ambiguous_dispatch_never_reposts(self):
        def ambiguous(command, **kwargs):
            result_path = Path(command[command.index("--result-file") + 1])
            receipt_path = result_path.with_name(result_path.name + ".bounded-receipt.json")
            receipt_path.write_text(json.dumps({
                "session": "session-1", "request_id": self.request["request_id"],
                "request_sha256": self.request["request_sha256"],
                "repository": self.request["repository"], "model": self.request["model"],
                "strategy": "merge", "status": "dispatching", "task": None,
            }), encoding="utf-8")
            result = CLOUD.Result(
                status="error", error={"code": "task_failed", "message": "POST timed out"},
                model=self.request["model"], repository=self.request["repository"],
                strategy="merge", request_id=self.request["request_id"],
                request_sha256=self.request["request_sha256"],
                pull_request=self.request["pull_request"],
            ).as_dict()
            result_path.write_text(json.dumps(result), encoding="utf-8")
            return subprocess.CompletedProcess(command, 2, "", "")

        self.calls["run"] = self.patch("run", side_effect=ambiguous)
        self.assertEqual(1, MODULE.command_pipeline(self.args))
        state = MODULE.load_state(self.path)
        self.assertEqual("unknown", state["agent_task"]["task_id_status"])
        self.assertEqual("interrupted", state["agent_task"]["status"])
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.command_pipeline(self.args)
        self.assertEqual(1, self.calls["run"].call_count)

    def test_preflight_rejection_preserves_error_without_task(self):
        def rejected(command, **kwargs):
            result_path = Path(command[command.index("--result-file") + 1])
            receipt_path = result_path.with_name(result_path.name + ".bounded-receipt.json")
            receipt_path.write_text(json.dumps({
                "session": "session-1", "request_id": self.request["request_id"],
                "request_sha256": self.request["request_sha256"],
                "repository": self.request["repository"], "model": self.request["model"],
                "strategy": self.request["strategy"], "status": "preflight", "task": None,
            }), encoding="utf-8")
            result_path.write_text(json.dumps(CLOUD.Result(
                status="error",
                error={"code": "stale_target", "message": "pull request target changed"},
                model=self.request["model"], repository=self.request["repository"],
                strategy=self.request["strategy"], request_id=self.request["request_id"],
                request_sha256=self.request["request_sha256"],
                pull_request=self.request["pull_request"],
            ).as_dict()), encoding="utf-8")
            return subprocess.CompletedProcess(command, 2, "", "")

        helper = self.patch("run", side_effect=rejected)
        self.assertEqual(1, MODULE.command_pipeline(self.args))
        state = MODULE.load_state(self.path)
        self.assertEqual("failed", state["agent_task"]["status"])
        self.assertEqual("not_created", state["agent_task"]["task_id_status"])
        self.assertEqual("stale_target", state["agent_task"]["error"]["code"])
        self.assertFalse(state["bounded_pipeline"]["inflight"])
        self.published.assert_not_called()
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.command_pipeline(self.args)
        self.assertEqual(1, helper.call_count)

    def test_missing_preflight_receipt_does_not_prove_no_dispatch(self):
        def missing(command, **kwargs):
            result_path = Path(command[command.index("--result-file") + 1])
            result_path.write_text(json.dumps(CLOUD.Result(
                status="error",
                error={"code": "stale_target", "message": "pull request target changed"},
                model=self.request["model"], repository=self.request["repository"],
                strategy=self.request["strategy"], request_id=self.request["request_id"],
                request_sha256=self.request["request_sha256"],
                pull_request=self.request["pull_request"],
            ).as_dict()), encoding="utf-8")
            return subprocess.CompletedProcess(command, 2, "", "")

        self.patch("run", side_effect=missing)
        with self.assertRaisesRegex(MODULE.WorkflowError, "receipt is missing"):
            MODULE.command_pipeline(self.args)
        self.assertEqual(
            "unknown", MODULE.load_state(self.path)["agent_task"]["task_id_status"]
        )

    def test_failed_execution_root_is_not_adopted(self):
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        state = MODULE.load_state(self.path)
        state["bounded_pipeline"]["inflight"] = True
        MODULE.save_state(self.path, state)
        with self.assertRaisesRegex(MODULE.WorkflowError, "cannot be adopted"):
            MODULE.command_pipeline(self.args)
        self.assertEqual(1, self.dispatches)
        self.assertEqual(0, self.observations)

    def test_whole_stack_requires_authorization_before_dispatch(self):
        self.args.whole_stack = True
        with self.assertRaisesRegex(MODULE.WorkflowError, "requires --stack-request"):
            MODULE.command_pipeline(self.args)
        self.assertFalse(self.path.exists())
        self.assertEqual(0, self.dispatches)

    def test_terminal_result_identity_fails_before_publication(self):
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        original = self.helper

        def forged(command, **kwargs):
            process = original(command, **kwargs)
            result_path = Path(command[command.index("--result-file") + 1])
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["task"]["base_sha"] = "f" * 40
            result_path.write_text(json.dumps(result), encoding="utf-8")
            return process

        self.patch("run", side_effect=forged)
        with self.assertRaisesRegex(MODULE.WorkflowError, "base does not match"):
            MODULE.command_pipeline(self.args)
        self.published.assert_not_called()
        self.assertEqual("interrupted", MODULE.load_state(self.path)["agent_task"]["status"])

    def test_helper_failure_without_task_id_preserves_stale_target_error(self):
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        original = self.helper

        def stale_target(command, **kwargs):
            original(command, **kwargs)
            result_path = Path(command[command.index("--result-file") + 1])
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["status"] = "error"
            result["error"] = {
                "code": "stale_target", "message": "pull request target changed",
            }
            result["task"]["id"] = None
            result_path.write_text(json.dumps(result), encoding="utf-8")
            return subprocess.CompletedProcess(command, 1, "", "")

        self.patch("run", side_effect=stale_target)
        self.assertEqual(1, MODULE.command_pipeline(self.args))
        state = MODULE.load_state(self.path)["agent_task"]
        self.assertEqual("interrupted", state["status"])
        self.assertEqual("task-1", state["task_id"])
        self.assertEqual("stale_target", state["error"]["code"])
        self.published.assert_not_called()

    def test_helper_failure_cannot_hide_a_different_task_id(self):
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        original = self.helper

        def wrong_task(command, **kwargs):
            original(command, **kwargs)
            result_path = Path(command[command.index("--result-file") + 1])
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["status"] = "error"
            result["error"] = {"code": "stale_target", "message": "stale"}
            result["task"]["id"] = "different-task"
            result_path.write_text(json.dumps(result), encoding="utf-8")
            return subprocess.CompletedProcess(command, 1, "", "")

        self.patch("run", side_effect=wrong_task)
        with self.assertRaisesRegex(MODULE.WorkflowError, "task identity changed"):
            MODULE.command_pipeline(self.args)
        self.published.assert_not_called()


class BoundedBackendTest(unittest.TestCase):
    def test_preflight_receipt_precedes_target_guard_and_prevents_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = CLOUD.LocalSnapshot(
                root / "repo", root, "owner/repo", "origin",
                "feature", "b" * 40, "", None,
            )
            snapshot.root.mkdir()
            for strategy in ("merge", "native-stack"):
                with self.subTest(strategy=strategy):
                    request = existing.ManagedConflictCoordinatorTest().request()
                    request["strategy"] = strategy
                    options = CLOUD.Options(
                        strategy, "gpt-5.6-sol", request["pull_request"]["url"],
                        root / f"{strategy}-request.json",
                        root / f"{strategy}-prompt.txt",
                        root / f"{strategy}-result.json",
                        request, "prompt", "dispatch", "session-1",
                    )
                    with (
                        mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": "session-1"}),
                        mock.patch.object(CLOUD, "local_snapshot", return_value=snapshot),
                        mock.patch.object(
                            CLOUD, "require_target_fresh",
                            side_effect=CLOUD.ConflictError(
                                "pull request target changed", "stale_target"
                            ),
                        ) as guard,
                        mock.patch.object(CLOUD, "start_task") as post,
                    ):
                        with self.assertRaisesRegex(CLOUD.ConflictError, "target changed"):
                            CLOUD.execute_bounded(
                                options, cwd=snapshot.root, runner=subprocess.run,
                                progress=CLOUD.Progress(), result=CLOUD.Result(),
                            )
                        receipt = json.loads(
                            CLOUD.bounded_receipt_path(options).read_text(encoding="utf-8")
                        )
                        self.assertEqual("preflight", receipt["status"])
                        self.assertIsNone(receipt["task"])
                        if strategy == "native-stack":
                            self.assertEqual(0, receipt["member_index"])
                        with self.assertRaisesRegex(
                            CLOUD.ConflictError, "dispatch already attempted"
                        ):
                            CLOUD.execute_bounded(
                                options, cwd=snapshot.root, runner=subprocess.run,
                                progress=CLOUD.Progress(), result=CLOUD.Result(),
                            )
                        guard.assert_called_once()
                        post.assert_not_called()

    def test_target_guard_allows_forward_base_without_replacing_pinned_snapshot(self):
        request = existing.ManagedConflictCoordinatorTest().request()
        old_base = request["pull_request"]["base_sha"]
        new_base = "d" * 40
        snapshot = CLOUD.LocalSnapshot(
            Path("repo"), Path("control"), "owner/repo", "origin",
            "feature", "b" * 40, "", None,
        )
        live = replace(CLOUD.request_pr_as_live(request), base_sha=new_base)
        comparisons = []

        def checked(_runner, command, **_kwargs):
            if command[:2] == ["gh", "repo"]:
                return json.dumps({
                    "mergeCommitAllowed": True,
                    "rebaseMergeAllowed": True,
                    "squashMergeAllowed": True,
                })
            if command[:2] == ["gh", "api"]:
                comparisons.append(command[2])
                return json.dumps({
                    "status": "ahead",
                    "merge_base_commit": {"sha": request["merge_base"]},
                })
            self.fail(f"unexpected command: {command}")

        with (
            mock.patch.object(CLOUD, "resolve_pr", return_value=live),
            mock.patch.object(CLOUD, "checked", side_effect=checked),
        ):
            CLOUD.require_target_fresh(subprocess.run, snapshot, request)
        self.assertEqual([
            f"repos/owner/repo/compare/{old_base}...{new_base}",
            f"repos/owner/repo/compare/{old_base}...{'b' * 40}",
        ], comparisons)
        self.assertEqual(old_base, request["pull_request"]["base_sha"])

    def test_target_guard_rejects_rewritten_base_and_retargeting(self):
        request = existing.ManagedConflictCoordinatorTest().request()
        snapshot = CLOUD.LocalSnapshot(
            Path("repo"), Path("control"), "owner/repo", "origin",
            "feature", "b" * 40, "", None,
        )
        live = replace(CLOUD.request_pr_as_live(request), base_sha="d" * 40)
        with (
            mock.patch.object(CLOUD, "resolve_pr", return_value=live),
            mock.patch.object(CLOUD, "checked", return_value='{"status":"diverged"}'),
            self.assertRaisesRegex(CLOUD.ConflictError, "base changed non-linearly"),
        ):
            CLOUD.require_target_fresh(subprocess.run, snapshot, request)
        with (
            mock.patch.object(
                CLOUD, "resolve_pr",
                return_value=replace(live, base_ref="other"),
            ),
            mock.patch.object(CLOUD, "checked") as checked,
            self.assertRaisesRegex(CLOUD.ConflictError, "target changed"),
        ):
            CLOUD.require_target_fresh(subprocess.run, snapshot, request)
        checked.assert_not_called()

    def test_target_guard_reports_head_change_even_when_base_advanced(self):
        request = existing.ManagedConflictCoordinatorTest().request()
        snapshot = CLOUD.LocalSnapshot(
            Path("repo"), Path("control"), "owner/repo", "origin",
            "feature", "b" * 40, "", None,
        )
        live = replace(
            CLOUD.request_pr_as_live(request),
            head_sha="c" * 40, base_sha="d" * 40,
        )
        with (
            mock.patch.object(CLOUD, "resolve_pr", return_value=live),
            self.assertRaises(CLOUD.SourceHeadChanged),
        ):
            CLOUD.require_target_fresh(subprocess.run, snapshot, request)

    def test_native_stack_trunk_can_advance_but_cannot_be_rewritten(self):
        request = existing.ManagedConflictCoordinatorTest().request()
        request["strategy"] = "native-stack"
        request["native_stack"] = {
            "trunk": {"ref": "main", "sha": "a" * 40},
            "members": [{
                "pr_number": 7, "repository": "owner/repo",
                "head_ref": "feature", "head_sha": "b" * 40,
                "direct_base_ref": "main",
            }],
            "outside_dependents": [],
        }
        snapshot = CLOUD.LocalSnapshot(
            Path("repo"), Path("control"), "owner/repo", "origin",
            "feature", "b" * 40, "", None,
        )
        live = CLOUD.request_pr_as_live(request)
        comparison = ["ahead"]

        def checked(_runner, command, **_kwargs):
            if command[:2] == ["gh", "repo"]:
                return json.dumps({
                    "mergeCommitAllowed": True, "rebaseMergeAllowed": True,
                    "squashMergeAllowed": True,
                })
            if command[2].startswith("repos/owner/repo/compare/"):
                return json.dumps({
                    "status": comparison[0],
                    "merge_base_commit": {"sha": request["merge_base"]},
                })
            return json.dumps({"object": {"sha": "d" * 40}})

        with (
            mock.patch.object(CLOUD, "resolve_pr", return_value=live),
            mock.patch.object(CLOUD, "checked", side_effect=checked),
        ):
            CLOUD.require_target_fresh(subprocess.run, snapshot, request)
            comparison[0] = "diverged"
            with self.assertRaisesRegex(CLOUD.ConflictError, "base changed non-linearly"):
                CLOUD.require_target_fresh(subprocess.run, snapshot, request)

    def test_parse_unbounded_and_bounded_conflict_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = existing.ManagedConflictCoordinatorTest().request()
            request["head_commits"] = [{
                "sha": request["pull_request"]["head_sha"],
                "subject": "Resolve conflict",
                "trailers": [],
                "patch_sha256": "c" * 64,
                "paths": ["app.py"],
            }]
            request["request_sha256"] = CLOUD.request_digest(request)
            request_path = root / "request.json"
            prompt_path = root / "prompt.txt"
            result_path = root / "result.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            prompt_path.write_text("Resolve the conflict", encoding="utf-8")
            command = [
                "--conflict-with-report",
                "--strategy", "merge",
                "--model", "sol",
                "--pr", request["pull_request"]["url"],
                "--request-file", str(request_path),
                "--prompt-file", str(prompt_path),
                "--result-file", str(result_path),
                "--policy", CLOUD.POLICY_SELECTOR,
            ]
            ordinary = CLOUD.parse_args(command)
            self.assertIsNone(ordinary.bounded_phase)
            self.assertIsNone(ordinary.bounded_session)
            bounded = CLOUD.parse_args([
                *command, "--bounded-phase", "dispatch",
                "--bounded-session", "session-1",
            ])
            self.assertEqual("dispatch", bounded.bounded_phase)
            self.assertEqual("session-1", bounded.bounded_session)
            for flags, message in (
                (["--bounded-phase", "dispatch"], "requires session"),
                (["--bounded-phase", ""], "requires session"),
                ([
                    "--bounded-phase", "unknown", "--bounded-session", "session-1",
                ], "invalid bounded phase"),
                (["--bounded-deadline", "140"], "unknown option"),
            ):
                with self.subTest(flags=flags), self.assertRaisesRegex(
                    CLOUD.ConflictError, message,
                ):
                    CLOUD.parse_args([*command, *flags])

    def test_bounded_receipt_keeps_runtime_observation_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = existing.ManagedConflictCoordinatorTest().request()
            options = CLOUD.Options(
                "merge", "gpt-5.6-sol", request["pull_request"]["url"],
                root / "request.json", root / "prompt.txt", root / "result.json",
                request, "prompt", "dispatch", "session-1",
            )
            runtime_path = root / "result.json.dispatch.json"
            observation = {"schema": "github.copilot.dispatch-observation.v1"}
            runtime_path.write_text(json.dumps(observation), encoding="utf-8")
            receipt = {
                "session": "session-1", "request_id": request["request_id"],
                "request_sha256": request["request_sha256"],
                "repository": request["repository"], "model": options.model,
                "strategy": options.strategy, "status": "dispatching", "task": None,
            }
            CLOUD.atomic_write_json(CLOUD.bounded_receipt_path(options), receipt)
            self.assertEqual(receipt, CLOUD.bounded_receipt(options))
            self.assertEqual(observation, json.loads(runtime_path.read_text(encoding="utf-8")))

    def test_observe_and_collect_only_the_original_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = existing.ManagedConflictCoordinatorTest().request()
            options = CLOUD.Options(
                "merge", "gpt-5.6-sol", request["pull_request"]["url"],
                root / "request.json", root / "prompt.txt", root / "result.json",
                request, "prompt", "dispatch", "session-1",
            )
            snapshot = CLOUD.LocalSnapshot(
                root / "repo", root, "owner/repo", "origin",
                "feature", "b" * 40, "", None,
            )
            snapshot.root.mkdir()
            updates = iter(("in_progress", "completed", "completed"))

            def get_task(runner, source, task_id):
                self.assertEqual("task-1", task_id)
                return {"id": task_id, "state": next(updates)}

            with (
                mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": "session-1"}),
                mock.patch.object(CLOUD, "local_snapshot", return_value=snapshot),
                mock.patch.object(CLOUD, "require_target_fresh"),
                mock.patch.object(CLOUD, "require_local_unchanged"),
                mock.patch.object(CLOUD, "fetch_pinned_inputs") as fetch,
                mock.patch.object(CLOUD, "verify_frozen_ranges"),
                mock.patch.object(CLOUD, "already_satisfied", return_value=False),
                mock.patch.object(
                    CLOUD, "start_task", return_value={"id": "task-1", "state": "queued"}
                ) as post,
                mock.patch.object(CLOUD, "get_task", side_effect=get_task),
                mock.patch.object(CLOUD, "prove_generated", return_value=(
                    [{"role": "code"}], {"branch": "artifact"}, [],
                )) as prove,
            ):
                for phase, expected in (
                    ("dispatch", "queued"), ("observe", "in_progress"),
                    ("observe", "completed"), ("collect", "completed"),
                ):
                    result = CLOUD.Result()
                    with self.subTest(phase=phase, expected=expected):
                        self.assertEqual(0, CLOUD.execute_bounded(
                            replace(options, bounded_phase=phase),
                            cwd=snapshot.root, runner=subprocess.run,
                            progress=CLOUD.Progress(), result=result,
                        ))
                        self.assertEqual(expected, result.task_state)
                        self.assertEqual(
                            "success" if phase == "collect" else "waiting", result.status
                        )
                post.assert_called_once()
                fetch.assert_called_once()
                prove.assert_called_once()
                with mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": "session-2"}):
                    with self.assertRaisesRegex(CLOUD.ConflictError, "does not own"):
                        CLOUD.execute_bounded(
                            replace(options, bounded_phase="observe"),
                            cwd=snapshot.root, runner=subprocess.run,
                            progress=CLOUD.Progress(), result=CLOUD.Result(),
                        )

    def test_post_receipt_prevents_duplicate_dispatch_after_ambiguous_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = existing.ManagedConflictCoordinatorTest().request()
            options = CLOUD.Options(
                "merge", "gpt-5.6-sol", request["pull_request"]["url"],
                root / "request.json", root / "prompt.txt", root / "result.json",
                request, "prompt", "dispatch", "session-1",
            )
            snapshot = CLOUD.LocalSnapshot(
                root / "repo", root, "owner/repo", "origin",
                "feature", "b" * 40, "", None,
            )
            (root / "repo").mkdir()
            with (
                mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": "session-1"}),
                mock.patch.object(CLOUD, "local_snapshot", return_value=snapshot),
                mock.patch.object(CLOUD, "require_target_fresh"),
                mock.patch.object(CLOUD, "require_local_unchanged"),
                mock.patch.object(CLOUD, "fetch_pinned_inputs"),
                mock.patch.object(CLOUD, "verify_frozen_ranges"),
                mock.patch.object(CLOUD, "already_satisfied", return_value=False),
                mock.patch.object(
                    CLOUD, "start_task",
                    side_effect=CLOUD.ConflictError("POST outcome unknown", "task_failed"),
                ) as post,
            ):
                with self.assertRaises(CLOUD.ConflictError):
                    CLOUD.execute_bounded(
                        options, cwd=snapshot.root, runner=subprocess.run,
                        progress=CLOUD.Progress(), result=CLOUD.Result(),
                    )
                receipt = CLOUD.bounded_receipt(options)
                self.assertEqual("dispatching", receipt["status"])
                with self.assertRaisesRegex(CLOUD.ConflictError, "already attempted"):
                    CLOUD.execute_bounded(
                        options, cwd=snapshot.root, runner=subprocess.run,
                        progress=CLOUD.Progress(), result=CLOUD.Result(),
                    )
                post.assert_called_once()


class BoundedSweepTest(unittest.TestCase):
    def test_prior_policy_no_task_clearance_can_be_revalidated(self):
        fixture = sweeps.ConflictPipelineSweepTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.args.bounded_step = True
        with mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": "session-1"}):
            self.assertEqual(0, MODULE.command_pipeline(fixture.args))
            state = MODULE.load_state(fixture.path)
            state["agent_task"]["policy"] = MODULE.LEGACY_NO_TASK_POLICY
            MODULE.save_state(fixture.path, state)
            fixture.args.pipeline_iteration = 2
            fixture.metadata["head_sha"] = "c" * 40
            self.assertEqual(0, MODULE.command_pipeline(fixture.args))
        self.assertEqual("c" * 40, MODULE.cleared_head_sha(MODULE.load_state(fixture.path)))
        fixture.calls["discover_conflict_task"].assert_not_called()

    def test_completed_sweep_rechecks_live_base_without_another_dispatch(self):
        fixture = sweeps.ConflictPipelineSweepTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.args.bounded_step = True
        with mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": "session-1"}):
            self.assertEqual(0, MODULE.command_pipeline(fixture.args))
            fixture.args.pipeline_iteration = 2
            fixture.metadata["base_sha"] = "d" * 40
            self.assertEqual(0, MODULE.command_pipeline(fixture.args))
        state = MODULE.load_state(fixture.path)
        self.assertEqual(2, state["pipeline"]["iteration"])
        self.assertEqual("d" * 40, state["attempt"]["base_sha"])
        self.assertEqual(0, state["managed_attempts"])
        fixture.calls["discover_conflict_task"].assert_not_called()


class BoundedNativeStackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stack_tests.SequentialStackTest.setUpClass()

    @classmethod
    def tearDownClass(cls):
        stack_tests.SequentialStackTest.tearDownClass()

    def test_two_member_stack_dispatches_once_per_member_and_verifies_final_history(self):
        fixture = stack_tests.SequentialStackTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        phases = (
            "dispatch", "observe", "observe", "collect",
            "dispatch", "observe", "collect",
        )
        outcomes = []
        observations = {"task-1": 0}

        def start_pending(runner, snapshot, options):
            task = fixture.start(runner, snapshot, options)
            return {**task, "state": "queued"}

        def get_task(runner, snapshot, task_id):
            task = copy.deepcopy(fixture.tasks[task_id])
            if task_id == "task-1" and observations[task_id] == 0:
                task["state"] = "in_progress"
            observations[task_id] = observations.get(task_id, 0) + 1
            return task

        with (
            mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": "session-1"}),
            mock.patch.object(CLOUD, "local_snapshot", return_value=fixture.snapshot),
            mock.patch.object(CLOUD, "require_target_fresh"),
            mock.patch.object(CLOUD, "already_satisfied", return_value=False),
            mock.patch.object(CLOUD, "fetch_pinned_inputs"),
            mock.patch.object(CLOUD, "start_task", side_effect=start_pending) as post,
            mock.patch.object(CLOUD, "get_task", side_effect=get_task),
            mock.patch.object(CLOUD, "_EXECUTION", mock.Mock(run=subprocess.run)) as execution,
        ):
            for phase in phases:
                result = CLOUD.Result()
                self.assertEqual(0, CLOUD.execute_bounded(
                    replace(
                        fixture.options, bounded_phase=phase,
                        bounded_session="session-1",
                    ),
                    cwd=fixture.root, runner=subprocess.run,
                    progress=CLOUD.Progress(), result=result,
                ))
                outcomes.append(result.status)
            self.assertEqual(2, post.call_count)
            for options in fixture.launched:
                self.assertTrue(
                    any(
                        call.args[:3] == (
                            options.result_file,
                            options.request["request_id"],
                            fixture.snapshot.repository,
                        )
                        and call.args[3]["state"] == "completed"
                        for call in execution.record_dispatch.call_args_list
                    )
                )
        self.assertEqual(["waiting"] * 6 + ["success"], outcomes)
        self.assertEqual(
            [fixture.trunk, fixture.new_lower],
            [member["task"]["base_sha"] for member in result.artifact["members"]],
        )
        refs, artifact = MODULE.validate_conflict_result_identity(
            result.as_dict(), fixture.request,
        )
        MODULE.verify_quarantined_result(fixture.root, fixture.request, refs, artifact)

    def test_unknown_member_post_is_never_dispatched_twice(self):
        fixture = stack_tests.SequentialStackTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        options = replace(
            fixture.options, bounded_phase="dispatch", bounded_session="session-1",
        )
        with (
            mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": "session-1"}),
            mock.patch.object(CLOUD, "local_snapshot", return_value=fixture.snapshot),
            mock.patch.object(CLOUD, "require_target_fresh"),
            mock.patch.object(CLOUD, "already_satisfied", return_value=False),
            mock.patch.object(CLOUD, "fetch_pinned_inputs"),
            mock.patch.object(
                CLOUD, "start_task",
                side_effect=CLOUD.ConflictError("unknown POST outcome", "task_failed"),
            ) as post,
        ):
            for attempt in (1, 2):
                with self.subTest(attempt=attempt), self.assertRaises(CLOUD.ConflictError):
                    CLOUD.execute_bounded(
                        options, cwd=fixture.root, runner=subprocess.run,
                        progress=CLOUD.Progress(), result=CLOUD.Result(),
                    )
            receipt = json.loads(
                CLOUD.bounded_receipt_path(options).read_text(encoding="utf-8")
            )
            self.assertEqual("dispatching", receipt["status"])
            self.assertEqual(0, receipt["member_index"])
            post.assert_called_once()

    def test_second_member_preflight_rejection_keeps_member_position(self):
        fixture = stack_tests.SequentialStackTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        options = replace(
            fixture.options, bounded_phase="dispatch", bounded_session="session-1",
        )
        first_result = fixture.directory / "result--member-6--result.json"
        first_result.write_text("{}", encoding="utf-8")
        CLOUD.stack_root_receipt(
            options, 0, "completed", {"id": "task-1", "state": "completed"},
        )
        guard_calls = 0

        def guard(*_args):
            nonlocal guard_calls
            guard_calls += 1
            if guard_calls == 2:
                raise CLOUD.ConflictError("pull request target changed", "stale_target")

        with (
            mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": "session-1"}),
            mock.patch.object(CLOUD, "local_snapshot", return_value=fixture.snapshot),
            mock.patch.object(CLOUD, "require_target_fresh", side_effect=guard),
            mock.patch.object(CLOUD, "require_local_unchanged"),
            mock.patch.object(CLOUD, "prove_native_stack_member_input"),
            mock.patch.object(
                CLOUD, "completed_stack_member",
                return_value=(
                    {"id": "task-1", "state": "completed"},
                    {"branch": "copilot/lower-task", "task": {
                        "url": "https://github.com/owner/repo/tasks/task-1",
                        "base_sha": fixture.trunk,
                    }},
                    {"new_sha": fixture.new_lower},
                ),
            ),
            mock.patch.object(CLOUD, "start_task") as post,
        ):
            with self.assertRaisesRegex(CLOUD.ConflictError, "target changed"):
                CLOUD.execute_bounded(
                    options, cwd=fixture.root, runner=subprocess.run,
                    progress=CLOUD.Progress(), result=CLOUD.Result(),
                )
        receipt = json.loads(
            CLOUD.bounded_receipt_path(options).read_text(encoding="utf-8")
        )
        self.assertEqual("preflight", receipt["status"])
        self.assertEqual(1, receipt["member_index"])
        self.assertIsNone(receipt["task"])
        post.assert_not_called()


class BoundedNativeControllerTest(unittest.TestCase):
    def test_pipeline_advances_two_member_stack_without_reposting(self):
        fixture = BoundedPipelineTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.request.update(
            strategy="native-stack",
            native_stack={"members": [{"pr_number": 6}, {"pr_number": 7}]},
        )
        fixture.request["request_sha256"] = MODULE.request_digest(fixture.request)
        fixture.calls["conflict_preflight"].side_effect = lambda *a, **kw: {
            "already_mergeable": False, "pr": copy.deepcopy(fixture.metadata),
            "strategy": "native-stack", "request": copy.deepcopy(fixture.request),
        }
        fixture.patch(
            "authorize_resolver_native_stack", return_value={"request_id": "stack-1"},
        )
        fixture.patch(
            "validate_conflict_result_identity",
            return_value=([{"role": "member:6"}, {"role": "member:7"}], {"members": []}),
        )
        launches = []

        def helper(command, **kwargs):
            phase = command[command.index("--bounded-phase") + 1]
            state = MODULE.load_state(fixture.path)
            index = state["bounded_pipeline"].get("member_index", 0)
            if phase == "dispatch":
                launches.append(index)
            result_path = Path(command[command.index("--result-file") + 1])
            receipt_path = result_path.with_name(result_path.name + ".bounded-receipt.json")
            remote_state = "completed"
            task_id = f"task-{index}"
            receipt_path.write_text(json.dumps({
                "session": "session-1", "request_id": fixture.request["request_id"],
                "request_sha256": fixture.request["request_sha256"],
                "repository": fixture.request["repository"],
                "model": fixture.request["model"], "strategy": "native-stack",
                "member_index": index, "status": "completed",
                "task": {"id": task_id, "state": remote_state},
            }), encoding="utf-8")
            result = CLOUD.Result(
                status="success" if phase == "collect" and index == 1 else "waiting",
                model=fixture.request["model"], repository=fixture.request["repository"],
                strategy="native-stack", request_id=fixture.request["request_id"],
                request_sha256=fixture.request["request_sha256"],
                pull_request=fixture.request["pull_request"],
                task_id=task_id, task_state=remote_state,
                task_base_ref="a" * 40, task_base_sha="a" * 40,
            ).as_dict()
            result_path.write_text(json.dumps(result), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        fixture.patch("run", side_effect=helper)
        for expected_phase in ("dispatch", "collect", "dispatch", "collect", "dispatch"):
            code = MODULE.command_pipeline(fixture.args)
            self.assertEqual(0, code, MODULE.load_state(fixture.path)["agent_task"])
            current = MODULE.load_state(fixture.path)
            if current["agent_task"]["status"] != "completed":
                self.assertEqual(expected_phase, current["bounded_pipeline"]["phase"])
        self.assertEqual([0, 1], launches)
        self.assertEqual(1, fixture.published.call_count)
        self.assertEqual("completed", MODULE.load_state(fixture.path)["agent_task"]["status"])


class BoundedStackAuthorizationTest(unittest.TestCase):
    def test_resolver_owned_stack_survives_only_its_bound_session(self):
        fixture = stack_auth.ResolverOwnedAuthorizationTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.state["bounded_pipeline"] = {
            "session": "session-1",
            "binding": {"run": "pipeline-run"},
            "phase": "observe",
        }
        request = fixture.authorize({
            "run": "pipeline-run", "iteration": 1, "budget": 2,
        })
        state = MODULE.load_state(fixture.state_path)
        state["agent_task"]["status"] = "running"
        MODULE.save_state(fixture.state_path, state)
        with (
            mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": "session-1"}),
            mock.patch.object(MODULE, "stack_owner_is_running", return_value=False),
            mock.patch.object(
                MODULE, "_BOUNDED_STACK_AUTH", (fixture.state_path, "session-1"),
            ),
        ):
            MODULE.require_authorized_stack(
                request, fixture.preflight["pr"], fixture.stack,
            )
            with mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": "session-2"}):
                with self.assertRaises(MODULE.WorkflowError):
                    MODULE.require_authorized_stack(
                        request, fixture.preflight["pr"], fixture.stack,
                    )

    def test_finished_owner_pid_needs_original_authorization_and_same_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request, request_path, _, owner_path = stack_auth.write_authorization(
                root, existing.native_stack_detection()["stack"],
                fixed=7, operation="whole-stack",
            )
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            owner_path.write_text(json.dumps(owner), encoding="utf-8")
            resolver_path = root / "bounded-state.json"
            state = {
                "version": MODULE.STATE_VERSION,
                "bounded_pipeline": {
                    "session": "session-1",
                    "binding": {"run": "run-1"},
                    "stack_authorization_sha256": request["request_sha256"],
                },
                "agent_task": {"status": "running", "run_id": "run-1"},
                "stack_requests": {request["request_id"]: request["request_sha256"]},
            }
            MODULE.save_state(resolver_path, state)
            with (
                mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": "session-1"}),
                mock.patch.object(MODULE, "stack_owner_is_running", return_value=False),
                mock.patch.object(
                    MODULE, "_BOUNDED_STACK_AUTH", (resolver_path, "session-1")
                ),
            ):
                self.assertEqual(
                    request, MODULE.load_stack_request(
                        str(request_path), operation="whole-stack", run_id="run-1",
                    ),
                )
                with mock.patch.dict(os.environ, {"COPILOT_AGENT_SESSION_ID": "session-2"}):
                    with self.assertRaises(MODULE.WorkflowError):
                        MODULE.load_stack_request(
                            str(request_path), operation="whole-stack", run_id="run-1",
                        )
                state["bounded_pipeline"]["stack_authorization_sha256"] = "f" * 64
                MODULE.save_state(resolver_path, state)
                with self.assertRaises(MODULE.WorkflowError):
                    MODULE.load_stack_request(
                        str(request_path), operation="whole-stack", run_id="run-1",
                    )
                state["bounded_pipeline"]["stack_authorization_sha256"] = (
                    request["request_sha256"]
                )
                MODULE.save_state(resolver_path, state)
                owner["result"] = {"status": "failed"}
                owner_path.write_text(json.dumps(owner), encoding="utf-8")
                with self.assertRaisesRegex(MODULE.WorkflowError, "active authorized run"):
                    MODULE.load_stack_request(
                        str(request_path), operation="whole-stack", run_id="run-1",
                    )
                owner_path.write_text("[]", encoding="utf-8")
                with self.assertRaisesRegex(MODULE.WorkflowError, "owner or run"):
                    MODULE.load_stack_request(
                        str(request_path), operation="whole-stack", run_id="run-1",
                    )
