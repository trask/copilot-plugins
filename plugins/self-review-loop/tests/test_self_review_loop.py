import contextlib
import importlib.util
import io
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

    def test_names_the_session_from_preflight_metadata_idempotently(self):
        self.assertIn(
            "tools: [read, edit, search, execute, agent, todo, rename_session]",
            self.instructions,
        )
        self.assertIn("## Session Naming", self.instructions)
        self.assertIn(
            "ensure the session name is `Self Review Loop: <PR number> - <PR title>`",
            self.instructions,
        )
        self.assertIn(
            "If the harness has already supplied a name beginning "
            "`Self Review Loop: <PR number> - `",
            self.instructions,
        )
        self.assertIn("do not call `rename_session`", self.instructions)
        self.assertIn("Otherwise call `rename_session` once", self.instructions)
        self.assertIn(
            "accept that result and continue without retrying or reporting it as "
            "retrospective friction",
            self.instructions,
        )
        self.assertIn("Never use an interim number-only name", self.instructions)
        self.assertNotIn("call `rename_session` again", self.instructions)
        self.assertNotIn("immediately call `rename_session`", self.instructions)

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

    def test_never_posts_review_comments_and_allows_required_metadata_corrections(self):
        self.assertIn(
            "This agent never posts inline comments, a review body, or a PR comment. "
            "Its normal GitHub mutation is pushing commits to the PR head branch; "
            "the only exception is the narrowly required PR title or description "
            "correction under **PR Metadata Accuracy**.",
            self.instructions,
        )
        self.assertNotIn("pending review", self.instructions)
        self.assertNotIn("thread", self.instructions.lower())
        self.assertIn("## PR Metadata Accuracy", self.instructions)
        self.assertIn(
            "takes precedence over the normal push-only mutation limit",
            self.instructions,
        )
        self.assertIn(
            "After each successful `publish`, before the next `preflight`, re-read "
            "the live title and description against the newly published diff",
            self.instructions,
        )
        self.assertIn(
            "If a commit from this loop made either materially false or misleading",
            self.instructions,
        )
        self.assertIn(
            "Recheck once more before the terminal response", self.instructions
        )
        self.assertIn(
            "If a required metadata correction cannot be completed safely, stop",
            self.instructions,
        )

    def test_records_both_clean_exits(self):
        self.assertEqual(
            self.instructions.count("run `resolve --state <path> --outcome clean`"),
            2,
        )

    def test_keeps_the_claude_only_model_gate(self):
        self.assertIn("## Model Gate", self.instructions)
        self.assertIn("Run only on a Claude model.", self.instructions)
        self.assertIn(
            "using agent type **general-purpose**, model **GPT-5.6 Sol**, and "
            "reasoning effort **max**",
            self.instructions,
        )
        self.assertIn(
            "The agent type is required even when setting the model override",
            self.instructions,
        )
        self.assertIn(
            "do not substitute an explore, task, review, or other specialized agent",
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

    def test_documents_file_based_commit_message_authoring(self):
        self.assertIn(
            "Write the whole commit message to a temporary UTF-8 file outside the "
            "repository and commit it with `git commit -F <path>`",
            self.instructions,
        )
        self.assertIn(
            "Never assemble the message with `git commit -m` or with shell escape "
            "sequences",
            self.instructions,
        )
        self.assertIn(
            "read the message back with `git log -1 --pretty=%B`", self.instructions
        )

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

    def test_documents_force_push_recovery_and_helper_inputs(self):
        self.assertIn(
            "safely realign a force-pushed PR branch only when `git cherry` proves "
            "the local commits have no unique patches",
            self.instructions,
        )
        self.assertIn(
            "If it reports `head_moved`, stop on that exact error", self.instructions
        )
        self.assertIn(
            "objects contain exactly `path`, `line`, `side`, and `body`",
            self.instructions,
        )
        self.assertIn(
            "`plan --state <path> --batch <id> --candidates <ids...> "
            "--label <label>",
            self.instructions,
        )

    def test_documents_shell_safe_drop_and_evaluator_improved_fixes(self):
        self.assertIn(
            "--rationale-file <file-or->", self.instructions
        )
        self.assertIn(
            "prefer a temporary UTF-8 `--rationale-file` for model-authored text",
            self.instructions,
        )
        self.assertIn(
            "The registered anchor identifies the defect, not the maximum edit range",
            self.instructions,
        )
        self.assertIn(
            "including lines already changed by the PR", self.instructions
        )
        self.assertIn(
            "Do not absorb a distinct defect merely because the evaluator noticed it",
            self.instructions,
        )
        self.assertIn(
            "expand the planned paths before editing", self.instructions
        )

    def test_routes_plausible_unresolved_candidates_to_the_evaluator(self):
        self.assertIn(
            '"Prefer silence" governs the final finding threshold, not evaluator access',
            self.instructions,
        )
        self.assertIn(
            "register a candidate when it presents a concrete, plausible defect",
            self.instructions,
        )
        self.assertIn(
            "factuality or actionability remains genuinely unresolved",
            self.instructions,
        )
        self.assertIn(
            "Self-drop a lead before registration only when direct evidence already "
            "disproves it",
            self.instructions,
        )
        self.assertIn(
            "do not self-drop them merely because they may prove to be no-ops",
            self.instructions,
        )

    def test_final_response_renders_commit_dropped_candidate_and_pr_links(self):
        self.assertIn(
            "canonical pull request link from the most recent preflight result's "
            "`pr.pr_url`",
            self.instructions,
        )
        self.assertIn(
            "Render ordinary Markdown, never a fenced code block", self.instructions
        )
        self.assertIn(
            "[<short-sha> <short batch summary>](<pr.pr_url>/changes/<full-sha>)",
            self.instructions,
        )
        self.assertNotIn("/commits/<full-sha>", self.instructions)
        self.assertIn(
            "**PR:** [#<pr.number> <pr.title>](<pr.pr_url>)", self.instructions
        )
        self.assertNotIn("PR: <pr.pr_url>", self.instructions)
        self.assertIn(
            "For a clean pass with zero commits and no no-code outcomes",
            self.instructions,
        )
        self.assertIn(
            "With no dropped candidates, render exactly the `**Outcome:**` line "
            "followed by the `**PR:**` line",
            self.instructions,
        )
        self.assertIn(
            "after `**Outcome:**` so the primary result remains first and immediately "
            "before `**PR:**`",
            self.instructions,
        )
        self.assertIn("`**Dropped candidates:**`", self.instructions)
        self.assertIn(
            "List every dropped candidate separately with its original problem and "
            "the evaluator's concrete reason; do not collapse them into a count",
            self.instructions,
        )
        self.assertIn(
            "Report only candidates evaluated and dropped during this run",
            self.instructions,
        )
        self.assertNotIn(
            "Report dropped candidates only as a count", self.instructions
        )
        self.assertIn(
            "Do not invent a commit, no-code, or narrative line", self.instructions
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
            "whenever the head contains any change not published by this run, read "
            "the whole pinned diff",
            self.instructions,
        )
        self.assertIn(
            "the new preflight head equals the head returned by the preceding `publish`",
            self.instructions,
        )
        self.assertIn(
            "the only new commits were this loop's recorded commits",
            self.instructions,
        )
        self.assertIn(
            "carry forward the prior full review and re-review only those newly "
            "published commits in their current pinned-diff context",
            self.instructions,
        )
        self.assertIn(
            "unchanged hunks do not need to be read again", self.instructions
        )
        self.assertIn(
            "the prior review plus the exact proven delta covers every line of the "
            "current pin",
            self.instructions,
        )
        self.assertIn(
            "Before retaining a candidate that asserts a semantic or convention "
            "violation",
            self.instructions,
        )
        self.assertIn(
            "read the implementation or authoritative documentation of any shared "
            "helper that defines that contract",
            self.instructions,
        )
        self.assertIn(
            "do not send an assumption to the evaluator when one direct helper read "
            "can disprove it",
            self.instructions,
        )
        self.assertIn(
            "refuse to publish a skipped batch, require the commits sitting on the "
            "pinned head to be exactly the recorded ones",
            self.instructions,
        )

    def test_isolates_validation_failures_owned_by_another_pending_batch(self):
        self.assertIn(
            "When evidence shows the failure is caused solely by a different "
            "still-pending candidate assigned to another batch",
            self.instructions,
        )
        self.assertIn(
            "focused validation that isolates the current batch", self.instructions
        )
        self.assertIn(
            "if that batch's own relevant checks pass, record it normally",
            self.instructions,
        )
        self.assertIn(
            "preserve the other failure, and handle that candidate in its own batch",
            self.instructions,
        )
        self.assertIn(
            "Never use this exception for an unexplained failure, a shared root cause, "
            "or a failure introduced by the current batch",
            self.instructions,
        )

    def test_closes_every_run_with_a_categorized_retrospective(self):
        self.assertIn(
            "## Self Review Loop Agent Retrospective", self.instructions
        )
        self.assertIn(
            "**Self Review Loop Agent Retrospective**", self.instructions
        )
        self.assertIn(
            "Silence is the normal outcome, and a run that went smoothly reports "
            "nothing",
            self.instructions,
        )
        self.assertIn(
            "Produce the retrospective on every terminal outcome, including a clean "
            "pass, an unfixable validation stop, `max_iterations_reached`, "
            "`nothing_to_publish`, a helper error, and a failed **Model Gate**",
            self.instructions,
        )
        for category in (
            "- **Agent**:",
            "- **Helper**:",
            "- **General instructions**:",
            "- **Repository**:",
        ):
            self.assertIn(category, self.instructions)
        self.assertIn(
            "Report only friction actually encountered in this run", self.instructions
        )
        self.assertIn(
            "The **Self Review Loop Agent Retrospective** is the only content "
            "permitted after the `**PR:**` line",
            self.instructions,
        )
        self.assertIn("The retrospective is advisory and chat-only", self.instructions)
        self.assertIn(
            "never commit it or push it as part of this loop", self.instructions
        )
        self.assertIn(
            "omit the label entirely when there is nothing to report", self.instructions
        )
        self.assertIn("Emit exactly one terminal response", self.instructions)
        self.assertIn("must be the absolute final block", self.instructions)
        self.assertIn("after its last list item, stop immediately", self.instructions)
        self.assertIn(
            "never emit a preliminary final response followed by a fuller report",
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

    def test_invalid_anchor_reports_nearest_and_accepted_lines(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.validate_candidates(
                [{"path": "app.py", "line": 4, "side": "RIGHT", "body": "Fix it."}],
                self.anchors,
            )

        self.assertIn("nearest valid RIGHT line: 3", str(error.exception))
        self.assertIn("accepted RIGHT lines: 2, 3", str(error.exception))

    def test_invalid_side_reports_the_other_sides_accepted_lines(self):
        anchors = {"app.py": {"LEFT": [7, 8], "RIGHT": []}}

        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.validate_candidates(
                [{"path": "app.py", "line": 8, "side": "RIGHT", "body": "Fix it."}],
                anchors,
            )

        self.assertIn("app.py has no changed RIGHT lines", str(error.exception))
        self.assertIn("accepted LEFT lines: 7, 8", str(error.exception))

    def test_invalid_anchor_path_reports_changed_paths(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.validate_candidates(
                [{"path": "other.py", "line": 3, "side": "RIGHT", "body": "Fix it."}],
                self.anchors,
            )

        self.assertIn("anchor path is not in the pinned diff: other.py", str(error.exception))
        self.assertIn("changed paths: app.py", str(error.exception))

    def test_candidates_help_documents_the_required_object_schema(self):
        output = io.StringIO()

        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            MODULE.build_parser().parse_args(["candidates", "--help"])

        help_text = " ".join(output.getvalue().split())
        self.assertIn("each object must contain exactly path (string)", help_text)
        self.assertIn("line (integer)", help_text)
        self.assertIn("side (LEFT or RIGHT)", help_text)
        self.assertIn("body (string)", help_text)

    def test_candidate_key_error_names_unexpected_and_missing_keys(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.validate_candidates(
                [
                    {
                        "id": 1,
                        "title": "Fix it",
                        "path": "app.py",
                        "line": 3,
                        "side": "RIGHT",
                    }
                ],
                self.anchors,
            )

        self.assertIn("unexpected keys: id, title", str(error.exception))
        self.assertIn("missing keys: body", str(error.exception))
        self.assertIn(
            "expected exactly: path, line, side, body", str(error.exception)
        )


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

    def test_waits_for_the_pushed_ref_to_propagate(self):
        with (
            mock.patch.object(
                MODULE,
                "remote_head",
                side_effect=["old-head", "old-head", "new-head"],
            ) as remote_head,
            mock.patch.object(MODULE.time, "sleep") as sleep,
        ):
            result = MODULE.wait_for_remote_head(
                "owner", "repo", "branch", "new-head"
            )

        self.assertEqual(result, "new-head")
        self.assertEqual(remote_head.call_count, 3)
        self.assertEqual(
            sleep.call_args_list,
            [
                mock.call(MODULE.REMOTE_REF_LAG_RETRY_DELAYS[0]),
                mock.call(MODULE.REMOTE_REF_LAG_RETRY_DELAYS[1]),
            ],
        )

    def test_stops_waiting_after_the_remote_ref_retry_budget(self):
        with (
            mock.patch.object(
                MODULE, "remote_head", return_value="old-head"
            ) as remote_head,
            mock.patch.object(MODULE.time, "sleep") as sleep,
        ):
            result = MODULE.wait_for_remote_head(
                "owner", "repo", "branch", "new-head"
            )

        self.assertEqual(result, "old-head")
        self.assertEqual(
            remote_head.call_count, len(MODULE.REMOTE_REF_LAG_RETRY_DELAYS) + 1
        )
        self.assertEqual(sleep.call_count, len(MODULE.REMOTE_REF_LAG_RETRY_DELAYS))

    def test_keeps_the_existing_pr_branch_checked_out(self):
        target = {"pr_url": "https://github.com/owner/repo/pull/7"}
        metadata = {"head_branch": "feature"}

        with (
            mock.patch.object(MODULE, "run") as run,
            mock.patch.object(MODULE, "git", return_value="feature"),
        ):
            checked_out_branch = MODULE.checkout_pr(Path("repo"), target, metadata)

        self.assertTrue(checked_out_branch)
        self.assertEqual(
            run.call_args,
            mock.call(
                ["gh", "pr", "checkout", target["pr_url"]],
                cwd=Path("repo"),
            ),
        )

    def test_realigns_equivalent_rebased_commits_after_force_push(self):
        target = {"pr_url": "https://github.com/owner/repo/pull/7"}
        metadata = {"head_branch": "feature", "head_sha": "remote"}
        checkout_error = MODULE.WorkflowError(
            "gh pr checkout failed: fatal: Not possible to fast-forward, aborting."
        )

        with (
            mock.patch.object(MODULE, "run", side_effect=checkout_error),
            mock.patch.object(
                MODULE,
                "git",
                side_effect=["feature", "local", "", "- local", ""],
            ) as git,
        ):
            checked_out_branch = MODULE.checkout_pr(Path("repo"), target, metadata)

        self.assertTrue(checked_out_branch)
        self.assertEqual(
            git.call_args_list,
            [
                mock.call(Path("repo"), "branch", "--show-current"),
                mock.call(Path("repo"), "rev-parse", "HEAD"),
                mock.call(Path("repo"), "rev-list", "--merges", "remote..local"),
                mock.call(Path("repo"), "cherry", "remote", "local"),
                mock.call(Path("repo"), "reset", "--hard", "remote"),
            ],
        )

    def test_reports_head_moved_when_force_push_leaves_unique_work(self):
        target = {"pr_url": "https://github.com/owner/repo/pull/7"}
        metadata = {"head_branch": "feature", "head_sha": "remote"}
        checkout_error = MODULE.WorkflowError(
            "gh pr checkout failed: fatal: Not possible to fast-forward, aborting."
        )

        with (
            mock.patch.object(MODULE, "run", side_effect=checkout_error),
            mock.patch.object(
                MODULE,
                "git",
                side_effect=["feature", "local", "", "+ unique"],
            ),
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "head_moved.*unique work.*unique"
            ):
                MODULE.checkout_pr(Path("repo"), target, metadata)

    def test_checks_out_the_remote_pr_head_when_on_another_branch(self):
        target = {"pr_url": "https://github.com/owner/repo/pull/7"}
        metadata = {"head_branch": "feature"}

        with (
            mock.patch.object(MODULE, "run") as run,
            mock.patch.object(MODULE, "git", return_value="session-branch"),
        ):
            checked_out_branch = MODULE.checkout_pr(Path("repo"), target, metadata)

        self.assertFalse(checked_out_branch)
        self.assertEqual(
            run.call_args,
            mock.call(
                ["gh", "pr", "checkout", target["pr_url"], "--detach"],
                cwd=Path("repo"),
            ),
        )

    def test_does_not_mask_other_checkout_failures(self):
        target = {"pr_url": "https://github.com/owner/repo/pull/7"}
        metadata = {"head_branch": "feature"}
        error = MODULE.WorkflowError("authentication failed")

        with (
            mock.patch.object(MODULE, "git", return_value="feature"),
            mock.patch.object(MODULE, "run", side_effect=error),
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "authentication failed"):
                MODULE.checkout_pr(Path("repo"), target, metadata)


class StateCommandTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
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

    def test_drop_reads_shell_sensitive_rationale_from_utf8_file(self):
        path = write_state(self.directory)
        self.register(
            path, [{"path": "app.py", "line": 3, "side": "RIGHT", "body": "Fix it."}]
        )
        rationale_path = self.directory / "rationale.txt"
        rationale_path.write_text(
            'Rejected: use isRegisteredWithDestination() ("already guarded").',
            encoding="utf-8",
        )

        MODULE.command_drop(
            SimpleNamespace(
                state=str(path),
                candidates=[1],
                rationale=None,
                rationale_file=str(rationale_path),
            )
        )

        saved = json.loads(path.read_text(encoding="utf-8"))
        expected = (
            'Rejected: use isRegisteredWithDestination() ("already guarded").'
        )
        self.assertEqual(saved["review"]["candidates"][0]["rationale"], expected)
        self.assertEqual(self.emitted[-1]["rationale"], expected)

    def test_drop_reads_rationale_from_stdin(self):
        path = write_state(self.directory)
        self.register(
            path, [{"path": "app.py", "line": 3, "side": "RIGHT", "body": "Fix it."}]
        )

        with mock.patch.object(MODULE.sys, "stdin", io.StringIO("reason (from stdin)")):
            MODULE.command_drop(
                SimpleNamespace(
                    state=str(path),
                    candidates=[1],
                    rationale=None,
                    rationale_file="-",
                )
            )

        self.assertEqual(self.emitted[-1]["rationale"], "reason (from stdin)")

    def test_drop_parser_requires_one_rationale_source(self):
        parser = MODULE.build_parser()
        common = ["drop", "--state", "state.json", "--candidates", "1"]

        with self.assertRaises(SystemExit):
            parser.parse_args(common)
        with self.assertRaises(SystemExit):
            parser.parse_args(
                common
                + ["--rationale", "inline", "--rationale-file", "rationale.txt"]
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

    def test_resolve_marks_the_active_review_clean_at_its_pinned_head(self):
        path = write_state(self.directory)

        with mock.patch.object(
            MODULE, "metadata_for", return_value={"head_sha": "head1"}
        ):
            MODULE.command_resolve(SimpleNamespace(state=str(path), outcome="clean"))

        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["review"]["outcome"], "clean")
        self.assertEqual(state["review"]["clean_at_head_sha"], "head1")
        self.assertEqual(self.emitted[-1]["result"], "resolved")
        self.assertEqual(self.emitted[-1]["clean_at_head_sha"], "head1")

    def test_resolve_refuses_a_stale_live_pr_head(self):
        path = write_state(self.directory)

        with (
            mock.patch.object(
                MODULE, "metadata_for", return_value={"head_sha": "head2"}
            ),
            self.assertRaisesRegex(MODULE.WorkflowError, "PR head changed"),
        ):
            MODULE.command_resolve(SimpleNamespace(state=str(path), outcome="clean"))

        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("outcome", state["review"])

    def test_resolve_requires_no_candidates_or_all_dropped(self):
        for status in ("pending", "planned", "handled", "skipped"):
            with self.subTest(status=status):
                path = write_state(self.directory)
                state = json.loads(path.read_text(encoding="utf-8"))
                state["review"]["candidates"] = [{"id": 1, "status": status}]
                path.write_text(json.dumps(state), encoding="utf-8")

                with (
                    mock.patch.object(MODULE, "metadata_for") as metadata_for,
                    self.assertRaisesRegex(
                        MODULE.WorkflowError, "every candidate is dropped"
                    ),
                ):
                    MODULE.command_resolve(
                        SimpleNamespace(state=str(path), outcome="clean")
                    )

                metadata_for.assert_not_called()

        path = write_state(self.directory)
        state = json.loads(path.read_text(encoding="utf-8"))
        state["review"]["candidates"] = [
            {"id": 1, "status": "dropped"},
            {"id": 2, "status": "dropped"},
        ]
        path.write_text(json.dumps(state), encoding="utf-8")
        with mock.patch.object(
            MODULE, "metadata_for", return_value={"head_sha": "head1"}
        ):
            MODULE.command_resolve(SimpleNamespace(state=str(path), outcome="clean"))
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["review"]["outcome"],
            "clean",
        )


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
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
            mock.patch.object(
                MODULE, "remote_head", side_effect=["oldhead", "newhead"]
            ),
            mock.patch.object(
                MODULE, "metadata_for", return_value={"head_sha": "otherhead"}
            ) as metadata,
            mock.patch.object(MODULE.time, "sleep") as sleep,
            mock.patch.object(MODULE, "run"),
        ):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.command_publish(SimpleNamespace(state=str(path)))

        self.assertIn("PR head mismatch", str(error.exception))
        self.assertEqual(metadata.call_count, 2)
        sleep.assert_called_once_with(MODULE.PR_HEAD_LAG_RETRY_DELAY)

    def test_retries_a_stale_pr_head_after_confirming_the_pushed_ref(self):
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
                MODULE,
                "metadata_for",
                side_effect=[
                    {"head_sha": "oldhead"},
                    {"head_sha": "newhead"},
                ],
            ) as metadata,
            mock.patch.object(MODULE.time, "sleep") as sleep,
            mock.patch.object(MODULE, "run") as run,
        ):
            MODULE.command_publish(SimpleNamespace(state=str(path)))

        self.assertEqual(
            run.call_args.args[0][-3:], ["push", "origin", "HEAD:feature"]
        )
        self.assertEqual(metadata.call_count, 2)
        sleep.assert_called_once_with(MODULE.PR_HEAD_LAG_RETRY_DELAY)
        self.assertEqual(self.emitted[-1]["result"], "published")


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
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

    def preflight(
        self,
        state_path,
        *,
        metadata_sequence=None,
        max_iterations=5,
        checked_out_branch=True,
    ):
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
            mock.patch.object(
                MODULE, "checkout_pr", return_value=checked_out_branch
            ),
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

    def test_accepts_detached_checkout_from_another_branch(self):
        self.git_results[("branch", "--show-current")] = ""

        result = self.preflight(
            self.directory / "state.json", checked_out_branch=False
        )

        self.assertEqual(result["head_sha"], "head1")

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
        self.directory = Path(self.temporary.name).resolve()
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
