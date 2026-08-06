from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / "skills" / "copilot-config-maintenance" / "SKILL.md"
LINKER = ROOT / "link.sh"


class CopilotConfigMaintenanceSkillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = SKILL.read_text(encoding="utf-8")

    def test_frontmatter_has_explicit_change_trigger(self):
        match = re.match(r"\A---\n(.*?)\n---\n", self.skill, re.DOTALL)
        self.assertIsNotNone(match)
        frontmatter = match.group(1)
        self.assertIn("name: copilot-config-maintenance", frontmatter)
        self.assertIn("explicitly asks", frontmatter)
        self.assertIn("Do not use for ordinary use", frontmatter)
        for subject in (
            "skills",
            "custom agents",
            "instructions",
            "settings",
            "plugins",
            "copilot-config repository",
        ):
            self.assertIn(subject, frontmatter)

    def test_skill_records_operational_invariants(self):
        normalized_skill = " ".join(self.skill.split())
        for requirement in (
            r"C:\src\copilot-config",
            "trask/copilot-config",
            "isolated project session",
            "current worktree",
            "git status",
            "`instructions/` and `agents/`",
            "`skills/` is not linked wholesale",
            "`linked_dirs`",
            "`copilot-instructions.md` and `settings.json`",
            "`./link.sh pull` only",
            "Never run `./link.sh` from an isolated worktree",
            "`~/.copilot-backups/`",
            "git diff --check",
            "**draft** pull request into `main`",
            "request a Copilot review",
            "published to GitHub",
            "## After Merge",
            "restart",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, normalized_skill)

    def test_skill_is_individually_linked(self):
        linker = LINKER.read_text(encoding="utf-8")
        linked_dirs = re.search(
            r"linked_dirs=\(\n(?P<body>.*?)\n\)", linker, re.DOTALL
        )
        self.assertIsNotNone(linked_dirs)
        entries = {
            line.strip()
            for line in linked_dirs.group("body").splitlines()
            if line.strip()
        }
        self.assertIn("skills/copilot-config-maintenance", entries)


if __name__ == "__main__":
    unittest.main()
