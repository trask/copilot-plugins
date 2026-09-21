import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "runtime_loader_pins.py"
SPEC_PATH = ROOT / "tools" / "runtime-loader-pins.json"
MODULE_SPEC = importlib.util.spec_from_file_location("runtime_loader_pins", TOOL)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
MODULE = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(MODULE)


class RuntimeLoaderPinsTest(unittest.TestCase):
    def test_committed_pins_match_runtime_sources(self):
        MODULE.check_spec(SPEC_PATH)

    def test_generated_loader_is_deterministic_and_byte_verified(self):
        data = MODULE.read_spec(SPEC_PATH)
        dependency = data["dependencies"]["cloud-task"]
        first = MODULE.render_loader("cloud-task", dependency)
        second = MODULE.render_loader("cloud-task", dependency)
        self.assertEqual(first, second)
        self.assertIn(dependency["sha256"], first)
        self.assertIn("hashlib.sha256(source).hexdigest()", first)
        self.assertIn("exec(compile(source", first)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "runtime.py"
            source.write_text("VALUE = 7\n", encoding="utf-8")
            local_dependency = {
                **dependency,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
            namespace: dict[str, object] = {}
            exec(MODULE.render_loader("test", local_dependency), namespace)
            loaded = namespace[dependency["loader"]](source)
            self.assertEqual(7, loaded.VALUE)
            source.write_text("VALUE = 8\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "digest changed"):
                namespace[dependency["loader"]](source)

    def test_check_rejects_a_stale_pin(self):
        data = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        data["dependencies"]["execution"]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pins.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(MODULE.PinError, "pins are stale"):
                MODULE.check_spec(path)

    def test_cli_generates_a_named_loader(self):
        result = subprocess.run(
            [sys.executable, str(TOOL), "generate", "execution"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("def load_execution_runtime", result.stdout)
        self.assertEqual("", result.stderr)


if __name__ == "__main__":
    unittest.main()
