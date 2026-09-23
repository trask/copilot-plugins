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
        self.patch("require_no_credentials")
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
        result_path = Path(command[command.index("--result-file") + 1])
        receipt_path = result_path.with_name(result_path.name + ".dispatch.json")
        result = CLOUD.Result(
            status="waiting", model=self.request["model"],
            repository=self.request["repository"], strategy="merge",
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
            "strategy": "merge",
            "status": "completed" if result["task"]["state"] == "completed" else "active",
            "task": {"id": "task-1", "state": result["task"]["state"]},
        }
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        result_path.write_text(json.dumps(result), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_remote_work_longer_than_outer_deadline_uses_one_dispatch(self):
        for elapsed in (0, 65, 130, 180):
            with self.subTest(elapsed=elapsed):
                with mock.patch.object(MODULE.time, "monotonic", return_value=elapsed):
                    self.assertEqual(0, MODULE.command_pipeline(self.args))
                state = MODULE.load_state(self.path)
                if elapsed < 180:
                    self.assertEqual("running", state["agent_task"]["status"])
                    self.assertEqual(1, state["managed_attempts"])
        self.assertEqual("completed", MODULE.load_state(self.path)["agent_task"]["status"])
        self.assertEqual(1, self.dispatches)
        self.assertEqual(2, self.observations)
        self.published.assert_called_once()
        self.assertEqual(0, MODULE.command_pipeline(self.args))
        self.assertEqual(1, self.dispatches)

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
            receipt_path = result_path.with_name(result_path.name + ".dispatch.json")
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


class BoundedBackendTest(unittest.TestCase):
    def test_observe_and_collect_only_the_original_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = existing.ManagedConflictCoordinatorTest().request()
            options = CLOUD.Options(
                "merge", "gpt-6-sol", request["pull_request"]["url"],
                root / "request.json", root / "prompt.txt", root / "result.json",
                request, "prompt", "dispatch", "session-1", 10**20,
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
                "merge", "gpt-6-sol", request["pull_request"]["url"],
                root / "request.json", root / "prompt.txt", root / "result.json",
                request, "prompt", "dispatch", "session-1", 10**20,
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
        ):
            for phase in phases:
                result = CLOUD.Result()
                self.assertEqual(0, CLOUD.execute_bounded(
                    replace(
                        fixture.options, bounded_phase=phase,
                        bounded_session="session-1", bounded_deadline=10**20,
                    ),
                    cwd=fixture.root, runner=subprocess.run,
                    progress=CLOUD.Progress(), result=result,
                ))
                outcomes.append(result.status)
            self.assertEqual(2, post.call_count)
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
            fixture.options, bounded_phase="dispatch",
            bounded_session="session-1", bounded_deadline=10**20,
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
            receipt_path = result_path.with_name(result_path.name + ".dispatch.json")
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
        for expected_phase in ("collect", "dispatch", "collect", "dispatch"):
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
