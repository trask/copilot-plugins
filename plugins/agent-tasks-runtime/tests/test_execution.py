import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = Path(__file__).parents[1] / "skills" / "agent-tasks-runtime" / "scripts" / "execution.py"
SPEC = importlib.util.spec_from_file_location("foreground_execution_test", SCRIPT)
EXECUTION = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = EXECUTION
SPEC.loader.exec_module(EXECUTION)
IDENTITY = {"pid": 123, "creation_time": "456", "image": "python.exe", "running": True}


class ExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.handle = self.root / "handle.json"
        self.identity = mock.patch.object(EXECUTION, "process_identity", return_value=dict(IDENTITY))
        self.identity.start()
        self.addCleanup(self.identity.stop)

    def context(self):
        return EXECUTION.Execution(self.handle, command=["python", "controller.py"],
                                   terminal_results=frozenset({"complete"}))

    def test_fresh_handle_cannot_be_reused(self):
        self.context()
        before = self.handle.read_bytes()
        with self.assertRaises(FileExistsError):
            self.context()
        self.assertEqual(before, self.handle.read_bytes())

    def test_execution_artifacts_cannot_enter_an_explicit_target_checkout(self):
        target = self.root / "target"
        target.mkdir()
        for arguments in (["--repo-root", str(target)], [f"--repo-root={target}"]):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(EXECUTION.ExecutionError, "outside the target"):
                    EXECUTION.Execution(target / "handle.json", command=["python", "controller.py", *arguments])
                self.assertEqual([], list(target.iterdir()))

    def test_terminal_artifact_is_authoritative_and_status_is_read_only(self):
        context = self.context()
        context.emit({"result": "incomplete", "pending_comments": ["finding"]})
        context.finish(0)
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        result = EXECUTION.status(self.handle)
        self.assertTrue(result["terminal"])
        self.assertEqual("incomplete", result["workflow_result"]["result"])
        self.assertNotIn("all_ci_passed", result)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_missing_result_is_not_success(self):
        context = self.context()
        result = context.finish(0)
        self.assertEqual(1, result["exit_code"])
        self.assertEqual("failed", result["local_status"])

    def test_progress_event_is_not_a_terminal_workflow_result(self):
        context = self.context()
        context.emit({"event": "started"})
        self.assertEqual(1, context.finish(0)["exit_code"])

    def test_dispatch_creation_uncertainty_and_known_identity_survive_cancel(self):
        context = self.context()
        first = self.root / "first-result.json"
        second = self.root / "second-result.json"
        context.record_dispatch(first, "request-one", "owner/repo")
        context.record_dispatch(first, "request-one", "owner/repo",
                                {"id": "task-one", "state": "queued", "url": "https://example/task-one"})
        context.record_dispatch(second, "request-two", "owner/repo")
        result = context.finish(130, cancelled=True)
        observations = {item["request_id"]: item for item in result["remote_tasks"]}
        self.assertEqual("task-one", observations["request-one"]["task"]["id"])
        self.assertIsNone(observations["request-two"]["task"])
        self.assertEqual("unknown", observations["request-two"]["remote_status"])
        self.assertTrue(result["remote_work_may_continue"])

    def test_nonzero_preserves_original_result_and_error(self):
        context = self.context()
        context.emit({"result": "complete", "retained_commits": ["abc"]})
        result = context.finish(7, "original failure")
        self.assertEqual("failed", result["local_status"])
        self.assertEqual(7, result["exit_code"])
        self.assertEqual("original failure", result["error"])
        self.assertEqual(["abc"], result["workflow_result"]["retained_commits"])

    def test_explicit_error_cannot_keep_a_zero_exit_code(self):
        context = self.context()
        context.emit({"result": "complete"})
        result = context.finish(0, "original failure")
        self.assertEqual(1, result["exit_code"])
        self.assertEqual("original failure", result["error"])
        self.assertEqual("failed", result["local_status"])

    def test_terminal_hash_drift_is_rejected(self):
        context = self.context()
        context.emit({"result": "complete"})
        context.finish(0)
        Path(context.record["result"]).write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(EXECUTION.ExecutionError, "identity or digest"):
            EXECUTION.status(self.handle)

    def test_generation_loss_is_abandonment_not_completion(self):
        self.context()
        with mock.patch.object(EXECUTION, "process_identity", return_value={**IDENTITY, "creation_time": "789"}):
            result = EXECUTION.status(self.handle)
            self.assertFalse(result["terminal"])
            self.assertEqual("abandoned", result["status"])
            with self.assertRaises(EXECUTION.ExecutionError):
                EXECUTION.cancel(self.handle)

    def test_child_binds_parent_generation_command_and_shared_cancellation(self):
        root = self.context()
        child_identity = {**IDENTITY, "pid": 456, "creation_time": "789"}
        child_handle = root.directory / "handle-child.json"
        child_record = root.directory / "child-record.json"
        request = root.directory / "request-child.json"
        command = ["python", "child.py"]
        EXECUTION.write(child_record, {
            "schema": EXECUTION.SCHEMA, "process_identity": child_identity,
            "root": str(root.handle), "run_id": root.run_id, "handle": str(child_handle),
            "command_sha256": EXECUTION.digest(command),
        })
        EXECUTION.write(request, {
            "schema": EXECUTION.SCHEMA, "root": str(root.handle), "parent": str(root.handle),
            "handle": str(child_handle), "child_record": str(child_record),
            "command_sha256": EXECUTION.digest(command),
        })
        with (
            mock.patch.object(EXECUTION.os, "getpid", return_value=456),
            mock.patch.object(EXECUTION, "process_identity",
                              side_effect=lambda pid: dict(IDENTITY if pid == 123 else child_identity)),
        ):
            child = EXECUTION.Execution(child_handle, command=command, parent=request)
            self.assertEqual(root.run_id, child.run_id)
            with mock.patch.object(EXECUTION, "process_identity",
                                   return_value={**IDENTITY, "creation_time": "reused"}):
                with self.assertRaisesRegex(EXECUTION.ExecutionError, "generation"):
                    child.check_cancel()
            EXECUTION.cancel(root.handle)
            with mock.patch.object(EXECUTION.subprocess, "Popen") as launch:
                with self.assertRaises(EXECUTION.Cancelled):
                    child.start(["git", "push"])
                launch.assert_not_called()
            before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
            self.assertEqual("cancel_requested", EXECUTION.status(root.handle)["status"])
            self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_cancellation_is_idempotent_and_fences_new_launch(self):
        context = self.context()
        first = EXECUTION.cancel(self.handle)
        second = EXECUTION.cancel(self.handle)
        self.assertEqual(first, second)
        with mock.patch.object(EXECUTION.subprocess, "Popen") as launch:
            with self.assertRaises(EXECUTION.Cancelled):
                context.start(["git", "push"])
            launch.assert_not_called()
        result = context.finish(130, cancelled=True)
        self.assertEqual("cancelled_local", result["local_status"])
        self.assertEqual("unconfirmed", result["remote_status"])
        self.assertTrue(result["remote_work_may_continue"])

    def test_foreign_cancellation_record_is_not_overwritten(self):
        context = self.context()
        destination = Path(context.record["cancel"])
        EXECUTION.write(destination, {"run_id": "foreign"})
        before = destination.read_bytes()
        with self.assertRaises(EXECUTION.ExecutionError):
            EXECUTION.cancel(self.handle)
        self.assertEqual(before, destination.read_bytes())

    def test_closed_observer_output_does_not_control_execution(self):
        namespace = {}
        closed = io.StringIO()
        closed.close()

        def main():
            print("progress after observer disconnect")
            namespace["_EXECUTION"].emit({"result": "complete"})
            return 0

        with mock.patch.object(sys, "stdout", closed), mock.patch.dict(EXECUTION.os.environ, {}, clear=True):
            self.assertEqual(0, EXECUTION.controller_main(main, namespace, handle=self.handle))
        result = EXECUTION.status(self.handle)
        self.assertEqual("finished", result["local_status"])
        self.assertIn("progress after observer disconnect",
                      Path(result["artifacts"]["stdout"]).read_text(encoding="utf-8"))

    def test_windows_child_has_no_window_and_no_breakaway(self):
        context = self.context()
        process = mock.Mock(pid=456)
        process.poll.return_value = None
        process.wait.return_value = 0
        owner = mock.Mock()
        owner.active_count.return_value = 0
        with (
            mock.patch.object(EXECUTION, "IS_WINDOWS", True),
            mock.patch.object(EXECUTION, "guard", side_effect=lambda path: mock.MagicMock()),
            mock.patch.object(EXECUTION.subprocess, "Popen", return_value=process) as launch,
            mock.patch.object(EXECUTION, "WindowsOwner", return_value=owner),
            mock.patch.object(EXECUTION, "resume_process") as resume,
        ):
            child = context.start(["python", "child.py"])
        flags = launch.call_args.kwargs["creationflags"]
        self.assertTrue(flags & 0x08000000)
        self.assertTrue(flags & 4)
        self.assertFalse(flags & 0x01000000)
        resume.assert_called_once_with(456)
        child.wait()

    def test_descendant_drain_failure_never_records_drained(self):
        context = self.context()
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123)
        process.wait.return_value = 0
        owner = mock.Mock()
        owner.active_count.return_value = 1
        owner.drain.side_effect = EXECUTION.ExecutionError("descendants remain")
        child = EXECUTION.OwnedProcess(process, owner, record, [])
        with self.assertRaisesRegex(EXECUTION.ExecutionError, "descendants remain"):
            child.wait()
        with self.assertRaisesRegex(EXECUTION.ExecutionError, "descendants remain"):
            child.wait()
        self.assertFalse(child.drained)
        owner.drain.assert_called_once()
        self.assertFalse(EXECUTION.read(record)["local_drained"])

    def test_captured_output_is_file_backed_and_returned(self):
        context = self.context()
        child = mock.Mock()
        child.record = context.directory / "child-test.json"
        EXECUTION.write(child.record, {"process_identity": IDENTITY})
        child.process.communicate.return_value = (None, None)
        child.process.text_mode = True
        child.process.encoding = "utf-8"
        child.process.errors = "strict"
        child.wait.return_value = 0

        def start(command, **options):
            self.assertNotIn("capture_output", options)
            options["stdout"].write(b"durable stdout\n")
            options["stderr"].write(b"durable stderr\n")
            return child

        with mock.patch.object(context, "start", side_effect=start):
            result = context.run(["python", "child.py"], capture_output=True, text=True)
        self.assertEqual("durable stdout\n", result.stdout)
        self.assertEqual("durable stderr\n", result.stderr)
        for name, path in EXECUTION.read(child.record)["captured_output"].items():
            self.assertEqual(getattr(result, name), Path(path).read_text(encoding="utf-8"))

    def test_terminal_before_handle_seal_is_not_completion(self):
        context = self.context()
        EXECUTION.write(Path(context.record["result"]), {"terminal": True})
        result = EXECUTION.status(self.handle)
        self.assertFalse(result["terminal"])
        self.assertEqual("terminal_unsealed", result["status"])

    def test_writer_release_failure_cannot_seal_success(self):
        context = self.context()
        context.emit({"result": "complete"})
        with mock.patch.object(context, "release_writers", side_effect=OSError("lease error")):
            result = context.finish(0)
        self.assertEqual("failed", result["local_status"])
        self.assertIn("lease error", result["finalization_errors"])
        self.assertTrue(result["local_children_drained"])

    def test_cancel_retains_remote_identity_and_domain_budget(self):
        context = self.context()
        state = self.root / "state.json"
        EXECUTION.write(state, {"iterations_used": 3, "task_id": "active-task"})
        context.record_state(state, {})
        before = state.read_bytes()
        result = context.finish(130, "stop", cancelled=True)
        self.assertEqual(before, state.read_bytes())
        self.assertEqual(str(state), result["retained_evidence"][0]["path"])
        self.assertTrue(result["remote_work_may_continue"])

    def test_writer_lease_is_shared_and_unresolved_owner_cannot_be_replaced(self):
        with mock.patch.dict(EXECUTION.os.environ, {"COPILOT_HOME": str(self.root / "home")}):
            context = self.context()
            context.claim_writers([("Owner/Repo", "branch")])
            second = EXECUTION.Execution(self.root / "second.json", command=["python"])
            with self.assertRaisesRegex(EXECUTION.ExecutionError, "unresolved execution owner"):
                second.claim_writers([("owner/repo", "branch")])
            context.finish(130, cancelled=True)
            with self.assertRaisesRegex(EXECUTION.ExecutionError, "unresolved execution owner"):
                second.claim_writers([("owner/repo", "branch")])

    def test_successful_owner_releases_only_its_own_writer_lease(self):
        with mock.patch.dict(EXECUTION.os.environ, {"COPILOT_HOME": str(self.root / "home")}):
            context = self.context()
            context.claim_writers([("owner/repo", "branch")])
            context.emit({"result": "complete"})
            context.finish(0)
            second = EXECUTION.Execution(self.root / "second.json", command=["python"])
            second.claim_writers([("owner/repo", "branch")])

    def test_terminal_write_failure_does_not_release_branch_ownership(self):
        for fail_seal in (False, True):
            with self.subTest(fail_seal=fail_seal), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                with mock.patch.dict(EXECUTION.os.environ, {"COPILOT_HOME": str(root / "home")}):
                    context = EXECUTION.Execution(root / "first.json", command=["python"],
                                                  terminal_results=frozenset({"complete"}))
                    context.claim_writers([("owner/repo", "branch")])
                    context.emit({"result": "complete"})
                    original = EXECUTION.write

                    def fail_terminal(path, value, **options):
                        target = context.handle if fail_seal else Path(context.record["result"])
                        if path == target:
                            raise OSError("terminal sink unavailable")
                        return original(path, value, **options)

                    with mock.patch.object(EXECUTION, "write", side_effect=fail_terminal):
                        with self.assertRaisesRegex(OSError, "terminal sink unavailable"):
                            context.finish(0)
                    self.assertFalse(EXECUTION.status(context.handle)["terminal"])
                    second = EXECUTION.Execution(root / "second.json", command=["python"])
                    with self.assertRaisesRegex(EXECUTION.ExecutionError, "unresolved execution owner"):
                        second.claim_writers([("owner/repo", "branch")])

    def test_zero_exit_with_unknown_workflow_outcome_retains_ownership(self):
        with mock.patch.dict(EXECUTION.os.environ, {"COPILOT_HOME": str(self.root / "home")}):
            context = self.context()
            context.claim_writers([("owner/repo", "branch")])
            context.emit({"result": "invocation_abandoned", "task_id": "known-task"})
            result = context.finish(0)
            self.assertTrue(result["remote_work_may_continue"])
            self.assertEqual("retained", result["writer_ownership"])
            self.assertEqual("known-task", result["workflow_result"]["task_id"])

    def test_finalization_failure_keeps_original_error_in_file_backed_stderr(self):
        def controller():
            raise ValueError("original controller failure")

        with (
            mock.patch.dict(EXECUTION.os.environ, {}, clear=True),
            mock.patch.object(sys, "argv", ["controller.py", "run"]),
            mock.patch.object(EXECUTION.Execution, "finish", side_effect=OSError("terminal unavailable")),
        ):
            with self.assertRaisesRegex(EXECUTION.ExecutionError, "original controller failure"):
                EXECUTION.controller_main(controller, {}, handle=self.handle)
        stderr = self.handle.with_name(self.handle.name + ".d") / "stderr.log"
        text = stderr.read_text(encoding="utf-8")
        self.assertIn("original controller failure", text)
        self.assertIn("terminal unavailable", text)
        self.assertFalse(EXECUTION.status(self.handle)["terminal"])

    def test_output_sink_failure_has_a_failed_terminal_record(self):
        original = Path.open

        def opened(path, *args, **kwargs):
            if path.name == "stdout.log":
                raise OSError("output sink unavailable")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "open", opened), mock.patch.dict(EXECUTION.os.environ, {}, clear=True):
            self.assertEqual(1, EXECUTION.controller_main(lambda: 0, {}, handle=self.handle))
        result = EXECUTION.status(self.handle)
        self.assertEqual("failed", result["local_status"])
        self.assertIn("output sink unavailable", result["error"])

    def test_cancellation_arriving_during_final_drain_wins_before_sealing(self):
        context = self.context()
        context.emit({"result": "complete"})
        child = mock.Mock()
        child.record = context.directory / "child-final.json"
        EXECUTION.write(child.record, {"root": str(context.root), "local_drained": True})
        child.poll.side_effect = lambda: (EXECUTION.cancel(self.handle), 0)[1]
        context.children.append(child)
        result = context.finish(0)
        self.assertEqual(130, result["exit_code"])
        self.assertEqual("cancelled_local", result["local_status"])
        self.assertEqual("retained", result["writer_ownership"])

    def test_matching_shell_exit_without_child_terminal_is_rejected(self):
        record = self.root / "child.json"
        EXECUTION.write(record, {
            "process_identity": IDENTITY, "requires_execution_result": True,
            "handle": str(self.root / "missing-handle.json"), "run_id": "root",
        })
        child = EXECUTION.OwnedProcess(mock.Mock(pid=123), None, record, [])
        with self.assertRaises(FileNotFoundError):
            child.verify_execution(0)

    def test_denied_child_owner_fails_without_fallback_or_resume(self):
        context = self.context()
        process = mock.Mock(pid=456)
        process.poll.return_value = None
        with (
            mock.patch.object(EXECUTION, "IS_WINDOWS", True),
            mock.patch.object(EXECUTION, "guard", side_effect=lambda path: mock.MagicMock()),
            mock.patch.object(EXECUTION.subprocess, "Popen", return_value=process) as launch,
            mock.patch.object(EXECUTION, "WindowsOwner", side_effect=OSError("denied")),
            mock.patch.object(EXECUTION, "resume_process") as resume,
        ):
            with self.assertRaisesRegex(OSError, "denied"):
                context.start(["python", "child.py"])
        launch.assert_called_once()
        resume.assert_not_called()
        process.kill.assert_called_once()
        self.assertEqual(1, len(context.launch_failures))
        self.assertIn("denied", context.launch_failures[0]["error"])

    def test_failed_launch_cleanup_preserves_original_error_and_unknown_drainage(self):
        context = self.context()
        process = mock.Mock(pid=456)
        process.poll.return_value = None
        process.kill.side_effect = OSError("cannot terminate")
        with (
            mock.patch.object(EXECUTION, "IS_WINDOWS", True),
            mock.patch.object(EXECUTION, "guard", side_effect=lambda path: mock.MagicMock()),
            mock.patch.object(EXECUTION.subprocess, "Popen", return_value=process),
            mock.patch.object(EXECUTION, "WindowsOwner", side_effect=OSError("assignment denied")),
        ):
            with self.assertRaisesRegex(EXECUTION.ExecutionError, "assignment denied.*cannot terminate"):
                context.start(["python", "child.py"])
        result = context.finish(1, "assignment denied")
        self.assertFalse(result["local_children_drained"])
        self.assertIn("assignment denied", result["launch_failures"][0]["error"])
        self.assertEqual(["cannot terminate"], result["launch_failures"][0]["cleanup_errors"])

    def test_partial_workflow_is_preserved_without_a_green_shortcut(self):
        context = EXECUTION.Execution(self.handle, command=["python", "controller.py"],
                                      terminal_results=frozenset({"partial"}))
        context.emit({"result": "partial", "all_ci_passed": False, "review": "carried"})
        result = context.finish(0)
        self.assertEqual("partial", result["workflow_result"]["result"])
        self.assertFalse(result["workflow_result"]["all_ci_passed"])
        self.assertEqual("carried", result["workflow_result"]["review"])

    def test_invalid_retained_evidence_does_not_erase_original_failure(self):
        context = self.context()
        path = self.root / "invalid-state.json"
        context.record_state(path, {})
        path.write_text("invalid JSON", encoding="utf-8")
        result = context.finish(1, "original controller error")
        self.assertEqual("original controller error", result["error"])
        self.assertEqual("failed", result["local_status"])
        self.assertTrue(result["finalization_errors"])

    def test_pipeline_foreground_routes_through_shared_owned_wait(self):
        path = ROOT / "plugins" / "pr-pipeline" / "scripts" / "pipeline_common.py"
        spec = importlib.util.spec_from_file_location("execution_pipeline_adapter_test", path)
        common = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(common)
        run = mock.Mock(return_value=subprocess.CompletedProcess(["python"], 0))
        common._EXECUTION = types.SimpleNamespace(run=run)
        result = common.run_foreground(["python", "stage.py"], cwd=self.root, log_path=self.root / "stage.log")
        self.assertEqual(0, result["returncode"])
        run.assert_called_once()
        self.assertEqual(["python", "stage.py"], run.call_args.args[0])


