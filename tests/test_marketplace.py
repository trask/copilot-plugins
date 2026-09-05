import json
from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]
MARKETPLACE = ROOT / ".github" / "plugin" / "marketplace.json"


class MarketplaceTest(unittest.TestCase):
    def test_entries_match_plugin_manifests(self):
        marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))

        self.assertEqual("trask-plugins", marketplace["name"])
        self.assertEqual(9, len(marketplace["plugins"]))
        self.assertEqual(
            {
                "ci-fix-loop",
                "pr-conflict-resolver",
                "copilot-review-loop",
                "historical-pr-audit",
                "orchestration-agents",
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

    def test_orchestration_agents_pin_the_intended_models(self):
        plugin_root = ROOT / "plugins" / "orchestration-agents"
        astra = (plugin_root / "agents" / "astra-coordinator.agent.md").read_text(
            encoding="utf-8"
        )
        luna = (plugin_root / "agents" / "luna-implementer.agent.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("name: Astra Coordinator", astra)
        self.assertIn("name: Luna Implementer", luna)
        self.assertNotIn("tools:", astra.split("---", 2)[1])
        self.assertNotIn("tools:", luna.split("---", 2)[1])
        self.assertIn("model: gpt-6-astra", astra)
        self.assertIn("model: gpt-5.6-luna", luna)
        self.assertIn("reasoning_effort: high", astra)
        self.assertIn("active_session_id", astra)
        self.assertIn("orchestration-agents:astra-coordinator", astra)
        self.assertIn("orchestration-agents:luna-implementer", astra)
        for tool in (
            "create_session",
            "open_pr_session",
            "get_session",
            "send_session_message",
            "list_projects",
            "session_store_sql",
            "respond_to_session_plan",
        ):
            with self.subTest(tool=tool):
                self.assertIn(tool, astra)
        self.assertIn("do not implement code", astra.lower())
        self.assertIn("one startup readiness report", astra)
        self.assertIn("Do not ask the child to repeat the same gate", astra)
        self.assertIn("stack_number", astra)
        self.assertIn("pr_number", astra)
        self.assertIn("at most one bounded", astra)
        self.assertIn("current assignment ID", astra)
        self.assertIn("send_session_message", luna)
        self.assertIn("orchestration-agents:luna-implementer", luna)
        self.assertIn("native PR session tools", luna)
        self.assertIn("authoritative runtime metadata", luna)
        self.assertIn("stop before substantive work", luna)
        self.assertIn("one `send_session_message`", luna)
        self.assertIn("exactly one clear outcome", luna)
        self.assertIn("DONE", luna)
        self.assertIn("BLOCKED", luna)
        self.assertIn("READY", luna)
        self.assertIn("ordinary follow-up turns", luna)
        self.assertIn("Do not spawn additional implementation children", luna)

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


if __name__ == "__main__":
    unittest.main()
