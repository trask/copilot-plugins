import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import test_ci_fix_loop as existing


MODULE = existing.MODULE
CONFLICT = existing.CONFLICT_MODULE
RUNTIME = (
    existing.SCRIPT.parents[2] / "agent-tasks-runtime"
    / "skills" / "agent-tasks-runtime" / "scripts" / "execution.py"
)


class PropagationExecutionTest(unittest.TestCase):
    def test_controller_reads_sealed_result_instead_of_stdout(self):
        command = ["python", "resolver.py", "descendant-propagate"]
        payload = {"result": "published", "members_published": [{"number": 7, "head_sha": "new"}]}
        execution = SimpleNamespace(children=[])

        def launch(*args, **kwargs):
            self.assertTrue(kwargs["require_execution"])
            self.assertEqual(0x08000000, kwargs["creationflags"])
            execution.children.append(SimpleNamespace(terminal_result={
                "exit_code": 0, "local_status": "finished", "workflow_result": payload,
            }))
            return subprocess.CompletedProcess(command, 0, "not a workflow result", "")

        execution.run = launch
        with (
            mock.patch.object(MODULE, "_EXECUTION", execution),
            mock.patch.object(MODULE, "IS_WINDOWS", True),
            mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
        ):
            result = MODULE.run(command, require_execution=True)

        self.assertEqual(payload, json.loads(result.stdout))

    def test_controller_rejects_missing_or_failed_seal_despite_successful_stdout(self):
        for terminal in (
            None,
            {"exit_code": 0, "local_status": "finished"},
            {"exit_code": 1, "local_status": "finished", "workflow_result": {"result": "published"}},
            {"exit_code": 0, "local_status": "failed", "workflow_result": {"result": "published"}},
        ):
            with self.subTest(terminal=terminal):
                execution = SimpleNamespace(children=[])

                def launch(command, **kwargs):
                    execution.children.append(SimpleNamespace(terminal_result=terminal))
                    return subprocess.CompletedProcess(command, 0, '{"result":"published"}', "")

                execution.run = launch
                with (
                    mock.patch.object(MODULE, "_EXECUTION", execution),
                    self.assertRaisesRegex(MODULE.WorkflowError, "sealed execution result"),
                ):
                    MODULE.run(["python", "resolver.py"], require_execution=True)

    def test_controller_preserves_failed_workflow_and_exit_code(self):
        execution = SimpleNamespace(children=[])
        payload = {"result": "error", "error": "propagation stopped"}

        def launch(command, **kwargs):
            execution.children.append(SimpleNamespace(terminal_result={
                "exit_code": 1, "local_status": "failed", "workflow_result": payload,
            }))
            return subprocess.CompletedProcess(command, 1, "", "")

        execution.run = launch
        with mock.patch.object(MODULE, "_EXECUTION", execution):
            result = MODULE.run(["python", "resolver.py"], require_execution=True, check=False)
        self.assertEqual(1, result.returncode)
        self.assertEqual(payload, json.loads(result.stdout))

    def test_direct_launch_keeps_stdout_and_hides_windows_console(self):
        for module in (MODULE, CONFLICT):
            with (
                self.subTest(module=module.__name__),
                mock.patch.object(module, "_EXECUTION", None),
                mock.patch.object(module, "IS_WINDOWS", True),
                mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
                mock.patch.object(subprocess, "run") as launch,
            ):
                launch.return_value = subprocess.CompletedProcess(["helper"], 0, "output", "")
                result = module.run(["helper"], require_execution=True)
                self.assertEqual("output", result.stdout)
                self.assertNotIn("require_execution", launch.call_args.kwargs)
                self.assertEqual(0x08000000, launch.call_args.kwargs["creationflags"])

    def test_conflict_helper_requires_owned_execution_without_a_console(self):
        execution = mock.Mock()
        execution.run.return_value = subprocess.CompletedProcess(["helper"], 0, "", "")
        with (
            mock.patch.object(CONFLICT, "_EXECUTION", execution),
            mock.patch.object(CONFLICT, "IS_WINDOWS", True),
            mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
        ):
            CONFLICT.run(["helper"], require_execution=True)
        self.assertTrue(execution.run.call_args.kwargs["require_execution"])
        self.assertEqual(0x08000000, execution.run.call_args.kwargs["creationflags"])

    def test_real_nested_controllers_bind_distinct_commands_and_return_sealed_result(self):
        runtime = MODULE.load_execution_runtime(RUNTIME)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            script = root / "propagation.py"
            script.write_text(
                "import importlib.util\n"
                "import json\n"
                "from pathlib import Path\n"
                "import sys\n"
                f"resolver_path = {str(existing.CONFLICT_SCRIPT)!r}\n"
                f"runtime_path = Path({str(RUNTIME)!r})\n"
                "def load(name, path):\n"
                "    spec = importlib.util.spec_from_file_location(name, path)\n"
                "    module = importlib.util.module_from_spec(spec)\n"
                "    sys.modules[name] = module\n"
                "    spec.loader.exec_module(module)\n"
                "    module._load_execution = lambda: module.load_execution_runtime(runtime_path)\n"
                "    return module\n"
                "if sys.argv[1] == 'descendant-propagate':\n"
                "    module = load('resolver', resolver_path)\n"
                "    def main():\n"
                "        command = [sys.executable, __file__, '--result-file', sys.argv[2]]\n"
                "        module.run(command, require_execution=True)\n"
                "        module.emit({'result': 'published', 'members_published': []})\n"
                "        return 0\n"
                "else:\n"
                "    module = load('cloud', str(Path(resolver_path).with_name('cloud_conflict_task.py')))\n"
                "    def main(**kwargs):\n"
                "        result = {'status': 'completed'}\n"
                "        Path(sys.argv[2]).write_text(json.dumps(result), encoding='utf-8')\n"
                "        return 0\n"
                "module.main = main\n"
                "raise SystemExit(module.execution_main())\n",
                encoding="utf-8",
                newline="\n",
            )
            context = runtime.Execution(
                root / "execution.json",
                command=[sys.executable, "ci-controller"],
                terminal_results=frozenset({"published"}),
            )
            command = [sys.executable, str(script), "descendant-propagate", str(root / "result.json")]
            try:
                with mock.patch.object(MODULE, "_EXECUTION", context):
                    process = MODULE.run(command, require_execution=True, timeout=60)
                self.assertEqual({"result": "published", "members_published": []}, json.loads(process.stdout))
                resolver = context.children[0]
                self.assertTrue(resolver.launch_receipt["requires_execution_result"])
                resolver_handle = Path(resolver.launch_receipt["handle"])
                resolver_record = runtime.read(resolver_handle)
                requests = list(resolver_handle.with_name(resolver_handle.name + ".d").glob("request-*.json"))
                self.assertEqual(1, len(requests))
                helper_request = runtime.read(requests[0])
                self.assertEqual(str(resolver_handle), helper_request["parent"])
                self.assertEqual(str(context.handle), helper_request["root"])
                self.assertNotEqual(resolver_record["parent_request"], str(requests[0]))
                self.assertEqual(runtime.digest(command), resolver_record["command_sha256"])
                self.assertEqual(
                    runtime.digest([sys.executable, str(script), "--result-file", str(root / "result.json")]),
                    helper_request["command_sha256"],
                )
                helper_record = runtime.read(Path(helper_request["child_record"]))
                self.assertTrue(helper_record["requires_execution_result"])
                self.assertTrue(helper_record["local_drained"])
                context.emit({"result": "published"})
            except BaseException as error:
                context.finish(1, str(error))
                raise
            terminal = context.finish(0)
            self.assertEqual("finished", terminal["local_status"])
            self.assertTrue(terminal["local_children_drained"])


if __name__ == "__main__":
    unittest.main()
