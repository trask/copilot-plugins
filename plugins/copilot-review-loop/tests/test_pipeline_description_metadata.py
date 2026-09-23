import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from test_copilot_review_loop import MODULE


class PipelineDescriptionMetadataTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "run"
        self.args = SimpleNamespace(
            pipeline_run="a" * 32, pipeline_iteration=2, pipeline_max_iterations=2
        )
        digest = hashlib.sha256(self.args.pipeline_run.encode()).hexdigest()[:16]
        name = f"owner--repo--7--invocation-{digest}.json"
        self.review_path = self.root / "copilot-review-loop" / name
        self.description_path = self.root / "pr-description" / name
        self.description_path.parent.mkdir(parents=True)
        self.repo_root = Path(temporary.name) / "checkout"
        self.old = {
            "repo_name": "owner/repo", "number": 7,
            "upstream_owner": "owner", "upstream_repo": "repo",
            "head_repository": "owner/repo", "head_branch": "feature",
            "base_branch": "main", "base_sha": "b" * 40,
            "head_sha": "c" * 40, "is_draft": True,
            "title": "Original title", "body": "Original body",
        }
        self.live = {
            **self.old, "head_sha": "d" * 40,
            "title": "Updated title", "body": "Updated body",
            "pr_url": "https://github.com/owner/repo/pull/7",
        }
        self.previous = {"pr": self.old, "repo_root": str(self.repo_root)}
        self.preflight = {
            "pr": self.live, "repository_root": str(self.repo_root),
            "viewer": {"login": "viewer"},
        }
        self.description = {
            "kind": "run", "pipeline_run": self.args.pipeline_run,
            "pipeline_iteration": 1, "pipeline_max_iterations": 2,
            "repo_root": str(self.repo_root), "run_id": "description-run",
            "validated_head_sha": self.live["head_sha"],
            "agent_task": {"status": "completed", "task": {"state": "completed"}},
            "proposal": {
                "run_id": "description-run", "token": "proposal-token",
                "base": {
                    "head_sha": self.live["head_sha"],
                    "title": self.old["title"], "body": self.old["body"],
                },
                "title": self.live["title"], "body": self.live["body"],
            },
            "validation": {
                "mode": "applied", "run_id": "description-run",
                "proposal_token": "proposal-token",
                "head_sha": self.live["head_sha"],
                "title": self.live["title"], "body": self.live["body"],
                "clearance_snapshot": {
                    "number": 7, "repo_name": "owner/repo",
                    "url": self.live["pr_url"],
                    "head_sha": self.live["head_sha"],
                    "base_sha": self.live["base_sha"],
                    "title": self.live["title"], "body": self.live["body"],
                    "head_repository": "owner/repo", "head_ref": "feature",
                    "base_repository": "owner/repo", "base_ref": "main",
                    "is_draft": True,
                },
            },
            "pr": {
                "number": 7, "repo_name": "owner/repo",
                "url": self.live["pr_url"],
                "head_sha": self.live["head_sha"],
                "head": {
                    "sha": self.live["head_sha"],
                    "repository": "owner/repo", "ref": "feature",
                },
                "base": {
                    "sha": self.live["base_sha"],
                    "repository": "owner/repo", "ref": "main",
                },
                "is_draft": True,
                "title": self.live["title"], "body": self.live["body"],
            },
        }

    def verify(self):
        MODULE.require_pipeline_source_identity(
            self.previous, self.preflight, self.args,
            self.review_path, self.repo_root,
        )

    def write(self):
        self.description_path.write_text(json.dumps(self.description), encoding="utf-8")

    def test_same_run_applied_description_allows_second_sweep(self):
        self.write()
        self.verify()
        self.assertEqual("Original body", self.previous["pr"]["body"])
        self.assertEqual("Updated body", self.preflight["pr"]["body"])

    def test_description_receipt_remains_valid_after_target_branch_advances(self):
        self.write()
        self.preflight["pr"]["base_sha"] = "e" * 40
        self.verify()

    def test_missing_description_does_not_allow_metadata_change(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "source identity changed"):
            self.verify()
        self.preflight["pr"]["title"] = self.old["title"]
        self.preflight["pr"]["body"] = self.old["body"]
        self.verify()

    def test_requires_exact_run_bound_applied_receipt(self):
        original = copy.deepcopy(self.description)
        changes = {
            "other pipeline run": lambda d: d.update(pipeline_run="other"),
            "wrong sweep": lambda d: d.update(pipeline_iteration=2),
            "wrong allowance": lambda d: d.update(pipeline_max_iterations=3),
            "other checkout": lambda d: d.update(repo_root="elsewhere"),
            "unfinished task": lambda d: d["agent_task"].update(status="running"),
            "unfinished hosted task": lambda d: d["agent_task"]["task"].update(state="running"),
            "no mutation": lambda d: d["validation"].update(mode="no_change"),
            "other description run": lambda d: d["validation"].update(run_id="other"),
            "different proposal": lambda d: d["validation"].update(proposal_token="other"),
            "different source body": lambda d: d["proposal"]["base"].update(body="other"),
            "different proposed title": lambda d: d["proposal"].update(title="other"),
            "different validated body": lambda d: d["validation"].update(body="other"),
            "different snapshot body": lambda d: d["validation"]["clearance_snapshot"].update(body="other"),
            "stale snapshot head": lambda d: d["validation"]["clearance_snapshot"].update(head_sha="e" * 40),
            "different base tip": lambda d: d["validation"]["clearance_snapshot"].update(base_sha="e" * 40),
            "different head branch": lambda d: d["validation"]["clearance_snapshot"].update(head_ref="other"),
            "different recorded head": lambda d: d["pr"]["head"].update(ref="other"),
            "malformed recorded head": lambda d: d["pr"].update(head=True),
            "missing snapshot": lambda d: d["validation"].pop("clearance_snapshot"),
        }
        for label, mutate in changes.items():
            with self.subTest(label=label):
                self.description = copy.deepcopy(original)
                mutate(self.description)
                self.write()
                with self.assertRaisesRegex(MODULE.WorkflowError, "source identity changed"):
                    self.verify()

    def test_unrelated_pr_mutations_still_fail(self):
        self.write()
        for field, value in (
            ("body", "Human edit"), ("title", "Human title"),
            ("head_branch", "another-branch"), ("head_repository", "another/repo"),
            ("base_branch", "other-base"), ("is_draft", False),
        ):
            with self.subTest(field=field):
                self.preflight["pr"] = {**self.live, field: value}
                with self.assertRaisesRegex(MODULE.WorkflowError, "source identity changed"):
                    self.verify()

    def test_unbound_or_malformed_description_is_not_accepted(self):
        self.write()
        for path in (
            self.review_path.with_name("other.json"),
            self.root / "other-review-loop" / self.review_path.name,
        ):
            with self.subTest(path=path):
                with self.assertRaisesRegex(MODULE.WorkflowError, "source identity changed"):
                    MODULE.require_pipeline_source_identity(
                        self.previous, self.preflight, self.args, path, self.repo_root,
                    )
        self.description_path.write_text("{invalid", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.WorkflowError, "cannot verify pipeline description state"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
