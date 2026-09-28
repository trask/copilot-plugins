import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "tools" / "plugin_package_manifest.py"
SPEC = importlib.util.spec_from_file_location("plugin_package_manifest", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def process_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


class PluginPackageManifestTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.repository = self.directory / "repository"
        self.installed = self.directory / "installed"
        self.repository.mkdir()
        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=self.repository,
            check=True,
            **process_options(),
        )
        subprocess.run(
            ["git", "config", "user.name", "Package Test"],
            cwd=self.repository,
            check=True,
            **process_options(),
        )
        subprocess.run(
            ["git", "config", "user.email", "package@example.invalid"],
            cwd=self.repository,
            check=True,
            **process_options(),
        )
        plugin = self.repository / "plugins" / "demo"
        (plugin / "nested").mkdir(parents=True)
        (plugin / "plugin.json").write_bytes(
            b'{"name":"demo","version":"1.2.3"}\n'
        )
        (plugin / "nested" / "data.bin").write_bytes(b"\x00\x01\xff")
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "add", "plugins/demo"],
            cwd=self.repository,
            check=True,
            **process_options(),
        )
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "fixture"],
            cwd=self.repository,
            check=True,
            **process_options(),
        )
        shutil.copytree(plugin, self.installed / "demo")

    def tearDown(self):
        self.temporary.cleanup()

    def test_record_framing_has_a_fixed_golden_digest(self):
        files = [
            {
                "path": "a.txt",
                "size": 3,
                "sha256": hashlib.sha256(b"abc").hexdigest(),
            },
            {
                "path": "nested/data.bin",
                "size": 3,
                "sha256": hashlib.sha256(b"\x00\x01\xff").hexdigest(),
            },
        ]

        self.assertEqual(
            "9b02609a6861d5527e4ca7b582893209cea2a9d9619d0ac6a43081dee88063a7",
            MODULE.package_metrics(files)[1],
        )

    def test_cli_creates_and_mechanically_verifies_manifest(self):
        manifest = self.directory / "manifest.json"
        create = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "create",
                "--repository-root",
                str(self.repository),
                "--installed-root",
                str(self.installed),
                "--commit",
                "HEAD",
                "--plugin",
                "demo",
                "--output",
                str(manifest),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            **process_options(),
        )
        verify = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "verify",
                "--repository-root",
                str(self.repository),
                "--installed-root",
                str(self.installed),
                "--manifest",
                str(manifest),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            **process_options(),
        )
        created = json.loads(create.stdout)
        verified = json.loads(verify.stdout)

        self.assertEqual("created", created["result"])
        self.assertEqual("verified", verified["result"])
        self.assertEqual(
            created["manifest_sha256"], verified["manifest_sha256"]
        )
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "id": MODULE.SCHEMA_ID,
                "version": MODULE.SCHEMA_VERSION,
            },
            payload["schema"],
        )
        self.assertEqual(MODULE.ALGORITHM, payload["algorithm"])
        self.assertEqual(
            ["nested/data.bin", "plugin.json"],
            [item["path"] for item in payload["packages"][0]["files"]],
        )

    def test_rejects_extra_files_and_byte_drift(self):
        commit = str(
            MODULE.run_git(self.repository, "rev-parse", "HEAD", text=True)
        ).strip()
        (self.installed / "demo" / "extra.pyc").write_bytes(b"cache")
        with self.assertRaisesRegex(
            MODULE.ManifestError, "installed file set mismatch"
        ):
            MODULE.package_evidence(
                self.repository, self.installed, commit, "demo"
            )
        (self.installed / "demo" / "extra.pyc").unlink()
        (self.installed / "demo" / "nested" / "data.bin").write_bytes(
            b"drift"
        )
        with self.assertRaisesRegex(
            MODULE.ManifestError, "installed bytes differ"
        ):
            MODULE.package_evidence(
                self.repository, self.installed, commit, "demo"
            )

    def test_rejects_noncanonical_and_colliding_paths(self):
        for parts in (
            ("..", "file"),
            ("bad\\name",),
            ("bad\nname",),
        ):
            with self.subTest(parts=parts):
                with self.assertRaises(MODULE.ManifestError):
                    MODULE.canonical_relative_path(parts)
        files = [
            {"path": "b", "size": 0, "sha256": "0" * 64},
            {"path": "a", "size": 0, "sha256": "0" * 64},
        ]
        self.assertEqual(
            MODULE.package_metrics(files),
            MODULE.package_metrics(list(reversed(files))),
        )
        files[0]["path"] = "a"
        with self.assertRaisesRegex(MODULE.ManifestError, "not unique"):
            MODULE.package_metrics(files)

    def test_verify_rejects_malformed_records_as_manifest_errors(self):
        commit = str(
            MODULE.run_git(self.repository, "rev-parse", "HEAD", text=True)
        ).strip()
        manifest = MODULE.build_manifest(
            self.repository, self.installed, commit, ["demo"], []
        )
        for field, value in (
            ("size", "3"),
            ("size", -1),
            ("path", None),
            ("path", "\ud800"),
            ("sha256", 42),
        ):
            with self.subTest(field=field, value=value):
                altered = json.loads(json.dumps(manifest))
                altered["packages"][0]["files"][0][field] = value
                with self.assertRaises(MODULE.ManifestError):
                    MODULE.validate_manifest_shape(altered)

        manifest["packages"][0]["files"][0]["size"] = "3"
        path = self.directory / "malformed-manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "verify",
                "--repository-root",
                str(self.repository),
                "--installed-root",
                str(self.installed),
                "--manifest",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            **process_options(),
        )
        self.assertEqual(1, result.returncode)
        self.assertEqual("error", json.loads(result.stdout)["result"])
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
