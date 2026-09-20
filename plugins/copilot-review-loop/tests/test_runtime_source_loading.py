import hashlib
import importlib.machinery
import importlib.util
import marshal
from pathlib import Path
import struct
import sys
import tempfile
from types import ModuleType
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "copilot_review_loop.py"
SPEC = importlib.util.spec_from_file_location("runtime_source_loading_review", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
RUNTIME = (
    Path(__file__).parents[2] / "agent-tasks-runtime" / "skills"
    / "agent-tasks-runtime" / "scripts" / "cloud_task.py"
)
MODULE_NAME = "_copilot_review_candidate_runtime"


class RuntimeSourceLoadingTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.helper = (
            self.root / "installed-plugins" / "trask-plugins"
            / "agent-tasks-runtime" / "skills" / "agent-tasks-runtime"
            / "scripts" / "cloud_task.py"
        )
        self.helper.parent.mkdir(parents=True)
        self.source = RUNTIME.read_bytes()
        self.helper.write_bytes(self.source)
        self.cache = Path(importlib.util.cache_from_source(str(self.helper)))
        self.saved_modules = mock.patch.dict(sys.modules)
        self.saved_modules.start()
        self.addCleanup(self.saved_modules.stop)
        sys.modules.pop(MODULE_NAME, None)
        bytecode = mock.patch.object(sys, "dont_write_bytecode", False)
        bytecode.start()
        self.addCleanup(bytecode.stop)

    def assert_runtime(self, runtime):
        self.assertEqual(MODULE_NAME, runtime.__name__)
        self.assertEqual(MODULE_NAME, runtime.__spec__.name)
        self.assertEqual(str(self.helper), runtime.__file__)
        self.assertEqual(str(self.helper), runtime.__spec__.origin)
        self.assertEqual("", runtime.__package__)
        self.assertIs(runtime, sys.modules[MODULE_NAME])
        self.assertEqual(
            MODULE_NAME,
            runtime.Options(report=False, model="fixture", prompt="fixture").__class__.__module__,
        )
        self.assertEqual(str(self.helper), runtime.task_payload.__code__.co_filename)
        self.assertFalse(sys.dont_write_bytecode)

    def test_real_pinned_source_loads_without_creating_cache(self):
        runtime = MODULE.load_candidate_runtime(self.helper)
        self.assert_runtime(runtime)
        self.assertEqual([self.helper], list(self.helper.parent.rglob("*")))

    def test_wrong_stale_and_malformed_bytecode_is_never_read_or_executed(self):
        body = marshal.dumps(compile(
            "raise AssertionError('unverified bytecode executed')",
            str(self.helper), "exec",
        ))
        timestamp = struct.pack("<III", 0, int(self.helper.stat().st_mtime), len(self.source))
        headers = (
            timestamp,
            struct.pack("<I", 1) + importlib.util.source_hash(self.source),
            struct.pack("<I", 3) + importlib.util.source_hash(self.source),
            struct.pack("<III", 0, 0, 0),
        )
        for payload in [
            *(importlib.util.MAGIC_NUMBER + header + body for header in headers),
            b"not a pyc",
        ]:
            with self.subTest(header=payload[:16].hex()):
                self.cache.parent.mkdir(exist_ok=True)
                self.cache.write_bytes(payload)
                before = self.cache.stat()
                get_code = importlib.machinery.SourceFileLoader.get_code

                def reject_runtime_bytecode(loader, name):
                    if name == MODULE_NAME:
                        self.fail("Runtime must not call the bytecode-aware loader")
                    return get_code(loader, name)

                with mock.patch.object(
                    importlib.machinery.SourceFileLoader, "get_code",
                    reject_runtime_bytecode,
                ):
                    runtime = MODULE.load_candidate_runtime(self.helper)
                self.assert_runtime(runtime)
                self.assertEqual(payload, self.cache.read_bytes())
                self.assertEqual(before.st_mtime_ns, self.cache.stat().st_mtime_ns)
                self.assertEqual([self.cache], list(self.cache.parent.iterdir()))

    def test_executes_the_same_bytes_that_were_hashed(self):
        with mock.patch.object(
            Path, "read_bytes", side_effect=[
                self.source, b"raise AssertionError('source read twice')\n",
            ],
        ) as read:
            runtime = MODULE.load_candidate_runtime(self.helper)
        read.assert_called_once_with()
        self.assert_runtime(runtime)
        self.assertFalse(self.cache.parent.exists())

    def test_changed_source_fails_before_replacing_existing_module(self):
        existing = ModuleType(MODULE_NAME)
        sys.modules[MODULE_NAME] = existing
        self.helper.write_bytes(b"raise AssertionError('unverified source executed')\n")
        with self.assertRaisesRegex(MODULE.WorkflowError, "integrity changed"):
            MODULE.load_candidate_runtime(self.helper)
        self.assertIs(existing, sys.modules[MODULE_NAME])
        self.assertFalse(self.cache.parent.exists())

    def test_missing_source_fails_with_workflow_error(self):
        self.helper.unlink()
        with self.assertRaisesRegex(MODULE.WorkflowError, "could not read"):
            MODULE.load_candidate_runtime(self.helper)
        self.assertNotIn(MODULE_NAME, sys.modules)

    def test_execution_failure_restores_module_registration(self):
        source = b"raise RuntimeError('fixture initialization failed')\n"
        self.helper.write_bytes(source)
        for existing in (None, ModuleType(MODULE_NAME)):
            with self.subTest(existing=existing):
                if existing is None:
                    sys.modules.pop(MODULE_NAME, None)
                else:
                    sys.modules[MODULE_NAME] = existing
                with (
                    mock.patch.object(
                        MODULE, "REQUIRED_CLOUD_TASK_SHA256",
                        hashlib.sha256(source).hexdigest(),
                    ),
                    self.assertRaisesRegex(RuntimeError, "fixture initialization failed"),
                ):
                    MODULE.load_candidate_runtime(self.helper)
                self.assertIs(existing, sys.modules.get(MODULE_NAME))
        self.assertFalse(self.cache.parent.exists())


if __name__ == "__main__":
    unittest.main()
