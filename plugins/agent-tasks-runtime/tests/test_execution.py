import hashlib
import importlib.util
import io
import json
import ctypes
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
ORIGINAL_PROCESS_IDENTITY = EXECUTION.process_identity
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

    def windows_owned(self, process, owner, record, streams=None):
        identity = EXECUTION.read(record)["process_identity"]
        binding = {
            "handle": process._handle,
            "identity": dict(identity),
            "job_provenance": "verified_live_handle",
            "image_provenance": "queried_live",
        }
        return EXECUTION.OwnedProcess(
            process, owner, record, [] if streams is None else streams, binding
        )

    def child_evidence(self, context, *, directory=None, name="stage", **outcome):
        directory = directory or context.directory
        handle = directory / f"handle-{name}.json"
        output = handle.with_name(handle.name + ".d")
        result_path = output / "result.json"
        state = output / "state.json"
        dispatch = output / "result.json.dispatch.json"
        owner = {**IDENTITY, "pid": 456}
        command_sha256 = EXECUTION.digest(["python", name])
        result = {
            "schema": EXECUTION.SCHEMA, "run_id": context.run_id, "owner": owner,
            "terminal": True, "exit_code": 0, "local_status": "finished",
            "local_children_drained": True, "remote_work_may_continue": False,
            **outcome,
        }
        EXECUTION.write(result_path, result)
        EXECUTION.write(state, {"iterations_used": 3, "task_id": f"task-{name}"})
        EXECUTION.write(dispatch, {
            "schema": "github.copilot.dispatch-observation.v1", "request_id": name,
            "repository": "owner/repo", "task": {"id": f"task-{name}"},
            "remote_status": "unconfirmed",
        })
        EXECUTION.write(handle, {
            "schema": EXECUTION.SCHEMA, "handle": str(handle), "root": str(context.root),
            "run_id": context.run_id, "owner": owner, "status": "ready",
            "command_sha256": command_sha256, "result": str(result_path),
            "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
            "domain_states": [str(state), str(dispatch)],
            **{name: str(output / name) for name in ("stdout", "stderr", "progress")},
        })
        record = directory / f"child-{name}.json"
        EXECUTION.write(record, {
            "schema": EXECUTION.SCHEMA, "root": str(context.root), "run_id": context.run_id,
            "handle": str(handle), "process_identity": owner, "command_sha256": command_sha256,
            "requires_execution_result": True, "local_drained": True,
            "result_file": str(result_path),
        })
        return handle, result_path, state, record

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

    def test_unsealed_exited_root_with_unavailable_image_is_abandoned(self):
        with mock.patch.dict(
            EXECUTION.os.environ, {"COPILOT_HOME": str(self.root / "home")}
        ):
            context = self.context()
            before = {
                path: path.read_bytes()
                for path in self.root.rglob("*")
                if path.is_file()
            }
            observed = {
                "pid": IDENTITY["pid"],
                "creation_time": IDENTITY["creation_time"],
                "image": None,
                "running": False,
                "image_provenance": "unavailable_after_exit",
            }
            with mock.patch.object(
                EXECUTION, "process_identity", return_value=observed
            ):
                result = EXECUTION.status(context.handle)

            self.assertFalse(result["terminal"])
            self.assertEqual("abandoned", result["status"])
            self.assertEqual("unconfirmed", result["remote_status"])
            self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_windows_process_identity_accepts_already_signaled_handle_without_image(self):
        handle = object()

        def get_process_times(observed, creation, _exit, _kernel, _user):
            self.assertIs(handle, observed)
            creation._obj.dwHighDateTime = 1
            creation._obj.dwLowDateTime = 2
            return True

        kernel = types.SimpleNamespace(
            OpenProcess=mock.Mock(return_value=handle),
            GetProcessTimes=mock.Mock(side_effect=get_process_times),
            QueryFullProcessImageNameW=mock.Mock(),
            WaitForSingleObject=mock.Mock(return_value=0),
            CloseHandle=mock.Mock(return_value=True),
        )
        with (
            mock.patch.object(EXECUTION, "IS_WINDOWS", True),
            mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True),
        ):
            self.assertEqual(
                {
                    "pid": 456,
                    "creation_time": str((1 << 32) | 2),
                    "image": None,
                    "running": False,
                    "image_provenance": "unavailable_after_exit",
                },
                ORIGINAL_PROCESS_IDENTITY(456),
            )

        kernel.GetProcessTimes.assert_called_once()
        self.assertIs(handle, kernel.GetProcessTimes.call_args.args[0])
        kernel.WaitForSingleObject.assert_called_once_with(handle, 0)
        kernel.QueryFullProcessImageNameW.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(handle)

    def test_windows_process_identity_handles_live_to_exited_image_race_on_same_handle(self):
        handle = object()

        def get_process_times(observed, creation, _exit, _kernel, _user):
            self.assertIs(handle, observed)
            creation._obj.dwHighDateTime = 1
            creation._obj.dwLowDateTime = 2
            return True

        def query_image(observed, _flags, _image, _size):
            self.assertIs(handle, observed)
            return False

        kernel = types.SimpleNamespace(
            OpenProcess=mock.Mock(return_value=handle),
            GetProcessTimes=mock.Mock(side_effect=get_process_times),
            QueryFullProcessImageNameW=mock.Mock(side_effect=query_image),
            WaitForSingleObject=mock.Mock(side_effect=[258, 0]),
            CloseHandle=mock.Mock(return_value=True),
        )
        image_failure = OSError("image unavailable")
        image_failure.winerror = 31
        with (
            mock.patch.object(EXECUTION, "IS_WINDOWS", True),
            mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True),
            mock.patch.object(
                ctypes, "get_last_error", return_value=31, create=True
            ),
            mock.patch.object(
                ctypes, "WinError", return_value=image_failure, create=True
            ),
        ):
            self.assertEqual(
                {
                    "pid": 456,
                    "creation_time": str((1 << 32) | 2),
                    "image": None,
                    "running": False,
                    "image_provenance": "unavailable_after_exit",
                },
                ORIGINAL_PROCESS_IDENTITY(456),
            )

        self.assertEqual(
            [mock.call(handle, 0), mock.call(handle, 0)],
            kernel.WaitForSingleObject.call_args_list,
        )
        kernel.CloseHandle.assert_called_once_with(handle)

    def test_windows_process_identity_rejects_unavailable_image_while_handle_live(self):
        handle = object()

        def get_process_times(observed, creation, _exit, _kernel, _user):
            self.assertIs(handle, observed)
            creation._obj.dwHighDateTime = 1
            creation._obj.dwLowDateTime = 2
            return True

        kernel = types.SimpleNamespace(
            OpenProcess=mock.Mock(return_value=handle),
            GetProcessTimes=mock.Mock(side_effect=get_process_times),
            QueryFullProcessImageNameW=mock.Mock(return_value=False),
            WaitForSingleObject=mock.Mock(side_effect=[258, 258]),
            CloseHandle=mock.Mock(return_value=True),
        )
        image_failure = OSError("image unavailable")
        image_failure.winerror = 31
        with (
            mock.patch.object(EXECUTION, "IS_WINDOWS", True),
            mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True),
            mock.patch.object(
                ctypes, "get_last_error", return_value=31, create=True
            ),
            mock.patch.object(
                ctypes, "WinError", return_value=image_failure, create=True
            ),
            self.assertRaises(OSError) as failure,
        ):
            ORIGINAL_PROCESS_IDENTITY(456)

        self.assertIs(image_failure, failure.exception)
        kernel.CloseHandle.assert_called_once_with(handle)

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
        identity = {**IDENTITY, "pid": 456}
        binding = {
            "handle": process._handle,
            "identity": identity,
            "job_provenance": "verified_live_handle",
            "image_provenance": "queried_live",
        }
        owner.bind_process.return_value = binding
        owner.process.return_value = {**identity, "running": False}
        owner.processes.return_value = []
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
        owner.bind_process.assert_called_once_with(process._handle, 456)
        resume.assert_called_once_with(456)
        child.wait()

    def test_windows_accounting_residue_drains_without_terminating(self):
        context = self.context()
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123)
        process.wait.return_value = 0
        owner = mock.Mock()
        owner.process.return_value = {**IDENTITY, "running": False}
        events = []
        owner.processes.side_effect = lambda _deadline, _retained: (
            events.append("observe") or [{**IDENTITY, "running": False}]
        )
        process._handle.Close.side_effect = lambda: events.append("close")
        owner.drain.side_effect = lambda _timeout: events.append("drain")
        child = self.windows_owned(process, owner, record)

        self.assertEqual(0, child.wait(timeout=5.0))

        owner.terminate.assert_not_called()
        owner.drain.assert_called_once()
        process._handle.Close.assert_called_once()
        self.assertEqual(["observe", "close", "drain"], events)
        self.assertTrue(child.drained)
        self.assertTrue(EXECUTION.read(record)["local_drained"])

    def test_signaled_descendant_residue_drains_without_terminating(self):
        context = self.context()
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123)
        process.wait.return_value = 0
        owner = mock.Mock()
        owner.process.return_value = {**IDENTITY, "running": False}
        owner.processes.return_value = [
            {**IDENTITY, "running": False},
            {"pid": 456, "creation_time": "789", "image": "nested.exe", "running": False},
        ]
        child = self.windows_owned(process, owner, record)

        self.assertEqual(0, child.wait())

        owner.terminate.assert_not_called()
        owner.drain.assert_called_once()

    def test_running_descendant_naturally_drains_before_deadline(self):
        context = self.context()
        context.emit({"result": "complete"})
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123)
        process.poll.return_value = 0
        process.wait.return_value = 0
        descendant = {
            "pid": 456, "creation_time": "789", "image": "nested.exe", "running": True,
        }
        owner = mock.Mock()
        owner.process.return_value = {**IDENTITY, "running": False}
        events = []
        owner.processes.side_effect = lambda _deadline, _retained: (
            events.append("observe")
            or [{**IDENTITY, "running": False}, descendant]
        )
        process._handle.Close.side_effect = lambda: events.append("close")
        owner.drain.side_effect = lambda _deadline: events.append("zero")
        child = self.windows_owned(process, owner, record)
        context.children.append(child)

        self.assertEqual(0, child.wait())
        self.assertEqual(0, child.wait())
        self.assertEqual(0, child.poll())

        result = context.finish(0)
        receipt = EXECUTION.read(record)
        owner.terminate.assert_not_called()
        owner.drain.assert_called_once()
        owner.close.assert_called_once()
        self.assertEqual(["observe", "close", "zero"], events)
        self.assertTrue(child.drained)
        self.assertTrue(receipt["local_drained"])
        self.assertNotIn("completion_error", receipt)
        self.assertEqual([descendant], receipt["observed_descendants"])
        self.assertEqual("finished", result["local_status"])
        self.assertTrue(result["local_children_drained"])
        self.assertFalse(result["remote_work_may_continue"])

    def test_finalization_naturally_drains_poll_detected_descendant(self):
        context = self.context()
        context.emit({"result": "complete"})
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123)
        process.poll.return_value = 0
        descendant = {
            "pid": 456, "creation_time": "789", "image": "nested.exe", "running": True,
        }
        owner = mock.Mock()
        owner.process.return_value = {**IDENTITY, "running": False}
        owner.processes.return_value = [{**IDENTITY, "running": False}, descendant]
        owner.active_count.return_value = 1
        child = self.windows_owned(process, owner, record)
        context.children.append(child)

        self.assertIsNone(child.poll())
        self.assertIs(owner, child.owner)
        result = context.finish(0)

        owner.terminate.assert_not_called()
        owner.drain.assert_called_once()
        self.assertTrue(child.drained)
        self.assertIsNone(child.completion_error)
        self.assertEqual("finished", result["local_status"])
        self.assertTrue(result["local_children_drained"])
        self.assertFalse(result["remote_work_may_continue"])

    def test_finalization_waits_for_poll_pending_accounting_without_cancelling(self):
        context = self.context()
        context.emit({"result": "complete"})
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123)
        process.poll.return_value = 0
        owner = mock.Mock()
        owner.process.return_value = {**IDENTITY, "running": False}
        owner.processes.return_value = [{**IDENTITY, "running": False}]
        owner.active_count.return_value = 1
        child = self.windows_owned(process, owner, record)
        context.children.append(child)

        result = context.finish(0)

        owner.terminate.assert_not_called()
        owner.drain.assert_called_once()
        self.assertTrue(child.drained)
        self.assertEqual("finished", result["local_status"])
        self.assertTrue(result["local_children_drained"])
        self.assertFalse(result["remote_work_may_continue"])

    def test_running_descendant_preserves_failure_when_cleanup_does_not_drain(self):
        context = self.context()
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123)
        process.wait.return_value = 0
        descendant = {
            "pid": 456, "creation_time": "789", "image": "nested.exe", "running": True,
        }
        owner = mock.Mock()
        owner.process.return_value = {**IDENTITY, "running": False}
        owner.processes.return_value = [{**IDENTITY, "running": False}, descendant]
        owner.drain.side_effect = EXECUTION.ExecutionError("descendants remain")
        child = self.windows_owned(process, owner, record)

        with self.assertRaisesRegex(EXECUTION.ExecutionError, "descendants remain"):
            child.wait()
        with self.assertRaisesRegex(EXECUTION.ExecutionError, "descendants remain"):
            child.wait()

        receipt = EXECUTION.read(record)
        owner.terminate.assert_not_called()
        self.assertFalse(child.drained)
        self.assertFalse(receipt["local_drained"])
        self.assertNotIn("completion_error", receipt)
        self.assertEqual("descendants remain", receipt["drainage_error"])
        self.assertEqual([descendant], receipt["observed_descendants"])

    def test_forced_drainage_after_zero_exit_is_sticky_failure(self):
        context = self.context()
        context.emit({"result": "complete"})
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123, args=["python"])
        process.wait.return_value = 0
        process.poll.return_value = 0
        descendant = {
            "pid": 456, "creation_time": "789", "image": "helper.exe", "running": True,
        }
        owner = mock.Mock()
        owner.process.return_value = {**IDENTITY, "running": False}
        owner.processes.return_value = [{**IDENTITY, "running": False}, descendant]
        owner.drain.side_effect = [
            EXECUTION._OwnershipPending("descendant remains"),
            None,
        ]
        child = self.windows_owned(process, owner, record)
        context.children.append(child)

        with self.assertRaises(subprocess.TimeoutExpired):
            child.wait(timeout=0)
        self.assertIs(owner, child.owner)
        self.assertFalse(child.drained)

        message = EXECUTION.FORCED_DRAINAGE_ERROR
        with self.assertRaisesRegex(EXECUTION.ExecutionError, message):
            child.terminate_tree(timeout=5.0)
        with self.assertRaisesRegex(EXECUTION.ExecutionError, message):
            child.wait()
        with self.assertRaisesRegex(EXECUTION.ExecutionError, message):
            child.poll()

        result = context.finish(0)
        receipt = EXECUTION.read(record)
        owner.terminate.assert_called_once()
        self.assertEqual(2, owner.drain.call_count)
        self.assertTrue(child.drained)
        self.assertTrue(receipt["local_drained"])
        self.assertEqual(message, receipt["completion_error"])
        self.assertEqual([descendant], receipt["observed_descendants"])
        self.assertEqual("failed", result["local_status"])
        self.assertTrue(result["local_children_drained"])
        self.assertIn(message, result["finalization_errors"])
        self.assertTrue(result["remote_work_may_continue"])

    def test_windows_observation_failure_is_sticky_and_never_records_drained(self):
        context = self.context()
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123)
        process.wait.return_value = 0
        owner = mock.Mock()
        owner.process.return_value = {**IDENTITY, "running": False}
        owner.processes.side_effect = EXECUTION.ExecutionError("job observation failed")
        child = self.windows_owned(process, owner, record)

        with self.assertRaisesRegex(EXECUTION.ExecutionError, "job observation failed"):
            child.wait()
        with self.assertRaisesRegex(EXECUTION.ExecutionError, "job observation failed"):
            child.wait()

        owner.processes.assert_called_once()
        self.assertFalse(child.drained)
        self.assertFalse(EXECUTION.read(record)["local_drained"])

    def test_windows_direct_handle_must_remain_bound_to_the_owned_job(self):
        context = self.context()
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123)
        process.wait.return_value = 0
        owner = mock.Mock()
        owner.process.side_effect = EXECUTION.ExecutionError(
            "observed process handle is not a member of the owned Windows job"
        )
        child = self.windows_owned(process, owner, record)

        with self.assertRaisesRegex(EXECUTION.ExecutionError, "not a member"):
            child.wait()
        with self.assertRaisesRegex(EXECUTION.ExecutionError, "not a member"):
            child.poll()

        owner.process.assert_called_once_with(
            process._handle, 123, child.process_binding
        )
        owner.processes.assert_not_called()
        self.assertFalse(child.drained)
        self.assertFalse(EXECUTION.read(record)["local_drained"])

    def test_windows_wait_and_drain_share_one_deadline(self):
        context = self.context()
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123)
        process.wait.return_value = 0
        owner = mock.Mock()
        owner.process.return_value = {**IDENTITY, "running": False}
        owner.processes.return_value = []
        child = self.windows_owned(process, owner, record)

        with mock.patch.object(EXECUTION.time, "monotonic", side_effect=[100.0, 102.0]):
            self.assertEqual(0, child.wait(timeout=10.0))

        self.assertEqual(8.0, process.wait.call_args.kwargs["timeout"])
        owner.processes.assert_called_once_with(
            110.0, {123: child.process_binding}
        )
        owner.drain.assert_called_once_with(110.0)

    def test_windows_poll_keeps_ownership_while_accounting_is_pending(self):
        context = self.context()
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123)
        process.poll.return_value = 0
        owner = mock.Mock()
        owner.process.return_value = {**IDENTITY, "running": False}
        owner.processes.return_value = [{**IDENTITY, "running": False}]
        owner.active_count.side_effect = [1, 0]
        child = self.windows_owned(process, owner, record)

        self.assertIsNone(child.poll())
        owner.drain.assert_not_called()
        owner.close.assert_not_called()
        self.assertIs(owner, child.owner)
        self.assertFalse(child.drained)
        self.assertFalse(EXECUTION.read(record)["local_drained"])

        self.assertEqual(0, child.poll())
        owner.drain.assert_not_called()
        owner.close.assert_called_once()
        self.assertTrue(child.drained)

    def test_windows_poll_does_not_turn_truncated_membership_into_failure(self):
        context = self.context()
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123)
        process.poll.return_value = 0
        owner = mock.Mock()
        owner.process.return_value = {**IDENTITY, "running": False}
        owner.processes.side_effect = EXECUTION._OwnershipPending("snapshot truncated")
        child = self.windows_owned(process, owner, record)

        self.assertIsNone(child.poll())

        self.assertIsNone(child.drainage_error)
        self.assertIs(owner, child.owner)
        self.assertFalse(child.process_handle_closed)
        owner.close.assert_not_called()

    def test_windows_zero_timeout_keeps_pending_job_for_repeated_wait(self):
        context = self.context()
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123, args=["python"])
        process.wait.return_value = 0
        owner = mock.Mock()
        owner.process.return_value = {**IDENTITY, "running": False}
        owner.processes.return_value = [{**IDENTITY, "running": False}]
        owner.drain.side_effect = [
            EXECUTION._OwnershipPending("accounting pending"),
            None,
        ]
        child = self.windows_owned(process, owner, record)

        with mock.patch.object(EXECUTION.time, "monotonic", side_effect=[10.0, 10.0]):
            with self.assertRaises(subprocess.TimeoutExpired):
                child.wait(timeout=0)

        self.assertIs(owner, child.owner)
        self.assertFalse(child.drained)
        self.assertIsNone(child.drainage_error)
        owner.close.assert_not_called()

        with mock.patch.object(EXECUTION.time, "monotonic", side_effect=[20.0]):
            self.assertEqual(0, child.wait(timeout=1))
        self.assertTrue(child.drained)
        self.assertEqual(
            [mock.call(10.0), mock.call(21.0)],
            owner.drain.call_args_list,
        )

    def test_windows_timeout_cleanup_does_not_reset_deadline(self):
        context = self.context()
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123)
        process.wait.side_effect = [
            subprocess.TimeoutExpired(["python"], 7.0),
            1,
        ]
        owner = mock.Mock()
        owner.process.return_value = {**IDENTITY, "running": False}
        owner.processes.return_value = []
        child = self.windows_owned(process, owner, record)

        with mock.patch.object(
            EXECUTION.time,
            "monotonic",
            side_effect=[100.0, 103.0, 109.0],
        ):
            with self.assertRaisesRegex(
                EXECUTION.ExecutionError, EXECUTION.FORCED_DRAINAGE_ERROR
            ):
                child.terminate_tree(timeout=10.0)

        self.assertEqual(
            [mock.call(timeout=7.0), mock.call(timeout=1.0)],
            process.wait.call_args_list,
        )
        process.kill.assert_not_called()
        owner.drain.assert_called_once_with(110.0)
        self.assertTrue(child.drained)
        self.assertTrue(EXECUTION.read(record)["local_drained"])

    def test_windows_process_observation_retries_vanished_member(self):
        owner = object.__new__(EXECUTION.WindowsOwner)
        owner.process_ids = mock.Mock(
            side_effect=[(123, 456), (123,), (123,), (123,)]
        )
        owner.open_process = mock.Mock(return_value=None)
        owner.process = mock.Mock(return_value={**IDENTITY, "running": False})
        owner.kernel = types.SimpleNamespace(CloseHandle=mock.Mock(return_value=True))
        retained = {
            "handle": 1230,
            "identity": dict(IDENTITY),
            "job_provenance": "verified_live_handle",
            "image_provenance": "queried_live",
        }

        self.assertEqual(
            [{**IDENTITY, "running": False}],
            owner.processes(EXECUTION.time.monotonic() + 1.0, {123: retained}),
        )
        owner.kernel.CloseHandle.assert_not_called()

    def test_windows_process_observation_rejects_stable_missing_identity(self):
        owner = object.__new__(EXECUTION.WindowsOwner)
        owner.process_ids = mock.Mock(side_effect=[(456,), (456,)])
        owner.open_process = mock.Mock(return_value=None)
        owner.kernel = types.SimpleNamespace(CloseHandle=mock.Mock(return_value=True))
        with self.assertRaisesRegex(
            EXECUTION.ExecutionError,
            "owned Windows job process identity is unavailable",
        ):
            owner.processes(EXECUTION.time.monotonic() + 1.0, {})

    def test_windows_process_observation_rejects_stable_nonmember_generation(self):
        owner = object.__new__(EXECUTION.WindowsOwner)
        owner.process_ids = mock.Mock(side_effect=[(456,), (456,)])
        owner.open_process = mock.Mock(return_value=4560)
        owner.process = mock.Mock(
            side_effect=EXECUTION.ExecutionError(
                "observed process handle is not a member of the owned Windows job"
            )
        )
        owner.kernel = types.SimpleNamespace(CloseHandle=mock.Mock(return_value=True))

        with self.assertRaisesRegex(EXECUTION.ExecutionError, "not a member"):
            owner.processes(EXECUTION.time.monotonic() + 1.0, {})

        owner.kernel.CloseHandle.assert_called_once_with(4560)

    def test_windows_process_observation_closes_handle_after_query_failure(self):
        owner = object.__new__(EXECUTION.WindowsOwner)
        owner.process_ids = mock.Mock(side_effect=[(456,), (456,)])
        owner.open_process = mock.Mock(return_value=4560)
        owner.process = mock.Mock(side_effect=OSError("membership query denied"))
        owner.kernel = types.SimpleNamespace(CloseHandle=mock.Mock(return_value=True))

        with self.assertRaisesRegex(OSError, "membership query denied"):
            owner.processes(EXECUTION.time.monotonic() + 1.0, {})

        owner.kernel.CloseHandle.assert_called_once_with(4560)

    def test_windows_process_identity_comes_from_handle_bound_to_exact_job(self):
        owner = object.__new__(EXECUTION.WindowsOwner)
        owner.handle = 900
        calls = []

        def is_process_in_job(handle, job, member):
            calls.append(("member", handle, job))
            member._obj.value = 1
            return True

        def get_process_times(handle, creation, _exit, _kernel, _user):
            calls.append(("times", handle))
            creation._obj.dwHighDateTime = 1
            creation._obj.dwLowDateTime = 2
            return True

        def query_image(handle, _flags, image, _size):
            calls.append(("image", handle))
            image.value = "C:\\Python\\python.exe"
            return True

        def wait(handle, timeout):
            calls.append(("wait", handle, timeout))
            return [258, 0][sum(call[0] == "wait" for call in calls) - 1]

        owner.kernel = types.SimpleNamespace(
            IsProcessInJob=is_process_in_job,
            GetProcessTimes=get_process_times,
            QueryFullProcessImageNameW=query_image,
            WaitForSingleObject=wait,
        )

        self.assertEqual(
            {
                "pid": 456,
                "creation_time": str((1 << 32) | 2),
                "image": EXECUTION.os.path.normcase("C:\\Python\\python.exe"),
                "running": False,
                "job_provenance": "verified_handle",
                "image_provenance": "queried_live",
            },
            owner.process(4560, 456),
        )
        self.assertEqual(
            [
                ("member", 4560, 900),
                ("times", 4560),
                ("wait", 4560, 0),
                ("image", 4560),
                ("wait", 4560, 0),
            ],
            calls,
        )

    def test_windows_bound_exited_process_uses_cached_image_without_query(self):
        owner = object.__new__(EXECUTION.WindowsOwner)
        owner.handle = 900
        owner._binding_token = object()
        handle = object()
        identity = {
            "pid": 456,
            "creation_time": str((1 << 32) | 2),
            "image": "python.exe",
            "running": True,
        }

        def get_process_times(observed, creation, _exit, _kernel, _user):
            self.assertIs(handle, observed)
            creation._obj.dwHighDateTime = 1
            creation._obj.dwLowDateTime = 2
            return True

        owner.kernel = types.SimpleNamespace(
            IsProcessInJob=mock.Mock(),
            GetProcessTimes=get_process_times,
            QueryFullProcessImageNameW=mock.Mock(
                side_effect=AssertionError("post-exit image query")
            ),
            WaitForSingleObject=mock.Mock(return_value=0),
        )
        binding = {
            "handle": handle,
            "owner_token": owner._binding_token,
            "job_handle": owner.handle,
            "identity": identity,
            "job_provenance": "verified_live_handle",
            "image_provenance": "queried_live",
        }

        self.assertEqual(
            {
                **identity,
                "running": False,
                "job_provenance": "cached_binding",
                "image_provenance": "cached_binding",
            },
            owner.process(handle, 456, binding),
        )
        owner.kernel.IsProcessInJob.assert_not_called()
        owner.kernel.QueryFullProcessImageNameW.assert_not_called()

    def test_windows_bound_process_rejects_cross_owner_or_invalidated_job(self):
        owner = object.__new__(EXECUTION.WindowsOwner)
        owner.handle = 900
        owner._binding_token = object()
        handle = object()

        def is_process_in_job(observed, job, member):
            self.assertIs(handle, observed)
            self.assertEqual(900, job)
            member._obj.value = 1
            return True

        def get_process_times(observed, creation, _exit, _kernel, _user):
            self.assertIs(handle, observed)
            creation._obj.dwHighDateTime = 1
            creation._obj.dwLowDateTime = 2
            return True

        def query_image(observed, _flags, image, _size):
            self.assertIs(handle, observed)
            image.value = "C:\\Python\\python.exe"
            return True

        kernel = types.SimpleNamespace(
            IsProcessInJob=mock.Mock(side_effect=is_process_in_job),
            GetProcessTimes=mock.Mock(side_effect=get_process_times),
            QueryFullProcessImageNameW=mock.Mock(side_effect=query_image),
            WaitForSingleObject=mock.Mock(return_value=258),
        )
        owner.kernel = kernel
        binding = owner.bind_process(handle, 456)
        kernel.IsProcessInJob.reset_mock()
        kernel.GetProcessTimes.reset_mock()
        kernel.QueryFullProcessImageNameW.reset_mock()
        kernel.WaitForSingleObject.reset_mock()

        other = object.__new__(EXECUTION.WindowsOwner)
        other.handle = owner.handle
        other._binding_token = object()
        other.kernel = kernel
        with self.assertRaisesRegex(EXECUTION.ExecutionError, "binding is invalid"):
            other.process(handle, 456, binding)

        owner.handle = 901
        with self.assertRaisesRegex(EXECUTION.ExecutionError, "binding is invalid"):
            owner.process(handle, 456, binding)

        owner.handle = None
        owner._binding_token = None
        with self.assertRaisesRegex(EXECUTION.ExecutionError, "binding is invalid"):
            owner.process(handle, 456, binding)

        kernel.IsProcessInJob.assert_not_called()
        kernel.GetProcessTimes.assert_not_called()
        kernel.QueryFullProcessImageNameW.assert_not_called()
        kernel.WaitForSingleObject.assert_not_called()

    def test_windows_live_member_exit_during_image_query_has_explicit_provenance(self):
        owner = object.__new__(EXECUTION.WindowsOwner)
        owner.handle = 900

        def is_process_in_job(_handle, _job, member):
            member._obj.value = 1
            return True

        def get_process_times(_handle, creation, _exit, _kernel, _user):
            creation._obj.dwHighDateTime = 1
            creation._obj.dwLowDateTime = 2
            return True

        owner.kernel = types.SimpleNamespace(
            IsProcessInJob=is_process_in_job,
            GetProcessTimes=get_process_times,
            QueryFullProcessImageNameW=mock.Mock(return_value=False),
            WaitForSingleObject=mock.Mock(side_effect=[258, 0]),
        )

        image_failure = OSError("image unavailable")
        image_failure.winerror = 31
        with (
            mock.patch.object(
                ctypes, "get_last_error", return_value=31, create=True
            ),
            mock.patch.object(
                ctypes, "WinError", return_value=image_failure, create=True
            ),
        ):
            self.assertEqual(
                {
                    "pid": 456,
                    "creation_time": str((1 << 32) | 2),
                    "image": None,
                    "running": False,
                    "job_provenance": "verified_handle",
                    "image_provenance": "unavailable_after_exit",
                },
                owner.process(4560, 456),
            )

    def test_windows_live_member_rejects_unavailable_image(self):
        owner = object.__new__(EXECUTION.WindowsOwner)
        owner.handle = 900

        def is_process_in_job(_handle, _job, member):
            member._obj.value = 1
            return True

        def get_process_times(_handle, creation, _exit, _kernel, _user):
            creation._obj.dwHighDateTime = 1
            creation._obj.dwLowDateTime = 2
            return True

        owner.kernel = types.SimpleNamespace(
            IsProcessInJob=is_process_in_job,
            GetProcessTimes=get_process_times,
            QueryFullProcessImageNameW=mock.Mock(return_value=False),
            WaitForSingleObject=mock.Mock(side_effect=[258, 258]),
        )

        image_failure = OSError("image unavailable")
        image_failure.winerror = 31
        with (
            mock.patch.object(
                ctypes, "get_last_error", return_value=31, create=True
            ),
            mock.patch.object(
                ctypes, "WinError", return_value=image_failure, create=True
            ),
            self.assertRaises(OSError) as failure,
        ):
            owner.process(4560, 456)
        self.assertEqual(31, failure.exception.winerror)

    def test_windows_bound_process_rejects_wrong_generation(self):
        owner = object.__new__(EXECUTION.WindowsOwner)
        owner.handle = 900
        owner._binding_token = object()
        handle = object()

        def get_process_times(_handle, creation, _exit, _kernel, _user):
            creation._obj.dwHighDateTime = 1
            creation._obj.dwLowDateTime = 3
            return True

        owner.kernel = types.SimpleNamespace(
            GetProcessTimes=get_process_times,
            WaitForSingleObject=mock.Mock(),
            QueryFullProcessImageNameW=mock.Mock(),
        )
        binding = {
            "handle": handle,
            "owner_token": owner._binding_token,
            "job_handle": owner.handle,
            "identity": {
                "pid": 456,
                "creation_time": str((1 << 32) | 2),
                "image": "python.exe",
                "running": True,
            },
        }

        with self.assertRaisesRegex(EXECUTION.ExecutionError, "generation changed"):
            owner.process(handle, 456, binding)
        owner.kernel.WaitForSingleObject.assert_not_called()
        owner.kernel.QueryFullProcessImageNameW.assert_not_called()

    def test_windows_process_identity_rejects_nonmember_before_identity_queries(self):
        owner = object.__new__(EXECUTION.WindowsOwner)
        owner.handle = 900
        identity_query = mock.Mock()

        def is_process_in_job(_handle, _job, member):
            member._obj.value = 0
            return True

        owner.kernel = types.SimpleNamespace(
            IsProcessInJob=is_process_in_job,
            GetProcessTimes=identity_query,
        )

        with self.assertRaisesRegex(EXECUTION.ExecutionError, "not a member"):
            owner.process(4560, 456)

        identity_query.assert_not_called()

    def test_windows_process_list_retries_until_complete(self):
        owner = object.__new__(EXECUTION.WindowsOwner)
        owner.handle = 1
        calls = []

        def query(_handle, info_class, buffer, _size, _returned):
            self.assertEqual(3, info_class)
            value = buffer._obj
            calls.append(len(value.ids))
            value.assigned = 9
            value.listed = min(9, len(value.ids))
            for index in range(value.listed):
                value.ids[index] = index + 1
            return True

        owner.kernel = types.SimpleNamespace(QueryInformationJobObject=query)
        with mock.patch.object(
            ctypes, "get_last_error", return_value=0, create=True
        ):
            self.assertEqual(
                tuple(range(1, 10)),
                owner.process_ids(EXECUTION.time.monotonic() + 1.0),
            )
        self.assertEqual([8, 16], calls)

    def test_windows_process_list_timeout_never_accepts_partial_membership(self):
        owner = object.__new__(EXECUTION.WindowsOwner)
        owner.handle = 1

        def query(_handle, _info_class, buffer, _size, _returned):
            value = buffer._obj
            value.assigned = len(value.ids) + 1
            value.listed = len(value.ids)
            return True

        owner.kernel = types.SimpleNamespace(QueryInformationJobObject=query)
        with (
            mock.patch.object(
                ctypes, "get_last_error", return_value=0, create=True
            ),
            mock.patch.object(EXECUTION.time, "monotonic", return_value=2.0),
            self.assertRaisesRegex(
                EXECUTION._OwnershipPending,
                "owned Windows job process list did not stabilize",
            ),
        ):
            owner.process_ids(1.0)

    def test_windows_drain_checks_cancellation_before_accounting(self):
        owner = object.__new__(EXECUTION.WindowsOwner)
        owner.active_count = mock.Mock()

        def cancelled():
            raise EXECUTION.Cancelled("explicit local cancellation")

        with self.assertRaisesRegex(EXECUTION.Cancelled, "explicit local cancellation"):
            owner.drain(EXECUTION.time.monotonic() + 1.0, cancelled)

        owner.active_count.assert_not_called()

    def test_windows_forced_cancellation_drainage_is_sticky_failure(self):
        context = self.context()
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123)
        process.wait.return_value = 1
        descendant = {
            "pid": 456, "creation_time": "789", "image": "nested.exe", "running": True,
        }
        owner = mock.Mock()
        owner.process.return_value = {**IDENTITY, "running": False}
        owner.processes.return_value = [{**IDENTITY, "running": False}, descendant]
        child = self.windows_owned(process, owner, record)

        with self.assertRaisesRegex(
            EXECUTION.ExecutionError, EXECUTION.FORCED_DRAINAGE_ERROR
        ):
            child.terminate_tree(timeout=5.0)

        owner.terminate.assert_called_once()
        owner.drain.assert_called_once()
        self.assertEqual(EXECUTION.FORCED_DRAINAGE_ERROR, child.completion_error)
        self.assertTrue(child.drained)
        self.assertTrue(EXECUTION.read(record)["local_drained"])

    def test_descendant_drain_failure_never_records_drained(self):
        context = self.context()
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123)
        process.wait.return_value = 0
        owner = mock.Mock()
        owner.process.return_value = {**IDENTITY, "running": False}
        owner.processes.return_value = []
        owner.drain.side_effect = EXECUTION.ExecutionError("descendants remain")
        child = self.windows_owned(process, owner, record)
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
        child._wait.return_value = 0

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

    def test_run_does_not_reset_timeout_after_communicate(self):
        context = self.context()
        command = ["python", "child.py"]
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123, args=command)
        process.communicate.return_value = (None, None)
        process.wait.return_value = 0
        owner = mock.Mock()
        owner.process.return_value = {**IDENTITY, "running": False}
        owner.processes.return_value = [{**IDENTITY, "running": False}]
        owner.drain.side_effect = EXECUTION._OwnershipPending("accounting pending")
        child = self.windows_owned(process, owner, record)

        with (
            mock.patch.object(context, "start", return_value=child),
            mock.patch.object(child, "terminate_tree") as cleanup,
            mock.patch.object(
                EXECUTION.time, "monotonic", side_effect=[100.0, 109.0, 109.5]
            ),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            context.run(command, timeout=10.0)

        self.assertEqual(0.2, process.communicate.call_args.kwargs["timeout"])
        process.wait.assert_called_once_with(timeout=0.5)
        self.assertEqual(110.0, owner.processes.call_args.args[0])
        self.assertEqual(110.0, owner.drain.call_args.args[0])
        self.assertIs(context, owner.drain.call_args.args[1].__self__)
        cleanup.assert_called_once_with()
        self.assertFalse(child.drained)
        self.assertIs(owner, child.owner)

    def test_run_zero_timeout_never_starts_a_fresh_wait_budget(self):
        context = self.context()
        command = ["python", "child.py"]
        child = mock.Mock()
        child.record = context.directory / "child-test.json"
        EXECUTION.write(child.record, {"process_identity": IDENTITY})

        with (
            mock.patch.object(context, "start", return_value=child),
            mock.patch.object(EXECUTION.time, "monotonic", side_effect=[100.0, 100.0]),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            context.run(command, timeout=0)

        child.process.communicate.assert_not_called()
        child._wait.assert_not_called()
        child.terminate_tree.assert_called_once_with()

    def test_run_preserves_timeout_after_verified_forced_drainage(self):
        context = self.context()
        command = ["python", "child.py"]
        child = mock.Mock()
        child.record = context.directory / "child-test.json"
        EXECUTION.write(child.record, {"process_identity": IDENTITY})
        child.drained = True
        child.completion_error = EXECUTION.FORCED_DRAINAGE_ERROR
        child.terminate_tree.side_effect = EXECUTION.ExecutionError(
            EXECUTION.FORCED_DRAINAGE_ERROR
        )

        with (
            mock.patch.object(context, "start", return_value=child),
            mock.patch.object(EXECUTION.time, "monotonic", side_effect=[100.0, 100.0]),
            self.assertRaises(subprocess.TimeoutExpired) as failure,
        ):
            context.run(command, timeout=0)

        self.assertEqual(0, failure.exception.timeout)
        child.terminate_tree.assert_called_once_with()

    def test_run_preserves_cancellation_after_verified_forced_drainage(self):
        context = self.context()
        command = ["python", "child.py"]
        child = mock.Mock()
        child.record = context.directory / "child-test.json"
        EXECUTION.write(child.record, {"process_identity": IDENTITY})
        child.drained = True
        child.completion_error = EXECUTION.FORCED_DRAINAGE_ERROR
        child.terminate_tree.side_effect = EXECUTION.ExecutionError(
            EXECUTION.FORCED_DRAINAGE_ERROR
        )

        with (
            mock.patch.object(context, "start", return_value=child),
            mock.patch.object(
                context, "check_cancel",
                side_effect=EXECUTION.Cancelled("explicit local cancellation"),
            ),
            self.assertRaisesRegex(
                EXECUTION.Cancelled, "explicit local cancellation"
            ),
        ):
            context.run(command)

        child.terminate_tree.assert_called_once_with()

    def test_run_observes_cancellation_during_accounting_drain(self):
        context = self.context()
        command = ["python", "child.py"]
        record = context.directory / "child-test.json"
        EXECUTION.write(record, {"process_identity": IDENTITY, "local_drained": False})
        process = mock.Mock(pid=123, args=command)
        process.communicate.return_value = (None, None)
        process.wait.return_value = 0
        owner = mock.Mock()
        owner.process.return_value = {**IDENTITY, "running": False}
        owner.processes.return_value = [{**IDENTITY, "running": False}]
        owner.drain.side_effect = lambda _deadline, check: check()
        child = self.windows_owned(process, owner, record)
        cancelled = EXECUTION.Cancelled("explicit local cancellation")

        with (
            mock.patch.object(context, "start", return_value=child),
            mock.patch.object(
                context, "check_cancel", side_effect=[None, None, cancelled]
            ) as check_cancel,
            mock.patch.object(child, "terminate_tree") as cleanup,
            mock.patch.object(EXECUTION.time, "monotonic", return_value=100.0),
            self.assertRaisesRegex(EXECUTION.Cancelled, "explicit local cancellation"),
        ):
            context.run(command)

        process.wait.assert_called_once_with(timeout=None)
        self.assertEqual(110.0, owner.drain.call_args.args[0])
        self.assertIs(check_cancel, owner.drain.call_args.args[1])
        cleanup.assert_called_once_with()
        self.assertFalse(child.drained)
        self.assertIs(owner, child.owner)

    def test_terminal_before_handle_seal_is_not_completion(self):
        context = self.context()
        EXECUTION.write(Path(context.record["result"]), {"terminal": True})
        result = EXECUTION.status(self.handle)
        self.assertFalse(result["terminal"])
        self.assertEqual("terminal_unsealed", result["status"])


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



    def test_unconfirmed_child_blocks_root_completion_for_allowed_domain_outcomes(self):
        cases = (
            {"local_status": "failed", "exit_code": 1, "remote_work_may_continue": True},
            {"local_status": "cancelled_local", "exit_code": 130, "remote_work_may_continue": True},
            {"remote_work_may_continue": True},
            {"local_children_drained": False},
        )
        for domain in ("complete", "incomplete", "partial"):
            for index, child_outcome in enumerate(cases):
                with self.subTest(domain=domain, child=child_outcome):
                    directory = self.root / f"{domain}-{index}"
                    with mock.patch.dict(EXECUTION.os.environ, {"COPILOT_HOME": str(directory / "home")}):
                        context = EXECUTION.Execution(directory / "root.json", command=["python"],
                                                      terminal_results=frozenset({domain}))
                        _, result_path, state, record = self.child_evidence(context, **child_outcome)
                        before = state.read_bytes()
                        context.emit({"result": domain})
                        result = context.finish(0)
                        self.assertEqual("failed", result["local_status"])
                        self.assertEqual(1, result["exit_code"])
                        self.assertTrue(result["remote_work_may_continue"])
                        self.assertEqual({"result": domain}, result["workflow_result"])
                        self.assertEqual(before, state.read_bytes())
                        self.assertIn(str(record), result["child_records"])
                        self.assertIn(str(result_path), [item["path"] for item in result["retained_evidence"]])
                        self.assertIn("task-stage", [item["task"]["id"] for item in result["remote_tasks"]])
                        following = EXECUTION.Execution(directory / "following.json", command=["python"])

    def test_missing_malformed_stale_or_unsealed_child_evidence_cannot_complete(self):
        for defect in ("missing_handle", "missing_result", "missing_receipt", "malformed",
                       "stale", "unsealed", "digest", "generation", "command",
                       "missing_remote_status", "malformed_generation"):
            with self.subTest(defect=defect):
                directory = self.root / defect
                with mock.patch.dict(EXECUTION.os.environ, {"COPILOT_HOME": str(directory / "home")}):
                    context = EXECUTION.Execution(directory / "root.json", command=["python"],
                                                  terminal_results=frozenset({"incomplete"}))
                    handle, result_path, state, record = self.child_evidence(context)
                    if defect == "missing_handle":
                        handle.unlink()
                    elif defect == "missing_result":
                        result_path.unlink()
                    elif defect == "missing_receipt":
                        record.unlink()
                    elif defect == "malformed":
                        result_path.write_text("invalid JSON", encoding="utf-8")
                    elif defect in ("stale", "unsealed", "digest"):
                        value = EXECUTION.read(handle)
                        if defect == "unsealed":
                            del value["result_sha256"]
                        else:
                            value["run_id" if defect == "stale" else "result_sha256"] = "changed"
                        EXECUTION.write(handle, value)
                    elif defect in ("generation", "command", "malformed_generation"):
                        value = EXECUTION.read(record)
                        if defect == "generation":
                            value["process_identity"]["creation_time"] = "different generation"
                        elif defect == "malformed_generation":
                            value["process_identity"] = None
                        else:
                            value["command_sha256"] = "different command"
                        EXECUTION.write(record, value)
                    else:
                        value = EXECUTION.read(result_path)
                        del value["remote_work_may_continue"]
                        EXECUTION.write(result_path, value)
                        EXECUTION.write(handle, {**EXECUTION.read(handle),
                                                "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest()})
                    before = state.read_bytes()
                    context.emit({"result": "incomplete"})
                    result = context.finish(0)
                    self.assertEqual(1, result["exit_code"])
                    self.assertTrue(result["remote_work_may_continue"])
                    self.assertTrue(result["finalization_errors"])
                    self.assertEqual(before, state.read_bytes())
                    following = EXECUTION.Execution(directory / "following.json", command=["python"])

    def test_unconfirmed_grandchild_overrides_a_settled_intermediate_result(self):
        with mock.patch.dict(EXECUTION.os.environ, {"COPILOT_HOME": str(self.root / "home")}):
            context = self.context()
            _, parent_result, _, _ = self.child_evidence(context)
            _, failed_result, state, record = self.child_evidence(
                context, directory=parent_result.parent, name="grandchild",
                local_status="failed", exit_code=1, remote_work_may_continue=True,
            )
            before = state.read_bytes()
            context.emit({"result": "complete"})
            result = context.finish(0)
            self.assertEqual(1, result["exit_code"])
            self.assertTrue(result["remote_work_may_continue"])
            self.assertIn(str(record), result["child_records"])
            self.assertIn(str(failed_result), [item["path"] for item in result["retained_evidence"]])
            self.assertIn("task-grandchild", [item["task"]["id"] for item in result["remote_tasks"]])
            self.assertEqual(before, state.read_bytes())
            replacement_owner = {**IDENTITY, "pid": 789, "creation_time": "new controller"}
            with mock.patch.object(EXECUTION, "process_identity", return_value=replacement_owner):
                EXECUTION.Execution(self.root / "following.json", command=["python"])

    def test_dispatch_evidence_is_unique_for_direct_and_nested_children(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                directory = self.root / str(nested)
                with mock.patch.dict(EXECUTION.os.environ, {"COPILOT_HOME": str(directory / "home")}):
                    context = EXECUTION.Execution(directory / "root.json", command=["python"],
                                                  terminal_results=frozenset({"partial"}))
                    children = [self.child_evidence(context, remote_work_may_continue=True)]
                    if nested:
                        children.append(self.child_evidence(
                            context, directory=children[0][1].parent,
                            name="grandchild", remote_work_may_continue=True,
                        ))
                    before = {state: state.read_bytes() for _, _, state, _ in children}
                    context.emit({"result": "partial"})
                    result = context.finish(0)
                    dispatch_paths = sorted(str(path.with_name(path.name + ".dispatch.json"))
                                            for _, path, _, _ in children)
                    self.assertEqual(dispatch_paths, [item["evidence"] for item in result["remote_tasks"]])
                    expected_paths = [str(path) for _, path, _, _ in children] + sorted(
                        dispatch_paths + [str(state) for _, _, state, _ in children]
                    )
                    self.assertEqual(expected_paths, [item["path"] for item in result["retained_evidence"]])
                    for item in result["retained_evidence"]:
                        self.assertEqual(hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest(),
                                         item["sha256"])
                    self.assertEqual(before, {path: path.read_bytes() for path in before})
                    self.assertTrue(result["remote_work_may_continue"])
                    following = EXECUTION.Execution(directory / "following.json", command=["python"])

    def test_dispatch_evidence_keeps_distinct_unknown_creation_paths(self):
        context = self.context()
        children = [self.child_evidence(context, name=name, remote_work_may_continue=True)
                    for name in ("first", "second")]
        paths = []
        for _, result_path, _, _ in children:
            path = result_path.with_name(result_path.name + ".dispatch.json")
            EXECUTION.write(path, {
                "schema": "github.copilot.dispatch-observation.v1",
                "request_id": None, "task": None, "remote_status": "unknown",
                "evidence": "payload-must-not-supply-the-evidence-path",
            })
            paths.append(str(path))
        context.emit({"result": "complete"})
        result = context.finish(0)
        self.assertEqual(sorted(paths), [item["evidence"] for item in result["remote_tasks"]])
        self.assertTrue(all(item["task"] is None for item in result["remote_tasks"]))
        self.assertTrue(result["remote_work_may_continue"])

    def test_dispatch_evidence_conflicts_fail_closed_without_dropping_versions(self):
        for change_bytes in (False, True):
            with self.subTest(change_bytes=change_bytes):
                directory = self.root / str(change_bytes)
                with mock.patch.dict(EXECUTION.os.environ, {"COPILOT_HOME": str(directory / "home")}):
                    context = EXECUTION.Execution(directory / "root.json", command=["python"],
                                                  terminal_results=frozenset({"complete"}))
                    _, result_path, state, _ = self.child_evidence(context)
                    dispatch = result_path.with_name(result_path.name + ".dispatch.json")
                    before = state.read_bytes()
                    original = EXECUTION.read
                    reads = 0

                    def changing(path):
                        nonlocal reads
                        value = original(path)
                        if path == dispatch:
                            reads += 1
                            if reads == 1 and change_bytes:
                                EXECUTION.write(path, {**value, "task": {"id": "changed-task"}})
                            elif reads == 2 and not change_bytes:
                                return {**value, "task": {"id": "changed-task"}}
                        return value

                    context.emit({"result": "complete"})
                    with mock.patch.object(EXECUTION, "read", side_effect=changing):
                        result = context.finish(0)
                    self.assertEqual(1, result["exit_code"])
                    self.assertTrue(result["remote_work_may_continue"])
                    self.assertEqual(["task-stage", "changed-task"],
                                     [item["task"]["id"] for item in result["remote_tasks"]])
                    self.assertIn(f"conflicting dispatch observations: {dispatch}", result["finalization_errors"])
                    versions = [item for item in result["retained_evidence"] if item["path"] == str(dispatch)]
                    self.assertEqual(2 if change_bytes else 1, len(versions))
                    if change_bytes:
                        self.assertNotEqual(versions[0]["sha256"], versions[1]["sha256"])
                        self.assertIn(f"conflicting retained evidence: {dispatch}", result["finalization_errors"])
                    self.assertEqual(before, state.read_bytes())
                    following = EXECUTION.Execution(directory / "following.json", command=["python"])

    def test_settled_children_allow_incomplete_or_partial_completion(self):
        for domain in ("incomplete", "partial"):
            with self.subTest(domain=domain):
                directory = self.root / domain
                with mock.patch.dict(EXECUTION.os.environ, {"COPILOT_HOME": str(directory / "home")}):
                    context = EXECUTION.Execution(directory / "root.json", command=["python"],
                                                  terminal_results=frozenset({domain}))
                    _, parent_result, _, _ = self.child_evidence(context)
                    self.child_evidence(context, directory=parent_result.parent, name="grandchild")
                    context.emit({"result": domain})
                    result = context.finish(0)
                    self.assertEqual(0, result["exit_code"])
                    self.assertFalse(result["remote_work_may_continue"])
                    self.assertEqual({"result": domain}, result["workflow_result"])
                    self.assertEqual("already_finished", EXECUTION.cancel(context.handle)["result"])
                    self.assertFalse(Path(context.record["cancel"]).exists())
                    replacement_owner = {**IDENTITY, "pid": 789, "creation_time": "new controller"}
                    with mock.patch.object(EXECUTION, "process_identity", return_value=replacement_owner):
                        EXECUTION.Execution(directory / "following.json", command=["python"])

    def test_missing_process_identity_fields_never_match(self):
        for invalid in ({}, None, {**IDENTITY, "pid": True},
                        {**IDENTITY, "creation_time": ""}, {**IDENTITY, "image": ""}):
            with self.subTest(identity=invalid):
                self.assertFalse(EXECUTION.same_process(invalid, invalid))

    def test_cancellation_before_sealing_overrides_verified_settled_children(self):
        with mock.patch.dict(EXECUTION.os.environ, {"COPILOT_HOME": str(self.root / "home")}):
            context = self.context()
            self.child_evidence(context)
            context.emit({"result": "complete"})
            load = EXECUTION.load_handle
            admitted = False

            def before_sealing(path):
                nonlocal admitted
                result = load(path)
                if path == context.root and not admitted:
                    admitted = True
                    EXECUTION.cancel(context.root)
                return result

            with mock.patch.object(EXECUTION, "load_handle", side_effect=before_sealing):
                result = context.finish(0)
            self.assertTrue(admitted)
            self.assertEqual(130, result["exit_code"])
            self.assertEqual("cancelled_local", result["local_status"])
            self.assertTrue(result["remote_work_may_continue"])
            following = EXECUTION.Execution(self.root / "following.json", command=["python"])

    def test_terminal_write_failure_does_not_seal_result(self):
        for fail_seal in (False, True):
            with self.subTest(fail_seal=fail_seal), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                with mock.patch.dict(EXECUTION.os.environ, {"COPILOT_HOME": str(root / "home")}):
                    context = EXECUTION.Execution(root / "first.json", command=["python"],
                                                  terminal_results=frozenset({"complete"}))
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

    def test_zero_exit_with_unknown_workflow_outcome_stays_unconfirmed(self):
        with mock.patch.dict(EXECUTION.os.environ, {"COPILOT_HOME": str(self.root / "home")}):
            context = self.context()
            context.emit({"result": "invocation_abandoned", "task_id": "known-task"})
            result = context.finish(0)
            self.assertTrue(result["remote_work_may_continue"])
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

    def test_controller_cancellation_preserves_status_after_forced_job_drainage(self):
        namespace = {}

        def controller():
            context = namespace["_EXECUTION"]
            record = context.directory / "child-cancel.json"
            EXECUTION.write(record, {
                "schema": EXECUTION.SCHEMA,
                "root": str(context.root),
                "run_id": context.run_id,
                "process_identity": IDENTITY,
                "local_drained": False,
                "requires_execution_result": False,
            })
            process = mock.Mock(pid=123, args=["python"])
            process.poll.return_value = None
            process.wait.return_value = 1
            owner = mock.Mock()
            owner.process.return_value = {**IDENTITY, "running": False}
            owner.processes.return_value = [{**IDENTITY, "running": False}]
            context.children.append(self.windows_owned(process, owner, record))
            EXECUTION.cancel(context.handle)
            raise EXECUTION.Cancelled("explicit local cancellation")

        with (
            mock.patch.dict(EXECUTION.os.environ, {}, clear=True),
            mock.patch.object(sys, "argv", ["controller.py", "run"]),
        ):
            self.assertEqual(
                130, EXECUTION.controller_main(controller, namespace, handle=self.handle)
            )

        result = EXECUTION.status(self.handle)
        child = EXECUTION.read(next(context for context in self.handle.with_name(
            self.handle.name + ".d"
        ).glob("child-*.json")))
        self.assertEqual(130, result["exit_code"])
        self.assertEqual("cancelled_local", result["local_status"])
        self.assertTrue(result["local_children_drained"])
        self.assertEqual([EXECUTION.FORCED_DRAINAGE_ERROR], result["finalization_errors"])
        self.assertEqual("unconfirmed", result["remote_status"])
        self.assertTrue(result["remote_work_may_continue"])
        self.assertTrue(child["local_drained"])
        self.assertEqual(EXECUTION.FORCED_DRAINAGE_ERROR, child["completion_error"])

    def test_controller_cancellation_does_not_hide_job_observation_failure(self):
        namespace = {}

        def controller():
            context = namespace["_EXECUTION"]
            record = context.directory / "child-cancel.json"
            EXECUTION.write(record, {
                "schema": EXECUTION.SCHEMA,
                "root": str(context.root),
                "run_id": context.run_id,
                "process_identity": IDENTITY,
                "local_drained": False,
                "requires_execution_result": False,
            })
            process = mock.Mock(pid=123, args=["python"])
            process.poll.return_value = None
            process.wait.return_value = 1
            owner = mock.Mock()
            owner.process.return_value = {**IDENTITY, "running": False}
            owner.processes.side_effect = EXECUTION.ExecutionError(
                "job observation failed"
            )
            context.children.append(self.windows_owned(process, owner, record))
            EXECUTION.cancel(context.handle)
            raise EXECUTION.Cancelled("explicit local cancellation")

        with (
            mock.patch.dict(EXECUTION.os.environ, {}, clear=True),
            mock.patch.object(sys, "argv", ["controller.py", "run"]),
        ):
            self.assertEqual(
                1, EXECUTION.controller_main(controller, namespace, handle=self.handle)
            )

        result = EXECUTION.status(self.handle)
        child = EXECUTION.read(next(context for context in self.handle.with_name(
            self.handle.name + ".d"
        ).glob("child-*.json")))
        self.assertEqual(1, result["exit_code"])
        self.assertEqual("failed", result["local_status"])
        self.assertFalse(result["local_children_drained"])
        self.assertTrue(any("job observation failed" in item
                            for item in result["drainage_errors"]))
        self.assertEqual("unconfirmed", result["remote_status"])
        self.assertTrue(result["remote_work_may_continue"])
        self.assertFalse(child["local_drained"])
        self.assertIn("job observation failed", child["drainage_error"])

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

    def test_windows_binding_failure_uses_created_owner_for_bounded_cleanup(self):
        context = self.context()
        process = mock.Mock(pid=456)
        owner = mock.Mock()
        owner.bind_process.side_effect = OSError("identity unavailable")
        events = []
        owner.terminate.side_effect = lambda: events.append("terminate")
        process.wait.side_effect = lambda **_kwargs: events.append("wait") or 1
        process._handle.Close.side_effect = lambda: events.append("close-process")

        def drain(deadline):
            self.assertEqual(110.0, deadline)
            self.assertTrue(process._handle.Close.called)
            events.append("drain")

        owner.drain.side_effect = drain
        owner.close.side_effect = lambda: events.append("close-owner")
        with (
            mock.patch.object(EXECUTION, "IS_WINDOWS", True),
            mock.patch.object(
                EXECUTION, "guard", side_effect=lambda path: mock.MagicMock()
            ),
            mock.patch.object(EXECUTION.subprocess, "Popen", return_value=process),
            mock.patch.object(EXECUTION, "WindowsOwner", return_value=owner),
            mock.patch.object(EXECUTION, "resume_process") as resume,
            mock.patch.object(EXECUTION.time, "monotonic", side_effect=[100.0, 102.0]),
        ):
            with self.assertRaisesRegex(OSError, "identity unavailable"):
                context.start(["python", "child.py"])

        owner.bind_process.assert_called_once_with(process._handle, 456)
        resume.assert_not_called()
        owner.terminate.assert_called_once()
        owner.drain.assert_called_once()
        owner.close.assert_called_once()
        process.kill.assert_not_called()
        process.wait.assert_called_once_with(timeout=8.0)
        self.assertEqual(
            ["terminate", "wait", "close-process", "drain", "close-owner"],
            events,
        )
        self.assertTrue(context.launch_failures[0]["local_drained"])
        self.assertEqual([], context.launch_failures[0]["cleanup_errors"])

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
    def test_runtime_backend_verifies_the_execution_source_without_a_window(self):
        paths = [SCRIPT.with_name("cloud_task.py")]
        adapters = [path for path in paths if "def _load_execution():" in path.read_text(encoding="utf-8")]
        self.assertEqual(1, len(adapters))
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
    def test_execution_has_no_persistent_writer_reservation_api(self):
        self.assertFalse(hasattr(EXECUTION.Execution, "claim_writers"))
        self.assertFalse(hasattr(EXECUTION.Execution, "release_writers"))
        with tempfile.TemporaryDirectory() as directory:
            context = EXECUTION.Execution(
                (Path(directory) / "execution.json").resolve(),
                command=["python"],
            )
            context.emit({"result": "complete"})
            result = context.finish(0)
        self.assertNotIn("writer_ownership", result)


if __name__ == "__main__":
    unittest.main()
