import copy
from pathlib import Path
import tempfile
import unittest

from test_copilot_review_loop import MODULE


class PipelineSweepTargetTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo_root = Path(temporary.name) / "checkout"
        self.target = {
            "owner": "owner",
            "repo": "repo",
            "number": 7,
            "pr_url": "https://github.com/owner/repo/pull/7",
        }
        self.old = {
            "repo_name": "owner/repo",
            "number": 7,
            "pr_url": self.target["pr_url"],
            "head_repository": "owner/repo",
            "head_branch": "feature",
            "base_branch": "main",
            "base_sha": "b" * 40,
            "head_sha": "c" * 40,
            "is_draft": True,
            "title": "Original title",
            "body": "Original body",
        }
        self.previous = {"pr": self.old, "repo_root": str(self.repo_root)}
        self.preflight = {
            "pr": {
                **self.old,
                "head_sha": "d" * 40,
                "title": "Updated title",
                "body": "Updated body",
            },
            "repository_root": str(self.repo_root),
            "viewer": {"login": "viewer"},
        }

    def verify(self):
        MODULE.require_pipeline_sweep_target(
            self.previous, self.preflight, self.target, self.repo_root
        )

    def test_later_sweep_uses_fresh_snapshot_without_description_receipt(self):
        self.verify()
        for field, value in (
            ("head_repository", "fork/repo"),
            ("head_branch", "replacement"),
            ("base_branch", "other-base"),
            ("base_sha", "e" * 40),
            ("is_draft", False),
            ("title", "Human title"),
            ("body", "Human body"),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.preflight)
                changed["pr"][field] = value
                self.preflight = changed
                self.verify()

    def test_description_change_on_same_head_does_not_need_a_receipt(self):
        self.preflight["pr"]["head_sha"] = self.old["head_sha"]
        self.verify()

    def test_target_and_checkout_cannot_change(self):
        original = copy.deepcopy(self.preflight)
        for field, value in (
            ("repo_name", "other/repo"),
            ("number", 8),
            ("pr_url", "https://github.com/owner/repo/pull/8"),
        ):
            with self.subTest(field=field):
                self.preflight = copy.deepcopy(original)
                self.preflight["pr"][field] = value
                with self.assertRaisesRegex(MODULE.WorkflowError, "target identity changed"):
                    self.verify()
                self.preflight = copy.deepcopy(original)
                saved = self.previous["pr"][field]
                self.previous["pr"][field] = value
                with self.assertRaisesRegex(MODULE.WorkflowError, "target identity changed"):
                    self.verify()
                self.previous["pr"][field] = saved
        self.preflight = copy.deepcopy(original)
        self.preflight["repository_root"] = "another checkout"
        with self.assertRaisesRegex(MODULE.WorkflowError, "target identity changed"):
            self.verify()

    def test_policy_skip_keeps_its_viewer(self):
        self.previous["policy_skip"] = {"viewer_login": "viewer"}
        self.verify()
        self.preflight["viewer"]["login"] = "another-viewer"
        with self.assertRaisesRegex(MODULE.WorkflowError, "target identity changed"):
            self.verify()

    def test_old_source_publication_is_not_reused_for_another_branch(self):
        sha = "d" * 40
        previous = copy.deepcopy(self.old)
        previous["head_sha"] = "c" * 40
        publication = {
            "task_id": "task-1",
            "source_head_sha": previous["head_sha"],
            "published_head_sha": sha,
            "commits": [sha],
            "completed_at": "2026-09-24T00:00:00Z",
            "scope": "source_only",
        }
        state = {
            "source_publication_history": [publication],
            "managed_task_history": [{
                "task_id": "task-1", "status": "completed", "producer": "local",
                "policy": MODULE.LOCAL_DECISION_POLICY,
                "model": MODULE.LOCAL_DECISION_MODEL,
                "reasoning_effort": MODULE.LOCAL_DECISION_REASONING_EFFORT,
                "publication_scope": "source_only",
                "publication_source_head_sha": previous["head_sha"],
                "published_head_sha": sha,
                "ordered_commits": [sha],
                "preflight": {"pr": previous},
            }],
        }
        self.preflight["pr"].update(
            head_sha=sha, head_branch="replacement", base_branch="release"
        )
        self.assertIsNone(
            MODULE.historical_source_fixes(state, self.preflight, self.repo_root)
        )


if __name__ == "__main__":
    unittest.main()
