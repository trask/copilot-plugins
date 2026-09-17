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
    "ce12f19bd6dd547945e319b2db612533090daa1782f4c3def8ff62cd85cf3c6a"
)
RUNTIME_PLUGIN = "agent-tasks-runtime"
RUNTIME_SKILL = ROOT / "plugins" / RUNTIME_PLUGIN / "skills" / RUNTIME_PLUGIN
CONFLICT_HELPER_SHA256 = (
    "c66f40f82d193667165f9ae46d5ed36f62039d3fbe867e126479951a754d807e"
)


class MarketplaceTest(unittest.TestCase):
    def test_entries_match_plugin_manifests(self):
        marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))

        self.assertEqual("trask-plugins", marketplace["name"])
        self.assertEqual(9, len(marketplace["plugins"]))
        self.assertEqual(
            {
                "agent-tasks-runtime",
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
            if entry["name"] == RUNTIME_PLUGIN:
                self.assertEqual(
                    manifest["$schema"],
                    "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                )
                self.assertNotIn("agents", manifest)
                self.assertFalse((plugin_root / "agents").exists())
                skill = plugin_root / "skills" / RUNTIME_PLUGIN / "SKILL.md"
                self.assertTrue(skill.is_file())
                instructions = skill.read_text(encoding="utf-8")
                self.assertTrue(instructions.startswith("---\n"))
                self.assertIn(f"\nname: {RUNTIME_PLUGIN}\n", instructions)
            else:
                self.assertTrue((plugin_root / manifest["agents"]).is_dir())
                self.assertTrue(
                    list((plugin_root / manifest["agents"]).glob("*.agent.md"))
                )

    def test_all_agents_require_explicit_invocation(self):
        marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))

        for entry in marketplace["plugins"]:
            plugin_root = ROOT / entry["source"]
            manifest = json.loads(
                (plugin_root / "plugin.json").read_text(encoding="utf-8")
            )
            agents = manifest.get("agents")
            if not isinstance(agents, str):
                continue
            for agent in (plugin_root / agents).glob("*.agent.md"):
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
            RUNTIME_PLUGIN: (
                Path("skills") / RUNTIME_PLUGIN / "scripts" / "cloud_task.py",
                ORDINARY_HELPER_SHA256,
            ),
            "pr-conflict-resolver": (
                Path("scripts") / "cloud_conflict_task.py",
                CONFLICT_HELPER_SHA256,
            ),
        }
        marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
        entries = {entry["name"]: entry for entry in marketplace["plugins"]}

        for name, (helper_path, expected_sha256) in expected.items():
            with self.subTest(plugin=name):
                plugin_root = (ROOT / entries[name]["source"]).resolve()
                helper = plugin_root / helper_path
                self.assertTrue(helper.is_file())
                self.assertFalse(helper.is_symlink())
                self.assertEqual(
                    hashlib.sha256(helper.read_bytes()).hexdigest(),
                    expected_sha256,
                )

    def test_ordinary_agent_task_helper_has_one_canonical_package_copy(self):
        helper = RUNTIME_SKILL / "scripts" / "cloud_task.py"
        self.assertEqual(
            hashlib.sha256(helper.read_bytes()).hexdigest(), ORDINARY_HELPER_SHA256
        )
        for name in ORDINARY_AGENT_TASK_PLUGINS:
            with self.subTest(plugin=name):
                self.assertFalse(
                    (ROOT / "plugins" / name / "scripts" / "cloud_task.py").exists()
                )

    def test_each_agent_task_plugin_isolated_from_private_configuration(self):
        for plugin_name, coordinator_name in ORDINARY_AGENT_TASK_PLUGINS.items():
            with (
                self.subTest(plugin=plugin_name),
                tempfile.TemporaryDirectory() as directory,
            ):
                isolated_root = Path(directory)
                plugin_root = isolated_root / "plugin"
                shutil.copytree(ROOT / "plugins" / plugin_name, plugin_root)
                coordinator = plugin_root / "scripts" / coordinator_name
                runtime_skill = isolated_root / "runtime-skill"
                shutil.copytree(RUNTIME_SKILL, runtime_skill)
                helper = runtime_skill / "scripts" / "cloud_task.py"
                private_home = isolated_root / "private-home"
                private_helper = (
                    private_home
                    / ".copilot"
                    / "skills"
                    / "cloud"
                    / "scripts"
                    / "cloud_task.py"
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
                    inventory = json.dumps(
                        [
                            {
                                "name": RUNTIME_PLUGIN,
                                "source": "plugin",
                                "path": str(runtime_skill.resolve()),
                                "enabled": True,
                            }
                        ]
                    )
                    listed = subprocess.CompletedProcess(
                        ["copilot", "skill", "list", "--json"], 0, inventory, ""
                    )
                    environment = {
                        "HOME": str(private_home),
                        "USERPROFILE": str(private_home),
                        "COPILOT_HOME": str(private_home / ".copilot"),
                    }
                    with (
                        mock.patch.dict(os.environ, environment),
                        mock.patch.object(module, "run", return_value=listed) as run,
                    ):
                        discovered = module.discover_cloud_task()
                    self.assertEqual(discovered, helper.resolve())
                    self.assertEqual(
                        run.call_args.args[0],
                        ["copilot", "skill", "list", "--json"],
                    )
                    self.assertFalse(run.call_args.kwargs["check"])
                    self.assertEqual(
                        module.REQUIRED_CLOUD_TASK_SHA256,
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
                    with (
                        mock.patch.dict(os.environ, environment),
                        mock.patch.object(module, "run", return_value=listed),
                        self.assertRaisesRegex(
                            module.WorkflowError, "integrity validation"
                        ),
                    ):
                        module.discover_cloud_task()

                    missing = subprocess.CompletedProcess(
                        ["copilot", "skill", "list", "--json"], 0, "[]", ""
                    )
                    with (
                        mock.patch.object(module, "run", return_value=missing),
                        self.assertRaisesRegex(
                            module.WorkflowError,
                            "agent-tasks-runtime@trask-plugins",
                        ),
                    ):
                        module.discover_cloud_task()

                    unavailable = [
                        {
                            "name": RUNTIME_PLUGIN,
                            "source": "plugin",
                            "path": str(runtime_skill.resolve()),
                            "enabled": False,
                        }
                    ]
                    duplicate = json.loads(inventory) * 2
                    for skills in (unavailable, duplicate):
                        with (
                            self.subTest(
                                plugin=plugin_name,
                                inventory="disabled"
                                if skills is unavailable
                                else "duplicate",
                            ),
                            mock.patch.object(
                                module,
                                "run",
                                return_value=subprocess.CompletedProcess(
                                    ["copilot", "skill", "list", "--json"],
                                    0,
                                    json.dumps(skills),
                                    "",
                                ),
                            ),
                            self.assertRaisesRegex(
                                module.WorkflowError, "uniquely installed and enabled"
                            ),
                        ):
                            module.discover_cloud_task()
                finally:
                    sys.modules.pop(module_name, None)

    def test_conflict_agent_keeps_its_dedicated_runtime(self):
        plugin_name = "pr-conflict-resolver"
        with tempfile.TemporaryDirectory() as directory:
            plugin_root = Path(directory) / "plugin"
            shutil.copytree(ROOT / "plugins" / plugin_name, plugin_root)
            coordinator = plugin_root / "scripts" / "pr_conflict_resolver.py"
            helper = plugin_root / "scripts" / "cloud_conflict_task.py"
            module_name = "_isolated_pr_conflict_resolver"
            spec = importlib.util.spec_from_file_location(module_name, coordinator)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
                self.assertEqual(module.discover_conflict_task(), helper.resolve())
                self.assertEqual(
                    module.REQUIRED_CONFLICT_TASK_SHA256,
                    hashlib.sha256(helper.read_bytes()).hexdigest(),
                )
                helper.write_bytes(helper.read_bytes() + b"\n# tampered\n")
                with self.assertRaisesRegex(
                    module.WorkflowError, "integrity validation"
                ):
                    module.discover_conflict_task()
            finally:
                sys.modules.pop(module_name, None)


if __name__ == "__main__":
    unittest.main()