class ExecutionBootstrapTest(unittest.TestCase):
    def test_every_controller_and_backend_verifies_the_same_source_without_a_window(self):
        paths = list(ROOT.glob("plugins/*/scripts/*.py"))
        paths.append(SCRIPT.with_name("cloud_task.py"))
        adapters = [path for path in paths if "def _load_execution():" in path.read_text(encoding="utf-8")]
        self.assertEqual(11, len(adapters))
        inventory = json.dumps([{"name": "agent-tasks-runtime", "source": "plugin",
                                 "enabled": True, "path": str(SCRIPT.parent.parent)}])
        for index, path in enumerate(adapters):
            with self.subTest(path=path):
                spec = importlib.util.spec_from_file_location(f"execution_adapter_{index}", path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                self.addCleanup(sys.modules.pop, spec.name, None)
                spec.loader.exec_module(module)
                with (
                    mock.patch.object(module, "os", types.SimpleNamespace(name="nt")),
                    mock.patch.object(module.subprocess, "run",
                                      return_value=subprocess.CompletedProcess([], 0, inventory, "")) as launch,
                ):
                    loaded = module._load_execution()
                    self.assertEqual(EXECUTION.SCHEMA, loaded.SCHEMA)
                    self.assertEqual(0x08000000, launch.call_args.kwargs["creationflags"])
                    self.assertEqual(["copilot", "skill", "list", "--json"], launch.call_args.args[0])
                    with mock.patch.object(module, "EXECUTION_SHA256", "0" * 64):
                        with self.assertRaisesRegex(RuntimeError, "digest changed"):
                            module._load_execution()
                    launch.return_value.stdout = "[]"
                    with self.assertRaisesRegex(RuntimeError, "uniquely installed"):
                        module._load_execution()
                if path.name not in {"cloud_task.py", "cloud_conflict_task.py"}:
                    self.assertIsInstance(module.EXECUTION_TERMINAL_RESULTS, frozenset)
                    self.assertNotIn("invocation_abandoned", module.EXECUTION_TERMINAL_RESULTS)
                    self.assertNotIn("error", module.EXECUTION_TERMINAL_RESULTS)
                    command = "run" if path.name in {
                        "pr_pipeline.py", "pr_stack_pipeline.py", "pr_reviewer.py",
                    } else ("pipeline" if path.name == "ci_fix_loop.py" else "agent-task")
                    shared = mock.Mock()
                    shared.entrypoint.return_value = 0
                    with (
                        mock.patch.object(module, "_load_execution", return_value=shared),
                        mock.patch.object(module, "main") as main,
                        mock.patch.object(sys, "argv", [str(path), command, "--execution-handle", "unused"]),
                    ):
                        self.assertEqual(0, module.execution_main())
                        main.assert_not_called()
                        self.assertIs(main, shared.entrypoint.call_args.args[0])
                    if path.name in {"pr_pipeline.py", "pr_stack_pipeline.py"}:
                        with (
                            mock.patch.object(module.common, "_EXECUTION",
                                              types.SimpleNamespace(run_id="b" * 32)),
                            mock.patch.object(module.common, "require_tools") as tools,
                        ):
                            arguments = types.SimpleNamespace(github_mutation_policy="allow", run_id="a" * 32)
                            with self.assertRaisesRegex(module.WorkflowError, "omit --run-id"):
                                module.command_run(arguments)
                            self.assertEqual("b" * 32, arguments.run_id)
                            tools.assert_not_called()
                        with (
                            mock.patch.object(module, "_load_execution") as loader,
                            mock.patch.object(module, "main", return_value=7),
                            mock.patch.object(sys, "argv", [str(path), "start"]),
                        ):
                            self.assertEqual(7, module.execution_main())
                            loader.assert_not_called()
                    else:
                        pr = {"head_owner": "fork", "head_repo": "repo", "head_branch": "feature"}
                        state = {"pr": pr}
                        expected = ("fork/repo", "feature")
                        if path.name == "historical_pr_audit.py":
                            state = {"pr": {"repo_name": "owner/repo"}, "audit_branch": "audit"}
                            expected = ("owner/repo", "audit")
                        elif path.name in {"pr_description.py", "pr_reviewer.py"}:
                            state = {"pr": {"head": {"repository": "fork/repo", "ref": "feature"}}}
                        execution = mock.Mock()
                        with tempfile.TemporaryDirectory() as directory, mock.patch.object(module, "_EXECUTION", execution):
                            state_path = Path(directory) / "state.json"
                            saved = getattr(module, "save_state", None) or module.save_run_state
                            saved(state_path, state)
                            execution.claim_writers.assert_called_once_with([expected])
                            execution.record_state.assert_called_once_with(state_path, state)
                            if path.name == "pr_conflict_resolver.py":
                                execution.reset_mock()
                                saved(state_path, {"pr": {"number": 1, "head_branch": None}})
                                execution.claim_writers.assert_not_called()


if __name__ == "__main__":
    unittest.main()
