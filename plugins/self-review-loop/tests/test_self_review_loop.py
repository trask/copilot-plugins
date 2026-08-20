import contextlib
import importlib.util
import inspect
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
            "asks you to run the full Self Review Loop", self.instructions
        )
        self.assertIn(
            "Never defer to the generic `github-pr-diff-review` skill for these "
            "inputs, and never call it or pass the work to it",
            self.instructions,
        )

    def test_never_posts_review_comments_and_allows_required_metadata_corrections(self):
        self.assertIn(
            "This agent never posts an inline comment, a review body, or a PR comment. "
            "Its normal change to GitHub is pushing commits to the PR head branch. The "
            "only exception is the narrow title or description correction that **PR "
            "Metadata Accuracy** requires.",
            self.instructions,
        )
        self.assertNotIn("pending review", self.instructions)
        self.assertNotIn("thread", self.instructions.lower())
        self.assertIn("## PR Metadata Accuracy", self.instructions)
        self.assertIn(
            "takes precedence over the normal push-only limit on changes",
            self.instructions,
        )
        self.assertIn(
            "After each successful `publish`, and before the next `preflight`, read "
            "the live title and description again against the newly published diff",
            self.instructions,
        )
        self.assertIn(
            "If a commit from this loop made either one materially false or misleading",
            self.instructions,
        )
        self.assertIn(
            "Check once more before the terminal response", self.instructions
        )
        self.assertIn(
            "If you cannot make a required metadata correction safely, stop",
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
            "The agent type is required even when you set the model override",
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

    def test_runs_candidate_evaluations_in_parallel(self):
        self.assertIn("## Parallel Evaluation", self.instructions)
        self.assertIn(
            "Run those evaluations concurrently under **Parallel Evaluation**",
            self.instructions,
        )
        self.assertIn(
            "Launch each candidate's evaluator with the task tool in `mode: "
            "background`, and keep at most **5 evaluators in flight**",
            self.instructions,
        )
        self.assertIn(
            "overrides the general guidance against launching a background agent and "
            "then reading its result",
            self.instructions,
        )
        self.assertIn(
            "Running evaluators at the same time never relaxes the isolation rule", self.instructions
        )
        self.assertIn("Evaluators only read.", self.instructions)
        self.assertIn(
            "write any artifact outside the repository under its own unique "
            "temporary location",
            self.instructions,
        )
        self.assertIn(
            "Consume the collected verdicts in candidate ID order whatever order they "
            "finish in",
            self.instructions,
        )
        self.assertIn(
            "Run an evaluator again, alone and for its own candidate, when it fails, "
            "times out, or returns a verdict you cannot use",
            self.instructions,
        )
        self.assertIn(
            "never let a missing verdict decide by default to keep or drop the "
            "candidate",
            self.instructions,
        )
        self.assertIn(
            "Only this evaluation phase of a single iteration runs in parallel",
            self.instructions,
        )

    def test_allows_focused_runtime_evidence_without_duplicating_ci(self):
        self.assertIn(
            "Skip a blanket run of the test suite, and any other check whose only "
            "purpose is to repeat CI during review",
            self.instructions,
        )
        self.assertIn(
            "CI runs the suite before this loop edits anything, so running it again "
            "here settles nothing",
            self.instructions,
        )
        self.assertIn(
            "Everything else about tests belongs to this review: read the test code "
            "the pull request changes, investigate a test when it bears on a "
            "candidate, and run a targeted test when that is how you answer a "
            "question about the change",
            self.instructions,
        )
        self.assertIn(
            "This does not forbid running something locally as evidence",
            self.instructions,
        )
        self.assertIn(
            "run the smallest throwaway probe that establishes the relevant "
            "repository, shared-helper, dependency, or third-party runtime behavior",
            self.instructions,
        )
        self.assertIn(
            "Reuse the dependencies and caches you already have", self.instructions
        )
        self.assertIn(
            "keep generated files outside the repository, delete them afterward",
            self.instructions,
        )
        self.assertIn(
            "do not widen the probe into general validation", self.instructions
        )

    def test_reviews_for_suppressed_test_coverage(self):
        self.assertIn(
            "Treat suppressed coverage as a defect only a reviewer catches",
            self.instructions,
        )
        self.assertIn(
            "A deleted assertion, an added skip or disable annotation, a loosened "
            "matcher or widened tolerance, and an exception swallowed inside a test "
            "each turn a check green by asking less of the code",
            self.instructions,
        )
        self.assertIn(
            "register a candidate that says exactly what is no longer checked",
            self.instructions,
        )
        self.assertIn(
            "Judge the edit on that, not on its size or on the rationale attached "
            "to it",
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
            "Never build the message with `git commit -m`, and never use a shell "
            "escape sequence",
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
            "A missing history commit is not enough to raise the finding again",
            self.instructions,
        )
        self.assertIn(
            "Raise it again only when the pinned diff and current code show that the "
            "fix was removed",
            self.instructions,
        )
        self.assertIn(
            "run `preflight --repo-root <workspace>` with no target", self.instructions
        )

    def test_documents_force_push_recovery_and_helper_inputs(self):
        self.assertIn(
            "realign a force-pushed PR branch safely and only when `git cherry` proves "
            "the local commits hold no unique patches",
            self.instructions,
        )
        self.assertIn(
            "If it reports `head_moved`, stop on that exact error", self.instructions
        )
        self.assertIn(
            "objects hold exactly `path`, `line`, `side`, and `body`",
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
            "prefer a temporary UTF-8 `--rationale-file` for text a model wrote",
            self.instructions,
        )
        self.assertIn(
            "The registered anchor identifies the defect, not the largest edit you may "
            "make",
            self.instructions,
        )
        self.assertIn(
            "including lines the PR already changed", self.instructions
        )
        self.assertIn(
            "Do not absorb a separate defect just because the evaluator noticed it",
            self.instructions,
        )
        self.assertIn(
            "widen the planned paths before you edit", self.instructions
        )

    def test_routes_plausible_unresolved_candidates_to_the_evaluator(self):
        self.assertIn(
            "\"Prefer silence\" sets the bar for a final finding, not for reaching the "
            "evaluator",
            self.instructions,
        )
        self.assertIn(
            "register a candidate when it presents a concrete, plausible defect",
            self.instructions,
        )
        self.assertIn(
            "you still cannot settle whether it is factual or worth acting on",
            self.instructions,
        )
        self.assertIn(
            "Drop a lead yourself, before you register it, only when direct evidence "
            "already disproves it",
            self.instructions,
        )
        self.assertIn(
            "Do not drop it yourself just because it may turn out to change nothing",
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
            "For a clean pass with no commits and no no-code outcomes",
            self.instructions,
        )
        self.assertIn(
            "With no dropped candidates, render exactly the `**Outcome:**` line "
            "followed by the `**PR:**` line",
            self.instructions,
        )
        self.assertIn(
            "after `**Outcome:**` so the main result stays first, and immediately "
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
            "Report every candidate this run evaluated and dropped in any of its "
            "iterations",
            self.instructions,
        )
        self.assertIn(
            "A drop from an earlier iteration still belongs in the block after "
            "`preflight` folds it into `history`",
            self.instructions,
        )
        self.assertIn(
            "Leave out only an entry `history` carried in from a previous run",
            self.instructions,
        )
        self.assertNotIn(
            "not dropped entries carried forward in `history`", self.instructions
        )
        self.assertNotIn(
            "Report dropped candidates only as a count", self.instructions
        )
        self.assertIn(
            "Do not invent a commit, a no-code line, or a narrative line", self.instructions
        )

    def test_reads_the_preflight_result_from_the_helper_file(self):
        self.assertIn("write its complete result to `preflight_path`", self.instructions)
        self.assertIn(
            "print only a compact envelope carrying `result`, `state`, "
            "`preflight_path`",
            self.instructions,
        )
        self.assertIn(
            "Read `changed_files`, `pr_commits`, `pr_authored_files`, `history`, and "
            "`history_commit_presence` from the complete result at `preflight_path`",
            self.instructions,
        )
        self.assertIn(
            "check what you read against the envelope's `counts`",
            self.instructions,
        )
        self.assertIn(
            "The envelope's `counts.history_commits_missing` reports how many recorded "
            "commits no longer appear",
            self.instructions,
        )
        self.assertIn(
            "Do not compare the history and PR commit lists by hand",
            self.instructions,
        )

    def test_reads_the_status_result_from_the_helper_file(self):
        self.assertIn(
            "write the complete state snapshot to `status_path` as JSON",
            self.instructions,
        )
        self.assertIn(
            "open the complete result at `status_path` only when you need",
            self.instructions,
        )

    def test_reads_the_pinned_diff_from_the_helper_snapshot(self):
        self.assertIn(
            "Read the pinned diff only from the returned `diff_path`",
            self.instructions,
        )
        self.assertIn("Never run `gh pr diff` again", self.instructions)
        self.assertIn(
            "Review the whole pinned diff read from `diff_path`", self.instructions
        )
        self.assertIn(
            "Read the whole pinned diff on the first iteration, and whenever the head "
            "holds any change this run did not publish",
            self.instructions,
        )
        self.assertIn(
            "the new preflight head equals the head the preceding `publish` returned",
            self.instructions,
        )
        self.assertIn(
            "the only new commits were this loop's recorded commits",
            self.instructions,
        )
        self.assertIn(
            "carry the earlier full review forward and review only those newly "
            "published commits in their current pinned-diff context",
            self.instructions,
        )
        self.assertIn(
            "you do not need to read unchanged hunks again", self.instructions
        )
        self.assertIn(
            "the earlier review plus the exact proven delta covers every line of the "
            "current pin",
            self.instructions,
        )
        self.assertIn(
            "Before you keep a candidate that claims a semantic or convention violation",
            self.instructions,
        )
        self.assertIn(
            "read the implementation or the authoritative documentation of any shared "
            "helper that defines that contract",
            self.instructions,
        )
        self.assertIn(
            "Do not send an assumption to the evaluator when one direct read of that "
            "helper can disprove it",
            self.instructions,
        )
        self.assertIn(
            "refuse to publish a skipped batch, require the commits sitting on the "
            "pinned head to be exactly the recorded ones",
            self.instructions,
        )
        self.assertIn(
            "GitHub's ordered `pr_commits` with each commit's touched `files`",
            self.instructions,
        )
        self.assertIn(
            "Use `pr_commits`, `pr_authored_files`, and `diff_only_files` to work out "
            "scope when the PR base has drifted",
            self.instructions,
        )
        self.assertIn(
            "treat it as context from base drift rather than as work the PR authored",
            self.instructions,
        )
        self.assertIn(
            "knowing where a change came from narrows who owns it, not what the "
            "authoritative changeset is",
            self.instructions,
        )
        self.assertIn(
            "Do not compare against `origin/main` by hand, do not work out another "
            "merge-base range, and do not replace the helper's provenance with `git "
            "log` or `git show`",
            self.instructions,
        )

    def test_isolates_validation_failures_owned_by_another_pending_batch(self):
        self.assertIn(
            "When the evidence shows that a different candidate, still pending in "
            "another batch, is the only cause",
            self.instructions,
        )
        self.assertIn(
            "focused validation that isolates the current batch", self.instructions
        )
        self.assertIn(
            "if that batch's own relevant checks pass, record it as normal",
            self.instructions,
        )
        self.assertIn(
            "keep the other failure, and handle that candidate in its own batch",
            self.instructions,
        )
        self.assertIn(
            "Never use this exception for a failure you cannot explain, for a shared "
            "root cause, or for a failure the current batch introduced",
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
            "pass, a validation stop you could not fix, `max_iterations_reached`, "
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
            "Report only friction you actually hit in this run", self.instructions
        )
        self.assertIn(
            "The **Self Review Loop Agent Retrospective** is the only content allowed "
            "after the `**PR:**` line",
            self.instructions,
        )
        self.assertIn("The retrospective is advice, and it belongs in chat only", self.instructions)
        self.assertIn(
            "never commit it or push it as part of this loop", self.instructions
        )
        self.assertIn(
            "leave the label out entirely when there is nothing to report", self.instructions
        )
        self.assertIn("Emit exactly one terminal response", self.instructions)
        self.assertIn("must be the very last block", self.instructions)
        self.assertIn("stop immediately after its last list item", self.instructions)
        self.assertIn(
            "never emit a short final response and then a fuller report",
            self.instructions,
        )
        self.assertIn("never send a recap after the retrospective", self.instructions)

    def test_sends_the_terminal_response_as_the_last_message(self):
        self.assertIn(
            "The terminal response is the run's last message", self.instructions
        )
        self.assertIn(
            "send it in a message that calls no tool, and never follow it with a "
            "recap or a second summary",
            self.instructions,
        )
        self.assertIn(
            "Emit exactly one terminal response and make it the last message of the "
            "run",
            self.instructions,
        )
        self.assertIn(
            "including the final `resolve` or `publish`, the PR metadata recheck, and "
            "the deletion of any temporary file, before you compose this response",
            self.instructions,
        )
        self.assertIn(
            "then send the whole thing in one message that calls no tool",
            self.instructions,
        )
        self.assertIn(
            "attach any part of it to a message that also calls a tool",
            self.instructions,
        )
        self.assertIn("Once you send it the run is over", self.instructions)
        self.assertIn(
            "never send another message because a tool result, a reminder, or a turn "
            "boundary invites one",
            self.instructions,
        )
        self.assertIn(
            "never open with a narrative recap of what the run did", self.instructions
        )
        self.assertIn(
            "render the `**Outcome:**`, `**Dropped candidates:**`, and `**PR:**` "
            "lines at most once each",
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


class SharedStateBackendTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)

    def process(self, returncode=0, stdout="", stderr=""):
        return SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr
        )

    def response_for(self, document, sha="blob-sha"):
        content = MODULE.shared_state_bytes(document)
        return self.process(
            stdout=json.dumps(
                {
                    "content": MODULE.base64.b64encode(content).decode("ascii"),
                    "sha": sha,
                }
            )
        )

    def test_shared_state_is_off_by_default_without_config(self):
        with (
            mock.patch.dict(MODULE.os.environ, {}, clear=True),
            mock.patch.object(MODULE.Path, "home", return_value=self.home),
            mock.patch.object(MODULE, "run") as run,
        ):
            MODULE.publish_shared_state(
                {"repo_name": "owner/repo", "number": 7},
                section="self_review",
                field="clean_at_head_sha",
                value="head1",
                updated_at="2026-01-01T00:00:00Z",
            )

        run.assert_not_called()

    def test_environment_override_and_empty_force_off(self):
        config_path = self.home / MODULE.SHARED_STATE_CONFIG
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({"repository": "config/state"}), encoding="utf-8"
        )
        with (
            mock.patch.object(MODULE.Path, "home", return_value=self.home),
            mock.patch.dict(
                MODULE.os.environ,
                {MODULE.SHARED_STATE_ENV: " env/state "},
                clear=True,
            ),
        ):
            self.assertEqual(MODULE.resolve_shared_state_repo(), "env/state")
        with (
            mock.patch.object(MODULE.Path, "home", return_value=self.home),
            mock.patch.dict(
                MODULE.os.environ, {MODULE.SHARED_STATE_ENV: "  "}, clear=True
            ),
        ):
            self.assertIsNone(MODULE.resolve_shared_state_repo())

    def test_resolves_config_and_warns_for_malformed_config(self):
        config_path = self.home / MODULE.SHARED_STATE_CONFIG
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps({"repository": "config/state"}), encoding="utf-8"
        )
        with (
            mock.patch.dict(MODULE.os.environ, {}, clear=True),
            mock.patch.object(MODULE.Path, "home", return_value=self.home),
        ):
            self.assertEqual(MODULE.resolve_shared_state_repo(), "config/state")

        config_path.write_text("{", encoding="utf-8")
        stderr = io.StringIO()
        with (
            mock.patch.dict(MODULE.os.environ, {}, clear=True),
            mock.patch.object(MODULE.Path, "home", return_value=self.home),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertIsNone(MODULE.resolve_shared_state_repo())
        self.assertIn("invalid config file", stderr.getvalue())

        config_path.write_text(
            json.dumps({"repository": "not-a-repository"}), encoding="utf-8"
        )
        stderr = io.StringIO()
        with (
            mock.patch.dict(MODULE.os.environ, {}, clear=True),
            mock.patch.object(MODULE.Path, "home", return_value=self.home),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertIsNone(MODULE.resolve_shared_state_repo())
        self.assertIn("expected owner/repo", stderr.getvalue())

    def test_publish_preserves_other_entries_and_owned_sections(self):
        document = {
            "version": 1,
            "repository": "owner/repo",
            "pull_requests": {
                "7": {
                    "description": {
                        "validated_head_sha": "other",
                        "updated_at": "2026-01-01T00:00:00Z",
                    }
                },
                "8": {"custom": {"kept": True}},
            },
        }
        with (
            mock.patch.object(
                MODULE, "resolve_shared_state_repo", return_value="state/repo"
            ),
            mock.patch.object(
                MODULE, "run", side_effect=[self.response_for(document), self.process()]
            ) as run,
        ):
            MODULE.publish_shared_state(
                {"repo_name": "owner/repo", "number": 7},
                section="self_review",
                field="clean_at_head_sha",
                value="head1",
                updated_at="2026-01-02T00:00:00Z",
            )

        payload = json.loads(run.call_args_list[1].kwargs["input_text"])
        published = json.loads(
            MODULE.base64.b64decode(payload["content"]).decode("utf-8")
        )
        self.assertEqual(published["pull_requests"]["8"], {"custom": {"kept": True}})
        self.assertEqual(
            published["pull_requests"]["7"]["description"],
            document["pull_requests"]["7"]["description"],
        )
        self.assertEqual(
            published["pull_requests"]["7"]["self_review"],
            {
                "clean_at_head_sha": "head1",
                "updated_at": "2026-01-02T00:00:00Z",
            },
        )

    def test_skips_write_when_merged_document_is_unchanged(self):
        document = {
            "version": 1,
            "repository": "owner/repo",
            "pull_requests": {
                "7": {
                    "self_review": {
                        "clean_at_head_sha": "head1",
                        "updated_at": "2026-01-02T00:00:00Z",
                    }
                }
            },
        }
        with (
            mock.patch.object(
                MODULE, "resolve_shared_state_repo", return_value="state/repo"
            ),
            mock.patch.object(
                MODULE, "run", return_value=self.response_for(document)
            ) as run,
        ):
            MODULE.publish_shared_state(
                {"repo_name": "owner/repo", "number": 7},
                section="self_review",
                field="clean_at_head_sha",
                value="head1",
                updated_at="2026-01-02T00:00:00Z",
            )

        run.assert_called_once()

    def test_does_not_replace_a_newer_shared_fact(self):
        document = {
            "version": 1,
            "repository": "owner/repo",
            "pull_requests": {
                "7": {
                    "self_review": {
                        "clean_at_head_sha": None,
                        "updated_at": "2026-01-03T00:00:00Z",
                    }
                }
            },
        }
        with (
            mock.patch.object(
                MODULE, "resolve_shared_state_repo", return_value="state/repo"
            ),
            mock.patch.object(
                MODULE, "run", return_value=self.response_for(document)
            ) as run,
        ):
            MODULE.publish_shared_state(
                {"repo_name": "owner/repo", "number": 7},
                section="self_review",
                field="clean_at_head_sha",
                value="older-head",
                updated_at="2026-01-02T00:00:00Z",
            )

        run.assert_called_once()


class PullRequestMetadataTest(unittest.TestCase):
    def test_includes_githubs_ordered_pr_commit_list(self):
        target = MODULE.parse_target("https://github.com/owner/repo/pull/7")
        payload = {
            "number": 7,
            "title": "Add a thing",
            "url": "https://github.com/owner/repo/pull/7",
            "headRefName": "feature",
            "headRefOid": "head",
            "headRepositoryOwner": {"login": "fork"},
            "headRepository": {"name": "repo"},
            "baseRefName": "main",
            "baseRefOid": "base",
            "commits": [
                {"oid": "one", "messageHeadline": "First change"},
                {"oid": "two", "messageHeadline": "Second change"},
            ],
        }

        with mock.patch.object(MODULE, "gh_json", return_value=payload) as gh_json:
            metadata = MODULE.metadata_for(target)

        self.assertEqual(
            metadata["commits"],
            [
                {"sha": "one", "message": "First change"},
                {"sha": "two", "message": "Second change"},
            ],
        )
        self.assertIn("commits", gh_json.call_args.args[0][-1])


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

    def test_compares_only_recorded_history_commits_with_current_pr_commits(self):
        history = [
            {"id": 1, "commit": "old"},
            {"id": 2, "commit": "current"},
            {"id": 3, "commit": None},
            {"id": 4},
        ]

        self.assertEqual(
            MODULE.compare_history_commits(
                history,
                [{"sha": "current"}, {"sha": "other"}],
            ),
            [
                {"history_id": 1, "commit": "old", "in_pr_commits": False},
                {"history_id": 2, "commit": "current", "in_pr_commits": True},
            ],
        )


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


class CommitProvenanceTest(unittest.TestCase):
    def test_returns_each_pr_commit_with_its_sorted_unique_file_set(self):
        commits = [
            {"sha": "one", "message": "First"},
            {"sha": "two", "message": "Second"},
        ]

        with mock.patch.object(
            MODULE,
            "git",
            side_effect=["z.py\na.py\nz.py\n", "docs/readme.md\n"],
        ) as git:
            result = MODULE.commit_provenance(Path("repo"), commits)

        self.assertEqual(
            result,
            [
                {"sha": "one", "message": "First", "files": ["a.py", "z.py"]},
                {
                    "sha": "two",
                    "message": "Second",
                    "files": ["docs/readme.md"],
                },
            ],
        )
        self.assertEqual(
            git.call_args_list,
            [
                mock.call(
                    Path("repo"),
                    "diff-tree",
                    "--root",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "-m",
                    "one",
                ),
                mock.call(
                    Path("repo"),
                    "diff-tree",
                    "--root",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "-m",
                    "two",
                ),
            ],
        )


class StateCommandTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.emitted = []
        patcher = mock.patch.object(MODULE, "emit", self.emitted.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        environment = mock.patch.dict(
            MODULE.os.environ, {MODULE.SHARED_STATE_ENV: ""}, clear=False
        )
        environment.start()
        self.addCleanup(environment.stop)

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

        with (
            mock.patch.object(
                MODULE, "metadata_for", return_value={"head_sha": "head1"}
            ),
            mock.patch.object(MODULE, "publish_shared_state") as publish,
        ):
            MODULE.command_resolve(SimpleNamespace(state=str(path), outcome="clean"))

        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["review"]["outcome"], "clean")
        self.assertEqual(state["review"]["clean_at_head_sha"], "head1")
        self.assertEqual(self.emitted[-1]["result"], "resolved")
        self.assertEqual(self.emitted[-1]["clean_at_head_sha"], "head1")
        publish.assert_called_once_with(
            state["pr"],
            section="self_review",
            field="clean_at_head_sha",
            value="head1",
            updated_at=state["updated_at"],
        )

    def test_publish_failure_is_non_fatal_after_clean_state_is_saved(self):
        path = write_state(self.directory)
        stderr = io.StringIO()
        with (
            mock.patch.object(
                MODULE, "metadata_for", return_value={"head_sha": "head1"}
            ),
            mock.patch.dict(
                MODULE.os.environ,
                {MODULE.SHARED_STATE_ENV: "state/repo"},
                clear=False,
            ),
            mock.patch.object(
                MODULE,
                "read_shared_state",
                side_effect=MODULE.WorkflowError("network unavailable"),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            MODULE.command_resolve(SimpleNamespace(state=str(path), outcome="clean"))

        state = MODULE.load_state(path)
        self.assertEqual(state["review"]["clean_at_head_sha"], "head1")
        self.assertEqual(self.emitted[-1]["result"], "resolved")
        self.assertIn("network unavailable", stderr.getvalue())

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
        environment = mock.patch.dict(
            MODULE.os.environ, {MODULE.SHARED_STATE_ENV: ""}, clear=False
        )
        environment.start()
        self.addCleanup(environment.stop)
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
            "commits": [{"sha": "commit1", "message": "Change app"}],
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
        provenance=None,
    ):
        arguments = SimpleNamespace(
            target="owner/repo#7",
            repo_root=str(self.directory),
            state=str(state_path),
            max_iterations=max_iterations,
        )
        metadata_sequence = metadata_sequence or [self.metadata, self.metadata]
        provenance = provenance or [
            {
                "sha": "commit1",
                "message": "Change app",
                "files": ["app.py"],
            }
        ]
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
            mock.patch.object(
                MODULE,
                "commit_provenance",
                return_value=provenance,
            ),
            mock.patch.object(MODULE, "run"),
        ):
            MODULE.command_preflight(arguments)
        return self.emitted[-1]

    def full_result(self, envelope):
        return json.loads(
            Path(envelope["preflight_path"]).read_text(encoding="utf-8")
        )

    def test_pins_the_diff_snapshot_and_starts_the_first_iteration(self):
        state_path = self.directory / "state.json"

        envelope = self.preflight(state_path)
        result = self.full_result(envelope)

        self.assertEqual(envelope["result"], "ready")
        self.assertEqual(envelope["head_sha"], "head1")
        self.assertEqual(envelope["pr"]["number"], 7)
        self.assertEqual(envelope["pr"]["title"], "Add a thing")
        self.assertEqual(
            envelope["pr"]["pr_url"], "https://github.com/owner/repo/pull/7"
        )
        self.assertEqual(envelope["diff_bytes"], len(DIFF.encode("utf-8")))
        self.assertEqual(
            envelope["counts"],
            {
                "changed_files": 1,
                "diff_only_files": 0,
                "history": 0,
                "history_commits_missing": 0,
                "pr_authored_files": 1,
                "pr_commits": 1,
            },
        )
        for field in ("changed_files", "pr_commits", "history"):
            self.assertNotIn(field, envelope)
        self.assertEqual(
            Path(envelope["preflight_path"]),
            MODULE.preflight_path_for(state_path),
        )
        self.assertEqual(result["result"], "ready")
        self.assertEqual(result["head_sha"], "head1")
        self.assertEqual(result["pr"], self.metadata)
        self.assertEqual(result["changed_files"], ["app.py"])
        self.assertEqual(
            result["pr_commits"],
            [{"sha": "commit1", "message": "Change app", "files": ["app.py"]}],
        )
        self.assertEqual(result["pr_authored_files"], ["app.py"])
        self.assertEqual(result["diff_only_files"], [])
        self.assertEqual(result["history_commit_presence"], [])
        self.assertEqual(result["iteration"], 1)
        self.assertEqual(result["history"], [])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state["review"]["anchors"], {"app.py": {"LEFT": [2], "RIGHT": [2, 3]}}
        )
        self.assertEqual(state["review"]["candidates"], [])
        diff_path = Path(envelope["diff_path"])
        self.assertEqual(diff_path, MODULE.diff_path_for(state_path))
        self.assertEqual(state["review"]["diff_path"], str(diff_path))
        self.assertEqual(diff_path.read_text(encoding="utf-8"), DIFF)

    def test_fresh_review_publishes_null_to_retract_a_clean_review(self):
        state_path = write_state(self.directory)
        state = MODULE.load_state(state_path)
        state["review"]["clean_at_head_sha"] = "head1"
        MODULE.save_state(state_path, state)

        with mock.patch.object(MODULE, "publish_shared_state") as publish:
            self.preflight(state_path)

        saved = MODULE.load_state(state_path)
        self.assertNotIn("clean_at_head_sha", saved["review"])
        publish.assert_called_once_with(
            saved["pr"],
            section="self_review",
            field="clean_at_head_sha",
            value=None,
            updated_at=saved["updated_at"],
        )

    def test_first_local_review_publishes_null_for_cross_machine_retraction(self):
        state_path = self.directory / "new-state.json"

        with mock.patch.object(MODULE, "publish_shared_state") as publish:
            self.preflight(state_path)

        saved = MODULE.load_state(state_path)
        publish.assert_called_once_with(
            saved["pr"],
            section="self_review",
            field="clean_at_head_sha",
            value=None,
            updated_at=saved["updated_at"],
        )

    def test_reports_diff_files_absent_from_all_pr_commits(self):
        result = self.full_result(
            self.preflight(
                self.directory / "state.json",
                provenance=[
                    {
                        "sha": "commit1",
                        "message": "Change docs",
                        "files": ["docs/readme.md"],
                    }
                ],
            )
        )

        self.assertEqual(result["pr_authored_files"], ["docs/readme.md"])
        self.assertEqual(result["diff_only_files"], ["app.py"])

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

        envelope = self.preflight(state_path)
        result = self.full_result(envelope)

        self.assertEqual(envelope["result"], "ready")
        self.assertEqual(envelope["iteration"], 2)
        self.assertEqual(envelope["counts"]["history"], 2)
        self.assertEqual(
            [(entry["id"], entry["outcome"]) for entry in result["history"]],
            [(1, "addressed"), (2, "dropped")],
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["next_candidate_id"], 3)
        self.assertEqual(state["review"]["id"], "pr-7-iteration-2")
        self.assertEqual(state["review"]["status"], "active")

    def test_reports_history_commits_missing_from_current_pr(self):
        state_path = write_state(
            self.directory,
            history=[
                {"id": 1, "outcome": "addressed", "commit": "old"},
                {"id": 2, "outcome": "addressed", "commit": "commit1"},
                {"id": 3, "outcome": "dropped", "commit": None},
            ],
        )

        envelope = self.preflight(state_path)
        result = self.full_result(envelope)

        self.assertEqual(envelope["counts"]["history_commits_missing"], 1)
        self.assertEqual(
            result["history_commit_presence"],
            [
                {"history_id": 1, "commit": "old", "in_pr_commits": False},
                {"history_id": 2, "commit": "commit1", "in_pr_commits": True},
            ],
        )
        state = MODULE.load_state(state_path)
        self.assertEqual(
            state["review"]["history_commit_presence"],
            result["history_commit_presence"],
        )

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

        envelope = self.emitted[-1]
        self.assertEqual(envelope["result"], "ready")
        self.assertEqual(envelope["pr"]["number"], 7)
        self.assertEqual(envelope["iterations"], 2)

    def test_writes_the_complete_state_snapshot_and_emits_a_compact_envelope(self):
        review = {
            "id": "pr-7-iteration-1",
            "status": "active",
            "iteration": 1,
            "head_sha": "head1",
            "diff_path": str(self.directory / "state.json.diff"),
            "anchors": {"app.py": {"LEFT": [2], "RIGHT": [2, 3]}},
            "pr_commits": [{"sha": "commit1", "message": "Change app", "files": []}],
            "diff_only_files": [],
            "candidates": [
                {"id": 1, "status": "pending", "path": "app.py", "body": "x" * 4096},
                {"id": 2, "status": "dropped", "path": "app.py", "body": "y" * 4096},
                {"id": 3, "status": "dropped", "path": "app.py", "body": "z" * 4096},
            ],
            "batches": [{"id": "batch-1", "status": "planned"}],
        }
        path = write_state(
            self.directory,
            iterations=1,
            review=review,
            history=[{"id": 9, "body": "w" * 4096}],
        )

        MODULE.command_status(
            SimpleNamespace(state=str(path), current=False, repo_root=None)
        )

        envelope = self.emitted[-1]
        status_path = MODULE.status_path_for(path)
        self.assertEqual(Path(envelope["status_path"]), status_path)
        self.assertNotIn("history", envelope)
        self.assertEqual(
            set(envelope["pr"]),
            {"number", "title", "pr_url", "repo_name", "head_branch", "base_branch"},
        )
        self.assertEqual(
            envelope["review"]["candidate_statuses"], {"pending": 1, "dropped": 2}
        )
        self.assertEqual(envelope["review"]["batch_statuses"], {"planned": 1})
        self.assertNotIn("anchors", envelope["review"])
        self.assertNotIn("candidates", envelope["review"])
        self.assertEqual(
            envelope["counts"],
            {
                "batches": 1,
                "candidates": 3,
                "changed_files": 1,
                "diff_only_files": 0,
                "history": 1,
                "pr_commits": 1,
            },
        )
        self.assertLess(len(json.dumps(envelope)), 2048)

        result = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(result["result"], "ready")
        self.assertEqual(result["review"], review)
        self.assertEqual(result["history"], [{"id": 9, "body": "w" * 4096}])
        self.assertEqual(result["iterations"], 1)

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
        self.assertNotIn("status_path", result)
        self.assertFalse(
            MODULE.status_path_for(self.directory / "missing.json").exists()
        )

    def test_cleanup_removes_the_state_file(self):
        path = write_state(self.directory)
        diff_path = MODULE.diff_path_for(path)
        diff_path.write_text(DIFF, encoding="utf-8")
        preflight_path = MODULE.preflight_path_for(path)
        preflight_path.write_text("{}", encoding="utf-8")
        status_path = MODULE.status_path_for(path)
        status_path.write_text("{}", encoding="utf-8")

        MODULE.command_cleanup(SimpleNamespace(state=str(path)))

        self.assertFalse(path.exists())
        self.assertFalse(diff_path.exists())
        self.assertFalse(preflight_path.exists())
        self.assertFalse(status_path.exists())
        self.assertEqual(self.emitted[-1]["result"], "cleaned_up")

    def test_cleanup_tolerates_a_missing_diff_snapshot(self):
        path = write_state(self.directory)

        MODULE.command_cleanup(SimpleNamespace(state=str(path)))

        self.assertFalse(path.exists())
        self.assertEqual(self.emitted[-1]["result"], "cleaned_up")


class StageOutcomeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.emitted = []
        patcher = mock.patch.object(MODULE, "emit", self.emitted.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def review(self, **overrides):
        review = {
            "id": "pr-7-iteration-1",
            "status": "active",
            "iteration": 1,
            "head_sha": "head1",
            "diff_path": str(self.directory / "state.json.diff"),
            "anchors": {"app.py": {"LEFT": [2], "RIGHT": [2, 3]}},
            "candidates": [],
            "batches": [],
        }
        review.update(overrides)
        return review

    def status(self, **overrides):
        path = write_state(self.directory, **overrides)
        MODULE.command_status(
            SimpleNamespace(state=str(path), current=False, repo_root=None)
        )
        envelope = self.emitted[-1]
        result = json.loads(
            MODULE.status_path_for(path).read_text(encoding="utf-8")
        )
        return envelope, result

    def clean_review(self):
        return self.review(outcome="clean", clean_at_head_sha="head1")

    def marker_of(self, state):
        """Read the clean-at-head marker the way an orchestrator reads it.

        This deliberately repeats the rule rather than calling the helper, so a
        change that lets `stage_outcome` claim `cleared` on its own still fails.
        """

        review = state.get("review")
        if not isinstance(review, dict) or review.get("outcome") != "clean":
            return None
        value = review.get("clean_at_head_sha")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def test_a_resolved_clean_review_cleared(self):
        envelope, result = self.status(review=self.clean_review())

        self.assertEqual(envelope["result"], "ready")
        self.assertEqual(envelope["stage_outcome"], "cleared")
        self.assertEqual(result["result"], "ready")
        self.assertEqual(result["stage_outcome"], "cleared")

    def test_a_batch_validation_blocked_reports_no_outcome(self):
        review = self.review(
            candidates=[{"id": 1, "status": "skipped", "path": "app.py"}],
            batches=[{"id": "batch-1", "status": "skipped"}],
        )

        envelope, result = self.status(review=review)

        self.assertNotIn("stage_outcome", envelope)
        self.assertNotIn("stage_outcome", result)

    def test_the_iteration_cap_reports_no_outcome(self):
        envelope, result = self.status(iterations=MODULE.DEFAULT_MAX_ITERATIONS)

        self.assertNotIn("stage_outcome", envelope)
        self.assertNotIn("stage_outcome", result)

    def test_an_unfinished_review_reports_no_outcome(self):
        pending, _ = self.status(
            review=self.review(
                candidates=[{"id": 1, "status": "pending", "path": "app.py"}]
            )
        )
        published, _ = self.status(
            review=self.review(status="published", published_head_sha="head2"),
            iterations=1,
        )

        self.assertNotIn("stage_outcome", pending)
        self.assertNotIn("stage_outcome", published)

    def test_the_outcome_can_say_that_it_has_no_answer(self):
        """A return type with no absence value has to invent an ending."""

        annotation = inspect.signature(MODULE.stage_outcome).return_annotation

        self.assertEqual(str(annotation).replace("'", ""), "str | None")
        self.assertIsNone(MODULE.stage_outcome({}))

    def test_a_state_that_holds_no_run_reports_no_outcome(self):
        target = MODULE.parse_target("owner/repo#7")

        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=self.directory),
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
        self.assertNotIn("stage_outcome", result)

    def test_cleared_never_outruns_the_recorded_clean_head(self):
        states = [
            {"review": self.review()},
            {"review": self.review(outcome="clean")},
            {"review": self.review(outcome="clean", clean_at_head_sha=None)},
            {"review": self.review(outcome="clean", clean_at_head_sha="   ")},
            {"review": self.review(clean_at_head_sha="head1")},
            {"review": self.review(status="published", clean_at_head_sha="head1")},
            {"review": self.review(outcome="dirty", clean_at_head_sha="head1")},
            {"review": None},
            {
                "review": self.review(
                    candidates=[{"id": 1, "status": "skipped", "path": "app.py"}]
                )
            },
            {"iterations": MODULE.DEFAULT_MAX_ITERATIONS},
            {"review": self.clean_review()},
            {"review": self.clean_review(), "iterations": 4},
        ]

        for overrides in states:
            with self.subTest(overrides=overrides):
                envelope, result = self.status(**overrides)
                state = MODULE.load_state(self.directory / "state.json")
                marker = self.marker_of(state)
                cleared = marker is not None
                self.assertEqual(envelope.get("stage_outcome") == "cleared", cleared)
                self.assertEqual(result.get("stage_outcome") == "cleared", cleared)
                if cleared:
                    self.assertEqual(envelope["review"]["clean_at_head_sha"], marker)
                    self.assertEqual(result["review"]["clean_at_head_sha"], marker)
                else:
                    self.assertNotIn("stage_outcome", envelope)
                    self.assertNotIn("stage_outcome", result)


if __name__ == "__main__":
    unittest.main()
