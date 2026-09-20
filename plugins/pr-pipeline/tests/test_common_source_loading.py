import hashlib
import importlib.machinery
import importlib.util
import marshal
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"
ENTRYPOINTS = ("pr_pipeline.py", "pr_stack_pipeline.py")
COMMON_NAME = "pr_pipeline_common"


class CommonSourceLoadingTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "installed-plugins" / "trask-plugins" / "pr-pipeline" / "scripts"
        self.root.mkdir(parents=True)
        self.path = self.root / "pipeline_common.py"
        self.source = (SCRIPTS / "pipeline_common.py").read_bytes()
        self.path.write_bytes(self.source)
        for name in ENTRYPOINTS:
            (self.root / name).write_bytes((SCRIPTS / name).read_bytes())
        self.cache = Path(importlib.util.cache_from_source(str(self.path)))
        modules = mock.patch.dict(sys.modules)
        modules.start()
        self.addCleanup(modules.stop)
        sys.modules.pop(COMMON_NAME, None)
        bytecode = mock.patch.object(sys, "dont_write_bytecode", False)
        bytecode.start()
        self.addCleanup(bytecode.stop)

    def load(self, filename):
        script = self.root / filename
        spec = importlib.util.spec_from_file_location(f"_fixture_{script.stem}", script)
        module = importlib.util.module_from_spec(spec)
        exec(compile(script.read_bytes(), str(script), "exec", dont_inherit=True), module.__dict__)
        return module

    def test_real_entrypoints_share_pinned_common_without_cache_or_global_changes(self):
        single, stack = (self.load(name) for name in ENTRYPOINTS)
        self.assertIs(single.common, stack.common)
        self.assertIs(single.WorkflowError, stack.WorkflowError)
        self.assertIs(single.common, sys.modules[COMMON_NAME])
        self.assertEqual(COMMON_NAME, single.common.__name__)
        self.assertEqual(str(self.path), single.common.__file__)
        self.assertEqual(str(self.path), single.common.__spec__.origin)
        self.assertEqual(
            hashlib.sha256(self.source).hexdigest(), single.COMMON_SHA256,
        )
        self.assertEqual(single.COMMON_SHA256, stack.COMMON_SHA256)
        self.assertEqual("gpt-5.6-sol", single.common.DEFAULT_STAGE_MODEL)
        self.assertEqual(str(self.path), single.common.run.__code__.co_filename)
        self.assertFalse(sys.dont_write_bytecode)
        self.assertFalse(self.cache.parent.exists())

    def test_both_entrypoints_ignore_wrong_stale_and_malformed_caches(self):
        body = marshal.dumps(compile(
            "raise AssertionError('unverified bytecode executed')", str(self.path), "exec",
        ))
        headers = (
            struct.pack("<III", 0, int(self.path.stat().st_mtime), len(self.source)),
            struct.pack("<I", 1) + importlib.util.source_hash(self.source),
            struct.pack("<I", 3) + importlib.util.source_hash(self.source),
            struct.pack("<III", 0, 0, 0),
        )
        for filename in ENTRYPOINTS:
            for payload in [
                *(importlib.util.MAGIC_NUMBER + header + body for header in headers),
                b"not a pyc",
            ]:
                with self.subTest(filename=filename, header=payload[:16].hex()):
                    sys.modules.pop(COMMON_NAME, None)
                    self.cache.parent.mkdir(exist_ok=True)
                    self.cache.write_bytes(payload)
                    before = self.cache.stat()
                    get_code = importlib.machinery.SourceFileLoader.get_code

                    def reject_common_bytecode(loader, name):
                        if name == COMMON_NAME:
                            self.fail("Pipeline must not call the bytecode-aware loader")
                        return get_code(loader, name)

                    with mock.patch.object(
                        importlib.machinery.SourceFileLoader, "get_code",
                        reject_common_bytecode,
                    ):
                        module = self.load(filename)
                    self.assertIs(module.common, sys.modules[COMMON_NAME])
                    self.assertEqual(payload, self.cache.read_bytes())
                    self.assertEqual(before.st_mtime_ns, self.cache.stat().st_mtime_ns)
                    self.assertEqual([self.cache], list(self.cache.parent.iterdir()))
                    self.assertFalse(sys.dont_write_bytecode)

    def test_hash_mismatch_fails_even_with_common_already_in_memory(self):
        for filename in ENTRYPOINTS:
            with self.subTest(filename=filename):
                self.path.write_bytes(self.source)
                sys.modules.pop(COMMON_NAME, None)
                module = self.load(filename)
                self.path.write_bytes(b"raise AssertionError('unverified source executed')\n")
                with self.assertRaisesRegex(RuntimeError, "integrity changed"):
                    module.load_common()
                self.assertIs(module.common, sys.modules[COMMON_NAME])
                sys.modules.pop(COMMON_NAME)
                with self.assertRaisesRegex(RuntimeError, "integrity changed"):
                    self.load(filename)
                self.assertNotIn(COMMON_NAME, sys.modules)
        self.assertFalse(self.cache.parent.exists())

    def test_each_loader_reads_source_once_and_preserves_shared_identity(self):
        for filename in ENTRYPOINTS:
            with self.subTest(filename=filename):
                module = self.load(filename)
                sys.modules.pop(COMMON_NAME)
                with mock.patch.object(
                    Path, "read_bytes", side_effect=[
                        self.source, b"raise AssertionError('source read twice')\n",
                    ],
                ) as read:
                    common = module.load_common()
                read.assert_called_once_with()
                self.assertIs(common, module.load_common())
        self.assertFalse(self.cache.parent.exists())

    def test_failed_initialization_does_not_poison_module_cache(self):
        for filename in ENTRYPOINTS:
            with self.subTest(filename=filename):
                self.path.write_bytes(self.source)
                module = self.load(filename)
                sys.modules.pop(COMMON_NAME)
                source = b"raise RuntimeError('fixture initialization failed')\n"
                self.path.write_bytes(source)
                with (
                    mock.patch.object(module, "COMMON_SHA256", hashlib.sha256(source).hexdigest()),
                    self.assertRaisesRegex(RuntimeError, "fixture initialization failed"),
                ):
                    module.load_common()
                self.assertNotIn(COMMON_NAME, sys.modules)
        self.assertFalse(self.cache.parent.exists())


if __name__ == "__main__":
    unittest.main()
