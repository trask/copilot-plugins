import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "runtime_loader_pins.py"
SPEC_PATH = ROOT / "tools" / "runtime-loader-pins.json"
MODULE_SPEC = importlib.util.spec_from_file_location("runtime_loader_pins", TOOL)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
MODULE = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(MODULE)


class RuntimeLoaderPinsTest(unittest.TestCase):
    def fixture(self, root):
        (root / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "middle.py").write_text(
            'BASE_SHA256 = "' + "0" * 64 + '"\nVALUE = 2\n', encoding="utf-8"
        )
        (root / "last.py").write_text(
            'MIDDLE_SHA256 = "' + "0" * 64 + '"\nVALUE = 3\n', encoding="utf-8"
        )
        dependencies = {}
        for name, source, consumer, constant in (
            ("base", "base.py", "middle.py", "BASE_SHA256"),
            ("middle", "middle.py", "last.py", "MIDDLE_SHA256"),
        ):
            dependencies[name] = {
                "constant": constant,
                "consumers": [{"path": consumer, "constant": constant}],
                "installed_path": source,
                "loader": f"load_{name}",
                "module": name,
                "sha256": "0" * 64,
                "source": source,
            }
        spec = root / "pins.json"
        spec.write_text(
            json.dumps({"schema": 2, "dependencies": dependencies}),
            encoding="utf-8",
        )
        return spec

    def test_update_propagates_transitive_byte_pins_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self.fixture(root)
            with mock.patch.object(MODULE, "ROOT", root):
                MODULE.update_spec(spec)
                MODULE.check_spec(spec)
                data = MODULE.read_spec(spec)["dependencies"]
                middle = (root / "middle.py").read_bytes()
                self.assertEqual(
                    hashlib.sha256((root / "base.py").read_bytes()).hexdigest(),
                    data["base"]["sha256"],
                )
                self.assertIn(data["base"]["sha256"].encode(), middle)
                self.assertEqual(
                    hashlib.sha256(middle).hexdigest(), data["middle"]["sha256"]
                )
                self.assertIn(
                    data["middle"]["sha256"].encode(), (root / "last.py").read_bytes()
                )
                before = {path: path.read_bytes() for path in root.iterdir()}
                MODULE.update_spec(spec)
                self.assertEqual(before, {path: path.read_bytes() for path in root.iterdir()})
                (root / "last.py").write_bytes(
                    (root / "last.py").read_bytes().replace(
                        data["middle"]["sha256"].encode(), b"0" * 64
                    )
                )
                with self.assertRaisesRegex(MODULE.PinError, "last.py:MIDDLE_SHA256"):
                    MODULE.check_spec(spec)

    def test_invalid_declarations_never_write_consumer_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self.fixture(root)
            with mock.patch.object(MODULE, "ROOT", root):
                for mutation in ("cycle", "duplicate", "missing", "path", "ambiguous"):
                    with self.subTest(mutation=mutation):
                        data = json.loads(spec.read_text(encoding="utf-8"))
                        if mutation == "cycle":
                            data["dependencies"]["middle"]["consumers"].append(
                                {"path": "base.py", "constant": "BACK_SHA256"}
                            )
                        elif mutation == "duplicate":
                            data["dependencies"]["base"]["consumers"] *= 2
                        elif mutation == "missing":
                            data["dependencies"]["base"]["consumers"][0]["constant"] = "MISSING"
                        elif mutation == "path":
                            data["dependencies"]["base"]["source"] = "../base.py"
                        else:
                            (root / "middle.py").write_text(
                                'BASE_SHA256 = "' + "0" * 64 + '"\n'
                                'BASE_SHA256 = "' + "0" * 64 + '"\n',
                                encoding="utf-8",
                            )
                        invalid = root / f"{mutation}.json"
                        invalid.write_text(json.dumps(data), encoding="utf-8")
                        before = {path: path.read_bytes() for path in root.iterdir()}
                        with self.assertRaises(MODULE.PinError):
                            MODULE.update_spec(invalid)
                        self.assertEqual(before, {path: path.read_bytes() for path in root.iterdir()})
                        if mutation == "ambiguous":
                            break

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
