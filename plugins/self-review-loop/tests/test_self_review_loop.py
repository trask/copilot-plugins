import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "self_review_loop.py"
AGENT = Path(__file__).parents[1] / "agents" / "self-review-loop.agent.md"
SPEC = importlib.util.spec_from_file_location("self_review_loop", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 import os
-value = 1
+value = 2
+extra = 3
 print(value)
"""


def write_state(directory: Path, **overrides) -> Path:
    state = {
        "version": MODULE.STATE_VERSION,
        "created_at": "2026-01-01T00:00:00Z",
        "iterations": 0,
        "next_candidate_id": 1,
        "history": [],
        "repo_root": str(directory),
        "pr": {
            "number": 7,
            "title": "Add a thing",
            "pr_url": "https://github.com/owner/repo/pull/7",
            "repo_name": "owner/repo",
            "upstream_owner": "owner",
            "upstream_repo": "repo",
            "head_owner": "fork",
            "head_repo": "repo",
            "head_branch": "feature",
            "head_sha": "head1",
            "base_branch": "main",
            "base_sha": "base1",
        },
        "review": {
            "id": "pr-7-iteration-1",
            "status": "active",
            "iteration": 1,
            "head_sha": "head1",
            "diff_path": str(directory / "state.json.diff"),
            "anchors": {"app.py": {"LEFT": [2], "RIGHT": [2, 3]}},
            "candidates": [],
            "batches": [],
        },
    }
    state.update(overrides)
    path = directory / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


class AgentInstructionsTest(unittest.TestCase):
    def setUp(self):
        self.instructions = AGENT.read_text(encoding="utf-8")

    def test_renames_the_session_from_preflight_metadata(self):
        self.assertIn(
            "tools: [read, edit, search, execute, agent, todo, rename_session]",
            self.instructions,
        )
        self.assertIn("## Session Naming", self.instructions)
        self.assertIn(
            "Call `rename_session` exactly once per run", self.instructions
        )
        self.assertIn(
            "call `rename_session` with `Self Review Loop: <PR number> - <PR title>` "
            "from its `pr.number` and `pr.title` fields",
            self.instructions,
        )
        self.assertIn("Never use an interim number-only name", self.instructions)

    def test_bare_pr_reference_runs_the_full_loop(self):
        self.assertIn("name: Self Review Loop", self.instructions)
        self.assertIn(
            'description: "Use when selected with only a PR URL, PR number, '
            'or owner/repo#number',
            self.instructions,
        )
        self.assertIn(
            "## Activation: Bare PR References Run The Full Loop", self.instructions
        )
        self.assertIn(
            "is an explicit request to run the full Self Review Loop", self.instructions
        )
        self.assertIn(
            "Never invoke, hand off to, or defer to the generic "
            "`github-pr-diff-review` skill",
            self.instructions,
        )

    def test_never_posts_review_comments(self):
        self.assertIn(
            "This agent never posts inline comments, a review body, or a PR comment. "
            "Its only GitHub mutation is pushing commits to the PR head branch.",
            self.instructions,
        )
        self.assertNotIn("pending review", self.instructions)
        self.assertNotIn("thread", self.instructions.lower())

    def test_keeps_the_claude_only_model_gate(self):
        self.assertIn("## Model Gate", self.instructions)
        self.assertIn("Run only on a Claude model.", self.instructions)
        self.assertIn(
            "using model **GPT-5.6 Sol** with reasoning effort **max**",
            self.instructions,
        )
        self.assertIn(
            "for **each candidate separately**",
            self.instructions,
        )

    def test_commit_body_uses_the_review_finding_label(self):
        self.assertIn("## Commit Content", self.instructions)
        self.assertIn("Address review finding: <short summary>", self.instructions)
        self.assertIn("Review finding:\n", self.instructions)
        self.assertIn("Analysis: <technical analysis and rationale>", self.instructions)
        self.assertIn("Upsides: <concrete benefits>", self.instructions)
        self.assertIn("No material downside identified", self.instructions)
        self.assertNotIn("Copilot comment:", self.instructions)

    def test_documents_the_capped_autonomous_loop(self):
        self.assertIn(
            "The loop is `preflight -> review -> evaluate -> batch -> commit -> publish`",
            self.instructions,
        )
        self.assertIn("The maximum is 5 iterations.", self.instructions)
        self.assertIn("`max_iterations_reached`", self.instructions)
        self.assertIn("`nothing_to_publish`", self.instructions)
        self.assertIn(
            "Never re-raise a finding the carried-forward `history` already records",
            self.instructions,
        )
        self.assertIn(
            "run `preflight --repo-root <workspace>` with no target", self.instructions
        )

    def test_reads_the_pinned_diff_from_the_helper_snapshot(self):
        self.assertIn(
            "Read the pinned diff only from the returned `diff_path`",
            self.instructions,
        )
        self.assertIn("never re-run `gh pr diff`", self.instructions)
        self.assertIn(
            "Review the entire pinned diff read from `diff_path`", self.instructions
        )
        self.assertIn(
            "refuse to publish a skipped batch, require the commits sitting on the "
            "pinned head to be exactly the recorded ones",
            self.instructions,
        )


class TargetParsingTest(unittest.TestCase):
    def test_accepts_urls_and_short_targets(self):
        self.assertEqual(
            MODULE.parse_target("https://github.com/owner/repo/pull/7"),
            {
                "owner": "owner",
                "repo": "repo",
                "number": 7,
                "repo_name": "owner/repo",
                "pr_url": "https://github.com/owner/repo/pull/7",
            },
        )
        self.assertEqual(
            MODULE.parse_target("owner/repo#7")["pr_url"],
            "https://github.com/owner/repo/pull/7",
        )
        self.assertEqual(
            MODULE.parse_target(
                "https://github.com/owner/repo/pull/7#discussion_r1"
            )["number"],
            7,
        )

    def test_rejects_unsupported_targets(self):
        for value in ("owner/repo", "https://github.com/owner/repo/issues/7", "7"):
            with self.assertRaises(MODULE.WorkflowError):
                MODULE.parse_target(value)

    def test_normalizes_git_bash_style_paths_on_windows(self):
        self.assertEqual(
            MODULE.normalize_cli_path("/c/Users/me/state.json", windows=True),
            "C:/Users/me/state.json",
        )
        self.assertEqual(
            MODULE.normalize_cli_path("/c/Users/me/state.json", windows=False),
            "/c/Users/me/state.json",
        )

    def test_recognizes_github_remotes(self):
        self.assertEqual(
            MODULE.github_repo_from_remote("git@github.com:fork/repo.git"), "fork/repo"
        )
        self.assertEqual(
            MODULE.github_repo_from_remote("https://github.com/fork/repo"), "fork/repo"
        )
        self.assertIsNone(MODULE.github_repo_from_remote("https://example.com/fork/repo"))


class DiffAnchorTest(unittest.TestCase):
    def test_parses_changed_lines_per_side(self):
        anchors = MODULE.parse_unified_diff(DIFF)

        self.assertEqual(sorted(anchors), ["app.py"])
        self.assertEqual(anchors["app.py"]["LEFT"], {2})
        self.assertEqual(anchors["app.py"]["RIGHT"], {2, 3})

    def test_serializes_anchors_as_sorted_lists(self):
        self.assertEqual(
            MODULE.serialize_anchors(MODULE.parse_unified_diff(DIFF)),
            {"app.py": {"LEFT": [2], "RIGHT": [2, 3]}},
        )


class CandidateValidationTest(unittest.TestCase):
    def setUp(self):
        self.anchors = {"app.py": {"LEFT": [2], "RIGHT": [2, 3]}}

    def test_accepts_candidates_anchored_to_changed_lines(self):
        self.assertEqual(
            MODULE.validate_candidates(
                [{"path": "app.py", "line": 3, "side": "RIGHT", "body": " Fix it. "}],
                self.anchors,
            ),
            [{"path": "app.py", "line": 3, "side": "RIGHT", "body": "Fix it."}],
        )

    def test_rejects_invalid_candidates(self):
        cases = [
            [],
            [{"path": "app.py", "line": 9, "side": "RIGHT", "body": "Fix it."}],
            [{"path": "app.py", "line": 3, "side": "LEFT", "body": "Fix it."}],
            [{"path": "other.py", "line": 3, "side": "RIGHT", "body": "Fix it."}],
            [{"path": "app.py", "line": 3, "side": "MIDDLE", "body": "Fix it."}],
            [{"path": "app.py", "line": 3, "side": "RIGHT", "body": "  "}],
            [{"path": "app.py", "line": 0, "side": "RIGHT", "body": "Fix it."}],
            [{"path": "app.py", "line": True, "side": "RIGHT", "body": "Fix it."}],
            [
                {
                    "path": "app.py",
                    "line": 3,
                    "side": "RIGHT",
                    "body": "Fix it.",
                    "extra": 1,
                }
            ],
            [{"path": "app.py", "line": 3, "side": "RIGHT"}],
            ["not an object"],
        ]
        for candidates in cases:
            with self.subTest(candidates=candidates):
                with self.assertRaises(MODULE.WorkflowError):
                    MODULE.validate_candidates(candidates, self.anchors)


class HistoryTest(unittest.TestCase):
    def test_maps_candidate_status_to_history_outcome(self):
        self.assertEqual(
            MODULE.history_outcome({"status": "handled", "commit": "abc"}), "addressed"
        )
        self.assertEqual(
            MODULE.history_outcome({"status": "handled", "commit": None}), "no_code"
        )
        self.assertEqual(MODULE.history_outcome({"status": "dropped"}), "dropped")
        self.assertEqual(MODULE.history_outcome({"status": "skipped"}), "skipped")
        self.assertEqual(MODULE.history_outcome({"status": "pending"}), "unresolved")

    def test_archives_only_resolved_candidates(self):
        state = {
            "history": [],
            "review": {
                "iteration": 1,
                "candidates": [
                    {
                        "id": 1,
                        "path": "app.py",
                        "line": 3,
                        "side": "RIGHT",
                        "body": "Fix it.",
                        "status": "handled",
                        "commit": "abc",
                        "summary": "fix",
                    },
                    {
                        "id": 2,
                        "path": "app.py",
                        "line": 2,
                        "side": "RIGHT",
                        "body": "Speculative.",
                        "status": "dropped",
                        "rationale": "not demonstrated",
                    },
                    {
                        "id": 3,
                        "path": "app.py",
                        "line": 2,
                        "side": "LEFT",
                        "body": "Never reached.",
                        "status": "pending",
                    },
                    {
                        "id": 4,
                        "path": "app.py",
                        "line": 3,
                        "side": "RIGHT",
                        "body": "Validation failed.",
                        "status": "skipped",
                        "rationale": "tests fail",
                    },
                ],
            },
        }

        MODULE.archive_review(state)
        MODULE.archive_review(state)

        self.assertEqual([entry["id"] for entry in state["history"]], [1, 2])
        self.assertEqual(state["history"][0]["outcome"], "addressed")
        self.assertEqual(state["history"][0]["commit"], "abc")
        self.assertEqual(state["history"][1]["outcome"], "dropped")
        self.assertEqual(state["history"][1]["detail"], "not demonstrated")


class HeadVerificationTest(unittest.TestCase):
    def test_requires_the_local_head_to_equal_the_pr_head(self):
        MODULE.require_checkout_head("abc", "abc")

        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.require_checkout_head("abc", "def")

        self.assertIn("HEAD mismatch", str(error.exception))

    def test_refuses_to_push_a_missing_upstream_head_branch(self):
        pr = {
            "upstream_owner": "owner",
            "upstream_repo": "repo",
            "head_owner": "owner",
            "head_repo": "repo",
            "head_branch": "feature",
        }

        with mock.patch.object(MODULE, "remote_head", return_value=None):
            with self.assertRaises(MODULE.WorkflowError):
                MODULE.require_fork_head(pr)

        with mock.patch.object(MODULE, "remote_head", return_value="abc"):
            MODULE.require_fork_head(pr)

    def test_allows_a_fork_head_without_checking_the_remote(self):
        pr = {
            "upstream_owner": "owner",
            "upstream_repo": "repo",
            "head_owner": "fork",
            "head_repo": "repo",
            "head_branch": "feature",
        }

        with mock.patch.object(MODULE, "remote_head") as remote_head:
            MODULE.require_fork_head(pr)

        remote_head.assert_not_called()


class StateCommandTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.emitted = []
        patcher = mock.patch.object(MODULE, "emit", self.emitted.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def register(self, path, candidates):
        source = self.directory / "candidates.json"
        source.write_text(json.dumps(candidates), encoding="utf-8")
        MODULE.command_candidates(
            SimpleNamespace(state=str(path), input=str(source))
        )
        return self.emitted[-1]

    def test_registers_candidates_with_stable_identifiers(self):
        path = write_state(self.directory)

        result = self.register(
            path,
            [
                {"path": "app.py", "line": 3, "side": "RIGHT", "body": "Fix it."},
                {"path": "app.py", "line": 2, "side": "LEFT", "body": "Removed guard."},
            ],
        )

        self.assertEqual(result["result"], "registered")
        self.assertEqual([item["id"] for item in result["candidates"]], [1, 2])
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["next_candidate_id"], 3)
        self.assertEqual(
            [item["status"] for item in state["review"]["candidates"]],
            ["pending", "pending"],
        )

    def test_refuses_to_register_candidates_twice(self):
        path = write_state(self.directory)
        self.register(
            path, [{"path": "app.py", "line": 3, "side": "RIGHT", "body": "Fix it."}]
        )

        with self.assertRaises(MODULE.WorkflowError):
            self.register(
                path,
                [{"path": "app.py", "line": 2, "side": "RIGHT", "body": "Another."}],
            )

    def test_refuses_to_use_a_published_iteration(self):
        path = write_state(self.directory)
        state = json.loads(path.read_text(encoding="utf-8"))
        state["review"]["status"] = "published"
        path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.active_review(state)

        self.assertIn("already published", str(error.exception))

    def test_dropped_candidates_cannot_be_planned(self):
        path = write_state(self.directory)
        self.register(
            path, [{"path": "app.py", "line": 3, "side": "RIGHT", "body": "Fix it."}]
        )
        MODULE.command_drop(
            SimpleNamespace(state=str(path), candidates=[1], rationale="not demonstrated")
        )

        with self.assertRaises(MODULE.WorkflowError):
            MODULE.command_plan(
                SimpleNamespace(
                    state=str(path),
                    batch="batch-1",
                    candidates=[1],
                    label="fix",
                    paths=["app.py"],
                    validation=None,
                )
            )

    def test_rejects_unregistered_candidate_identifiers(self):
        path = write_state(self.directory)
        self.register(
            path, [{"path": "app.py", "line": 3, "side": "RIGHT", "body": "Fix it."}]
        )

        with self.assertRaises(MODULE.WorkflowError):
            MODULE.command_drop(
                SimpleNamespace(state=str(path), candidates=[9], rationale="nope")
            )

    def test_record_requires_a_commit_or_a_rationale(self):
        path = write_state(self.directory)
        self.register(
            path, [{"path": "app.py", "line": 3, "side": "RIGHT", "body": "Fix it."}]
        )

        with self.assertRaises(MODULE.WorkflowError):
            MODULE.command_record(
                SimpleNamespace(
                    state=str(path),
                    batch="batch-1",
                    candidates=[1],
                    summary="fix",
                    commit=None,
                    rationale=None,
                )
            )

    def test_record_resolves_the_commit_and_approves_the_batch(self):
        path = write_state(self.directory)
        self.register(
            path, [{"path": "app.py", "line": 3, "side": "RIGHT", "body": "Fix it."}]
        )
        MODULE.command_plan(
            SimpleNamespace(
                state=str(path),
                batch="batch-1",
                candidates=[1],
                label="fix",
                paths=["app.py"],
                validation="pytest",
            )
        )

        with mock.patch.object(MODULE, "git", return_value="fullsha"):
            MODULE.command_record(
                SimpleNamespace(
                    state=str(path),
                    batch="batch-1",
                    candidates=[1],
                    summary="fix the guard",
                    commit="HEAD",
                    rationale=None,
                )
            )

        state = json.loads(path.read_text(encoding="utf-8"))
        candidate = state["review"]["candidates"][0]
        self.assertEqual(candidate["status"], "handled")
        self.assertEqual(candidate["commit"], "fullsha")
        self.assertEqual(candidate["summary"], "fix the guard")
        self.assertEqual(state["review"]["batches"][0]["status"], "approved")


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.emitted = []
        patcher = mock.patch.object(MODULE, "emit", self.emitted.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def candidate(self, **overrides):
        candidate = {
            "id": 1,
            "path": "app.py",
            "line": 3,
            "side": "RIGHT",
            "body": "Fix it.",
            "status": "handled",
            "batch": "batch-1",
            "summary": "fix the guard",
            "commit": "newhead",
            "rationale": None,
        }
        candidate.update(overrides)
        return candidate

    def state_with(self, candidates):
        path = write_state(self.directory)
        state = json.loads(path.read_text(encoding="utf-8"))
        state["review"]["candidates"] = candidates
        path.write_text(json.dumps(state), encoding="utf-8")
        return path

    def test_refuses_to_publish_while_candidates_are_pending(self):
        path = self.state_with([self.candidate(status="pending")])

        with mock.patch.object(MODULE, "git", return_value=""):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.command_publish(SimpleNamespace(state=str(path)))

        self.assertIn("neither dropped nor handled", str(error.exception))

    def test_refuses_to_publish_a_dirty_worktree(self):
        path = self.state_with([self.candidate()])

        with mock.patch.object(MODULE, "git", return_value=" M app.py"):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.command_publish(SimpleNamespace(state=str(path)))

        self.assertIn("worktree is not clean", str(error.exception))

    def test_reports_nothing_to_publish_without_a_commit(self):
        path = self.state_with(
            [self.candidate(commit=None, rationale="already correct")]
        )

        with mock.patch.object(MODULE, "git", side_effect=["", "head1", ""]):
            MODULE.command_publish(SimpleNamespace(state=str(path)))

        self.assertEqual(self.emitted[-1]["result"], "nothing_to_publish")
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["iterations"], 0)
        self.assertEqual(state["review"]["status"], "active")

    def test_refuses_to_publish_a_skipped_batch(self):
        path = self.state_with([self.candidate(status="skipped")])

        with mock.patch.object(MODULE, "git", return_value=""):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.command_publish(SimpleNamespace(state=str(path)))

        self.assertIn("skipped", str(error.exception))

    def test_refuses_to_publish_an_unrecorded_commit(self):
        path = self.state_with([self.candidate()])
        git_results = {
            ("status", "--porcelain=v1"): "",
            ("rev-parse", "HEAD"): "stray",
            ("rev-list", "head1..HEAD"): "stray\nnewhead",
        }

        with mock.patch.object(
            MODULE, "git", side_effect=lambda root, *args: git_results[args]
        ):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.command_publish(SimpleNamespace(state=str(path)))

        self.assertIn("unrecorded ['stray']", str(error.exception))

    def test_refuses_to_report_nothing_to_publish_over_a_stray_commit(self):
        path = self.state_with(
            [self.candidate(commit=None, rationale="already correct")]
        )

        with mock.patch.object(MODULE, "git", side_effect=["", "stray", "stray"]):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.command_publish(SimpleNamespace(state=str(path)))

        self.assertIn("unrecorded ['stray']", str(error.exception))

    def test_refuses_to_publish_a_missing_recorded_commit(self):
        path = self.state_with([self.candidate()])
        git_results = {
            ("status", "--porcelain=v1"): "",
            ("rev-parse", "HEAD"): "head1",
            ("rev-list", "head1..HEAD"): "",
        }

        with mock.patch.object(
            MODULE, "git", side_effect=lambda root, *args: git_results[args]
        ):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.command_publish(SimpleNamespace(state=str(path)))

        self.assertIn("missing ['newhead']", str(error.exception))

    def test_rejects_handled_candidates_without_publish_data(self):
        path = self.state_with([self.candidate(summary=None)])

        with mock.patch.object(MODULE, "git", return_value=""):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.command_publish(SimpleNamespace(state=str(path)))

        self.assertIn("lack publish data", str(error.exception))

    def test_publishes_pushes_verifies_and_archives(self):
        path = self.state_with([self.candidate()])
        git_results = {
            ("status", "--porcelain=v1"): "",
            ("rev-parse", "HEAD"): "newhead",
            ("rev-list", "head1..HEAD"): "newhead",
        }

        with (
            mock.patch.object(
                MODULE, "git", side_effect=lambda root, *args: git_results[args]
            ),
            mock.patch.object(MODULE, "find_push_remote", return_value="origin"),
            mock.patch.object(
                MODULE, "remote_head", side_effect=["oldhead", "newhead"]
            ),
            mock.patch.object(
                MODULE, "metadata_for", return_value={"head_sha": "newhead"}
            ),
            mock.patch.object(MODULE, "run") as run,
        ):
            MODULE.command_publish(SimpleNamespace(state=str(path)))

        self.assertEqual(
            run.call_args.args[0][-3:], ["push", "origin", "HEAD:feature"]
        )
        result = self.emitted[-1]
        self.assertEqual(result["result"], "published")
        self.assertEqual(result["commits"], ["newhead"])
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["iterations"], 1)
        self.assertEqual(state["review"]["status"], "published")
        self.assertEqual(state["history"][0]["outcome"], "addressed")

    def test_skips_the_push_when_the_remote_already_matches(self):
        path = self.state_with([self.candidate()])
        git_results = {
            ("status", "--porcelain=v1"): "",
            ("rev-parse", "HEAD"): "newhead",
            ("rev-list", "head1..HEAD"): "newhead",
        }

        with (
            mock.patch.object(
                MODULE, "git", side_effect=lambda root, *args: git_results[args]
            ),
            mock.patch.object(MODULE, "find_push_remote", return_value="origin"),
            mock.patch.object(MODULE, "remote_head", return_value="newhead"),
            mock.patch.object(
                MODULE, "metadata_for", return_value={"head_sha": "newhead"}
            ),
            mock.patch.object(MODULE, "run") as run,
        ):
            MODULE.command_publish(SimpleNamespace(state=str(path)))

        run.assert_not_called()
        self.assertEqual(self.emitted[-1]["result"], "published")

    def test_fails_when_the_pr_head_does_not_match_the_push(self):
        path = self.state_with([self.candidate()])
        git_results = {
            ("status", "--porcelain=v1"): "",
            ("rev-parse", "HEAD"): "newhead",
            ("rev-list", "head1..HEAD"): "newhead",
        }

        with (
            mock.patch.object(
                MODULE, "git", side_effect=lambda root, *args: git_results[args]
            ),
            mock.patch.object(MODULE, "find_push_remote", return_value="origin"),
            mock.patch.object(MODULE, "remote_head", return_value="newhead"),
            mock.patch.object(
                MODULE, "metadata_for", return_value={"head_sha": "otherhead"}
            ),
            mock.patch.object(MODULE, "run"),
        ):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.command_publish(SimpleNamespace(state=str(path)))

        self.assertIn("PR head mismatch", str(error.exception))


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.emitted = []
        patcher = mock.patch.object(MODULE, "emit", self.emitted.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.metadata = {
            "number": 7,
            "title": "Add a thing",
            "pr_url": "https://github.com/owner/repo/pull/7",
            "repo_name": "owner/repo",
            "upstream_owner": "owner",
            "upstream_repo": "repo",
            "head_owner": "fork",
            "head_repo": "repo",
            "head_branch": "feature",
            "head_sha": "head1",
            "base_branch": "main",
            "base_sha": "base1",
        }
        self.git_results = {
            ("status", "--porcelain=v1"): "",
            ("branch", "--show-current"): "feature",
            ("rev-parse", "HEAD"): "head1",
        }

    def preflight(self, state_path, *, metadata_sequence=None, max_iterations=5):
        arguments = SimpleNamespace(
            target="owner/repo#7",
            repo_root=str(self.directory),
            state=str(state_path),
            max_iterations=max_iterations,
        )
        metadata_sequence = metadata_sequence or [self.metadata, self.metadata]
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(
                MODULE, "resolve_repo_root", return_value=self.directory
            ),
            mock.patch.object(
                MODULE, "git", side_effect=lambda root, *args: self.git_results[args]
            ),
            mock.patch.object(MODULE, "metadata_for", side_effect=metadata_sequence),
            mock.patch.object(MODULE, "fetch_authoritative_diff", return_value=DIFF),
            mock.patch.object(MODULE, "run"),
        ):
            MODULE.command_preflight(arguments)
        return self.emitted[-1]

    def test_pins_the_diff_snapshot_and_starts_the_first_iteration(self):
        state_path = self.directory / "state.json"

        result = self.preflight(state_path)

        self.assertEqual(result["result"], "ready")
        self.assertEqual(result["head_sha"], "head1")
        self.assertEqual(result["changed_files"], ["app.py"])
        self.assertEqual(result["iteration"], 1)
        self.assertEqual(result["history"], [])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state["review"]["anchors"], {"app.py": {"LEFT": [2], "RIGHT": [2, 3]}}
        )
        self.assertEqual(state["review"]["candidates"], [])
        diff_path = Path(result["diff_path"])
        self.assertEqual(diff_path, MODULE.diff_path_for(state_path))
        self.assertEqual(state["review"]["diff_path"], str(diff_path))
        self.assertEqual(diff_path.read_text(encoding="utf-8"), DIFF)

    def test_rejects_a_dirty_worktree(self):
        self.git_results[("status", "--porcelain=v1")] = " M app.py"

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.preflight(self.directory / "state.json")

        self.assertIn("worktree is not clean", str(error.exception))

    def test_rejects_a_local_head_ahead_of_the_pr_head(self):
        self.git_results[("rev-parse", "HEAD")] = "localhead"

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.preflight(self.directory / "state.json")

        self.assertIn("HEAD mismatch", str(error.exception))

    def test_rejects_a_head_that_moves_while_the_diff_is_fetched(self):
        moved = dict(self.metadata, head_sha="head2")

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.preflight(
                self.directory / "state.json",
                metadata_sequence=[self.metadata, moved],
            )

        self.assertIn("PR head changed", str(error.exception))

    def test_carries_history_forward_and_starts_the_next_iteration(self):
        state_path = write_state(
            self.directory,
            iterations=1,
            next_candidate_id=3,
            review={
                "id": "pr-7-iteration-1",
                "status": "published",
                "iteration": 1,
                "head_sha": "head0",
                "anchors": {},
                "candidates": [
                    {
                        "id": 1,
                        "path": "app.py",
                        "line": 3,
                        "side": "RIGHT",
                        "body": "Fix it.",
                        "status": "handled",
                        "commit": "abc",
                        "summary": "fix",
                    },
                    {
                        "id": 2,
                        "path": "app.py",
                        "line": 2,
                        "side": "RIGHT",
                        "body": "Speculative.",
                        "status": "dropped",
                        "rationale": "not demonstrated",
                    },
                ],
                "batches": [],
            },
        )

        result = self.preflight(state_path)

        self.assertEqual(result["result"], "ready")
        self.assertEqual(result["iteration"], 2)
        self.assertEqual(
            [(entry["id"], entry["outcome"]) for entry in result["history"]],
            [(1, "addressed"), (2, "dropped")],
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["next_candidate_id"], 3)
        self.assertEqual(state["review"]["id"], "pr-7-iteration-2")
        self.assertEqual(state["review"]["status"], "active")

    def test_stops_at_the_iteration_cap(self):
        state_path = write_state(self.directory, iterations=5)

        result = self.preflight(state_path)

        self.assertEqual(result["result"], "max_iterations_reached")
        self.assertEqual(result["iteration"], 6)
        self.assertEqual(result["max_iterations"], 5)


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.emitted = []
        patcher = mock.patch.object(MODULE, "emit", self.emitted.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reports_the_state_attached_to_an_explicit_path(self):
        path = write_state(self.directory, iterations=2)

        MODULE.command_status(
            SimpleNamespace(state=str(path), current=False, repo_root=None)
        )

        result = self.emitted[-1]
        self.assertEqual(result["result"], "ready")
        self.assertEqual(result["pr"]["number"], 7)
        self.assertEqual(result["iterations"], 2)

    def test_reports_no_state_for_the_current_branch_pr(self):
        target = MODULE.parse_target("owner/repo#7")

        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(
                MODULE, "resolve_repo_root", return_value=self.directory
            ),
            mock.patch.object(MODULE, "current_pr_target", return_value=target),
            mock.patch.object(
                MODULE,
                "default_state_path",
                return_value=self.directory / "missing.json",
            ),
        ):
            MODULE.command_status(
                SimpleNamespace(state=None, current=True, repo_root=None)
            )

        result = self.emitted[-1]
        self.assertEqual(result["result"], "no_state")
        self.assertEqual(result["pr"]["number"], 7)
        self.assertIsNone(result["review"])

    def test_cleanup_removes_the_state_file(self):
        path = write_state(self.directory)
        diff_path = MODULE.diff_path_for(path)
        diff_path.write_text(DIFF, encoding="utf-8")

        MODULE.command_cleanup(SimpleNamespace(state=str(path)))

        self.assertFalse(path.exists())
        self.assertFalse(diff_path.exists())
        self.assertEqual(self.emitted[-1]["result"], "cleaned_up")

    def test_cleanup_tolerates_a_missing_diff_snapshot(self):
        path = write_state(self.directory)

        MODULE.command_cleanup(SimpleNamespace(state=str(path)))

        self.assertFalse(path.exists())
        self.assertEqual(self.emitted[-1]["result"], "cleaned_up")


if __name__ == "__main__":
    unittest.main()
