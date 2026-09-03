import json
from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]
MARKETPLACE = ROOT / ".github" / "plugin" / "marketplace.json"


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


if __name__ == "__main__":
    unittest.main()
