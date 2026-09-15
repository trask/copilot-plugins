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
from unittest import mock


ROOT = Path(__file__).parents[1]
MARKETPLACE = ROOT / ".github" / "plugin" / "marketplace.json"
ORDINARY_AGENT_TASK_PLUGINS = {
    "pr-description": "pr_description.py",
    "pr-reviewer": "pr_reviewer.py",
    "self-review-loop": "self_review_loop.py",
    "historical-pr-audit": "historical_pr_audit.py",
    "ci-fix-loop": "ci_fix_loop.py",
    "copilot-review-loop": "copilot_review_loop.py",
}
ORDINARY_HELPER_SHA256 = (
    "fa57bff76e2e2854d1bd73ea77a761e9e14ebcd89b89a7d90e91c6d28c73ff5f"
)
CONFLICT_HELPER_SHA256 = (
    "3f9807c392bb31dc3ddcfe74d367b620f417dffc00b1904c78415da43c8b9ad9"
)


class MarketplaceTest(unittest.TestCase):
    def test_entries_match_plugin_manifests(self):
        marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))

        self.assertEqual("trask-plugins", marketplace["name"])
        self.assertEqual(8, len(marketplace["plugins"]))
        self.assertEqual(
            {
                "ci-fix-loop",
                "pr-conflict-resolver",
                "copilot-review-loop",
                "historical-pr-audit",
                "pr-description",
                "pr-pipeline",
                "pr-reviewer",
                "self-review-loop",
            },
            {entry["name"] for entry in marketplace["plugins"]},
        )

        for entry in marketplace["plugins"]:
            plugin_root = ROOT / entry["source"]
            manifest = json.loads(
                (plugin_root / "plugin.json").read_text(encoding="utf-8")
            )

            self.assertEqual(entry["name"], manifest["name"])
            self.assertEqual(entry["version"], manifest["version"])
            self.assertTrue((plugin_root / manifest["agents"]).is_dir())
            self.assertTrue(list((plugin_root / manifest["agents"]).glob("*.agent.md")))

    def test_all_agents_require_explicit_invocation(self):
        marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))

        for entry in marketplace["plugins"]:
            plugin_root = ROOT / entry["source"]
            manifest = json.loads(
                (plugin_root / "plugin.json").read_text(encoding="utf-8")
            )
            for agent in (plugin_root / manifest["agents"]).glob("*.agent.md"):
                with self.subTest(agent=agent.relative_to(ROOT)):
                    instructions = agent.read_text(encoding="utf-8")
                    frontmatter = instructions.split("---", 2)[1]
                    description = next(
                        line for line in frontmatter.splitlines()
                        if line.startswith("description:")
                    )

                    self.assertIn("Explicit invocation only:", description)
                    self.assertIn("never select automatically", description)
                    self.assertIn("disable-model-invocation: true", frontmatter)
                    self.assertIn(
                        "Never select or start this agent automatically.",
                        instructions,
                    )

    def test_agent_task_helpers_are_complete_marketplace_package_files(self):
        expected = {
            **{
                name: ("cloud_task.py", ORDINARY_HELPER_SHA256)
                for name in ORDINARY_AGENT_TASK_PLUGINS
            },
            "pr-conflict-resolver": (
                "cloud_conflict_task.py",
                CONFLICT_HELPER_SHA256,
            ),
        }
        marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
        entries = {entry["name"]: entry for entry in marketplace["plugins"]}

        for name, (helper_name, expected_sha256) in expected.items():
            with self.subTest(plugin=name):
                plugin_root = (ROOT / entries[name]["source"]).resolve()
                helper = plugin_root / "scripts" / helper_name
                self.assertTrue(helper.is_file())
                self.assertFalse(helper.is_symlink())
                self.assertEqual(
                    helper.resolve().parent,
                    (plugin_root / "scripts").resolve(),
                )
                self.assertEqual(
                    hashlib.sha256(helper.read_bytes()).hexdigest(),
                    expected_sha256,
                )

    def test_ordinary_agent_task_helper_copies_are_byte_identical(self):
        helpers = [
            ROOT / "plugins" / name / "scripts" / "cloud_task.py"
            for name in ORDINARY_AGENT_TASK_PLUGINS
        ]
        reference = helpers[0].read_bytes()

        self.assertEqual(hashlib.sha256(reference).hexdigest(), ORDINARY_HELPER_SHA256)
        for helper in helpers[1:]:
            with self.subTest(helper=helper.relative_to(ROOT)):
                self.assertEqual(helper.read_bytes(), reference)

    def test_each_agent_task_plugin_isolated_from_private_configuration(self):
        configurations = [
            *(
                (
                    name,
                    coordinator,
                    "cloud_task.py",
                    "discover_cloud_task",
                    "REQUIRED_CLOUD_TASK_SHA256",
                )
                for name, coordinator in ORDINARY_AGENT_TASK_PLUGINS.items()
            ),
            (
                "pr-conflict-resolver",
                "pr_conflict_resolver.py",
                "cloud_conflict_task.py",
                "discover_conflict_task",
                "REQUIRED_CONFLICT_TASK_SHA256",
            ),
        ]

        for (
            plugin_name,
            coordinator_name,
            helper_name,
            discovery_name,
            digest_name,
        ) in configurations:
            with (
                self.subTest(plugin=plugin_name),
                tempfile.TemporaryDirectory() as directory,
            ):
                isolated_root = Path(directory)
                plugin_root = isolated_root / "plugin"
                shutil.copytree(ROOT / "plugins" / plugin_name, plugin_root)
                coordinator = plugin_root / "scripts" / coordinator_name
                helper = plugin_root / "scripts" / helper_name
                private_home = isolated_root / "private-home"
                private_helper = (
                    private_home
                    / ".copilot"
                    / "skills"
                    / "cloud"
                    / "scripts"
                    / helper_name
                )
                private_helper.parent.mkdir(parents=True)
                private_helper.write_text(
                    "private helper must be ignored\n",
                    encoding="utf-8",
                )
                (
                    private_home / ".copilot" / ".copilot-config-manifest.json"
                ).write_text(
                    '{"source":{"path":"private-checkout"}}\n',
                    encoding="utf-8",
                )

                module_name = f"_isolated_{plugin_name.replace('-', '_')}"
                spec = importlib.util.spec_from_file_location(module_name, coordinator)
                self.assertIsNotNone(spec)
                self.assertIsNotNone(spec.loader)
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                try:
                    spec.loader.exec_module(module)
                    with mock.patch.dict(
                        os.environ,
                        {
                            "HOME": str(private_home),
                            "USERPROFILE": str(private_home),
                            "COPILOT_HOME": str(private_home / ".copilot"),
                        },
                    ):
                        discovered = getattr(module, discovery_name)()
                    self.assertEqual(discovered, helper.resolve())
                    self.assertEqual(
                        getattr(module, digest_name),
                        hashlib.sha256(helper.read_bytes()).hexdigest(),
                    )

                    completed = subprocess.CompletedProcess(
                        [sys.executable, str(discovered)], 0, "", ""
                    )
                    with mock.patch.object(
                        module.subprocess, "run", return_value=completed
                    ) as run:
                        module.run(
                            [module.sys.executable, str(discovered), "--version"],
                            check=False,
                        )
                    self.assertEqual(
                        run.call_args.args[0][:2],
                        [module.sys.executable, str(discovered)],
                    )

                    helper.write_bytes(helper.read_bytes() + b"\n# tampered\n")
                    with self.assertRaisesRegex(
                        module.WorkflowError, "integrity validation"
                    ):
                        getattr(module, discovery_name)()
                finally:
                    sys.modules.pop(module_name, None)


if __name__ == "__main__":
    unittest.main()
