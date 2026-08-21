import argparse
import contextlib
import importlib.util
import inspect
import io
import json
from pathlib import Path
import re
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


def publish_args(path: Path, **overrides) -> SimpleNamespace:
    arguments = {
        "state": str(path),
        "validated": None,
        "not_validated": None,
        "rewrote": None,
    }
    arguments.update(overrides)
    return SimpleNamespace(**arguments)


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


LOCAL_VALIDATION_HEADING = "### Local Validation Before A Push"
def _agent_section(text, heading):
    """Return the body of one Markdown section, stopping at the next peer heading."""
    lines = text.split("\n")
    start = lines.index(heading)
    depth = len(heading) - len(heading.lstrip("#"))
    body = []
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            if level <= depth:
                break
        body.append(line)
    return "\n".join(body)


class AgentInstructionsTest(unittest.TestCase):
    def setUp(self):
        self.instructions = AGENT.read_text(encoding="utf-8")

    def test_documents_the_helper_activity_stamp_without_overselling_it(self):
        """A reader who thinks the stamp proves liveness stops checking further.

        The helper writes only when a subcommand runs, so an hour of silence is
        as consistent with hard thinking as with a hang.
        """
        self.assertIn("`last_helper_activity`", self.instructions)
        self.assertIn(
            "the moment this helper last wrote its state", self.instructions
        )
        self.assertIn("not proof the stage is alive", self.instructions)
        self.assertIn(
            "the agent driving it can think for a long time between two of them",
            self.instructions,
        )

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

    def test_defines_the_bar_each_evaluator_judges_against(self):
        self.assertIn("## Evaluation Standard", self.instructions)
        self.assertIn(
            "Give that evaluator the PR's stated scope, the relevant diff and "
            "context, the **Evaluation Standard**, and exactly one candidate",
            self.instructions,
        )
        self.assertIn(
            "Require two independent decisions, each judged against that "
            "standard and supported by evidence",
            self.instructions,
        )
        self.assertIn(
            "Would a reasonable author apply this fix or knowingly decline it, "
            "as part of what this PR already does?",
            self.instructions,
        )
        self.assertIn(
            "each evaluator judges against a fixed bar instead of its own taste",
            self.instructions,
        )
        self.assertIn(
            "Decision 1 asks whether this PR demonstrates the candidate as fact. "
            "Nothing here relaxes it.",
            self.instructions,
        )
        self.assertIn(
            "needs no user-visible impact, needs no runtime defect behind it, "
            "and needs no large fix",
            self.instructions,
        )
        self.assertIn("- dead code this PR creates.", self.instructions)
        self.assertIn(
            "a departure from the reviewed repository's own instructions, when "
            "the evaluator can name the instruction",
            self.instructions,
        )
        self.assertIn(
            "documentation, naming, or a test that this PR makes wrong or "
            "misleading",
            self.instructions,
        )
        self.assertIn(
            "A preference with no repository instruction behind it does not "
            "clear it",
            self.instructions,
        )

    def test_rejects_unprovable_doubt_and_worth_uncertainty_as_drop_reasons(self):
        self.assertIn("Both decisions need demonstrated doubt", self.instructions)
        self.assertIn(
            "never drops one because a caller, a use, or a reason might exist "
            "somewhere unseen",
            self.instructions,
        )
        self.assertIn(
            '"It cannot be ruled out" states that evidence is missing, so it '
            "decides nothing",
            self.instructions,
        )
        self.assertIn(
            "Each verdict names the decision it failed and the evidence behind "
            "that decision",
            self.instructions,
        )
        self.assertIn(
            "Run `drop` for any candidate where decision 1 fails or stays "
            "uncertain, or where decision 2 fails on evidence the evaluator "
            "named",
            self.instructions,
        )
        self.assertIn(
            "record the decision it failed together with the evaluator's "
            "concrete reason",
            self.instructions,
        )
        self.assertIn(
            "Uncertainty about decision 2 on its own never drops a candidate",
            self.instructions,
        )
        self.assertIn("--rationale-file", self.instructions)

    def test_narrows_the_silence_rule_to_preferences_without_an_instruction(self):
        self.assertIn(
            "a preference with no repository instruction behind it, or an issue "
            "that already existed",
            self.instructions,
        )
        self.assertNotIn("a triviality, a style preference", self.instructions)

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
            "keep a probe's own generated files outside the repository, delete "
            "them afterward",
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
        self.assertIn("The maximum is 5 iterations,", self.instructions)
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
            "register a candidate when the PR demonstrates it concretely and "
            "the **Evaluation Standard** admits it",
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

    def test_names_no_build_tool_or_programming_language(self):
        """Each stage runs under the configuration its own repository supplies.

        This list exists to fail on the one wrong fix that is tempting here:
        pasting a concrete build command into the file so the agent does not
        have to work one out. Every name is matched on a word boundary,
        because a bare substring on a short token eventually fires on an
        innocent word and gets deleted by whoever trips over it, and the guard
        is then gone.
        """
        forbidden = [
            "bazel",
            "cargo",
            "dotnet",
            "golang",
            "gradle",
            "gradlew",
            "java",
            "javac",
            "jest",
            "junit",
            "kotlin",
            "maven",
            "mvn",
            "npm",
            "pnpm",
            "pytest",
            "rustc",
            "tsc",
            "typescript",
            "yarn",
        ]
        found = sorted(
            name
            for name in forbidden
            if re.search(rf"\b{name}\b", self.instructions, re.IGNORECASE)
        )
        self.assertEqual([], found)

    def test_the_local_validation_fallback_publishes_instead_of_stopping(self):
        """A repository with no usable narrow command must not become a stop.

        Halting there would create a second class of false escalation on
        exactly the repositories where local validation buys nothing, so every
        paragraph that reaches for the skip flag has to push, and none of them
        may reach for escalation vocabulary.
        """
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        paragraphs = [
            paragraph
            for paragraph in section.split("\n\n")
            if "--not-validated" in paragraph
        ]
        self.assertTrue(paragraphs)
        for paragraph in paragraphs:
            with self.subTest(paragraph=paragraph):
                self.assertIn("publish", paragraph)
                self.assertNotIn("escalat", paragraph.lower())

    def test_every_validation_flag_the_section_names_reaches_publish(self):
        """Prose naming a flag the helper rejects would stop a push outright."""
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        named = sorted(set(re.findall(r"--[a-z][a-z-]+", section)))
        self.assertTrue(named)
        parser = MODULE.build_parser()
        for flag in named:
            with self.subTest(flag=flag):
                args = parser.parse_args(
                    ["publish", "--state", "state.json", flag, "value"]
                )
                self.assertEqual("publish", args.command)

    def test_publish_documents_every_validation_flag_it_accepts(self):
        """A flag the helper grows and the file never mentions goes unused."""
        parser = MODULE.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        accepted = sorted(
            option
            for action in subparsers.choices["publish"]._actions
            for option in action.option_strings
            if "valid" in option or "rewrote" in option
        )
        self.assertTrue(accepted)
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        for flag in accepted:
            with self.subTest(flag=flag):
                self.assertIn(flag, section)

    def test_local_validation_is_wired_into_the_step_that_pushes(self):
        """The requirement is only real where the run reaches the push."""
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        elsewhere = self.instructions.replace(section, "")
        self.assertIn(f"**{LOCAL_VALIDATION_HEADING.lstrip('# ')}**", elsewhere)

    def test_covering_checks_are_not_narrowed_to_compilation(self):
        """The failure this requirement was written for compiled cleanly.

        It was a documentation comment that a separate documentation task
        rejected, so wording that let covering mean "it builds" would sail
        past the very cycle this is meant to save.
        """
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        for word in ["documentation", "lint", "format"]:
            with self.subTest(word=word):
                self.assertIn(word, section)

    def test_requires_committing_what_a_fixing_command_rewrote(self):
        """A rewrite left in the worktree fails silently.

        The push carries the earlier commit, the same check fails on the pull
        request anyway, and the next reset discards the rewritten files.
        """
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        self.assertIn("fixing form", section)
        rewrite_paragraphs = [
            paragraph
            for paragraph in section.split("\n\n")
            if re.search(r"rewr\w+", paragraph, re.IGNORECASE)
            and "commit" in paragraph.lower()
        ]
        self.assertTrue(rewrite_paragraphs)

    def test_local_success_does_not_stand_in_for_the_checks(self):
        self.assertIn(
            "The pull request's checks remain the only thing that says a "
            "change is sound",
            self.instructions,
        )

    def test_routes_a_no_target_request_around_a_detached_worktree(self):
        """The pipeline leaves the worktree detached, so no branch resolves.

        A reader who copies the bare no-target form under a pipeline reaches a
        resolver that refuses on purpose, so both steps have to name what to
        pass instead of leaving the refusal as the answer.
        """
        self.assertIn(
            "`--current` finds that state through the branch that is checked "
            "out, and a detached worktree has no branch to look up, so pass "
            "`--state <path>` there instead.",
            self.instructions,
        )
        self.assertIn(
            "the pipeline leaves each stage's worktree detached at the PR head, "
            "so a request that reaches this loop from a pipeline must name the "
            "PR as a URL or `owner/repo#number`",
            self.instructions,
        )
        self.assertIn(
            "Leaving the target out is the attached case, not the shape to copy.",
            self.instructions,
        )

    def test_the_current_rule_admits_a_detached_worktree_has_no_pull_request(self):
        """The rule is still right about never guessing from a state file.

        It was only wrong to imply a checked-out branch is always there to ask.
        """
        self.assertIn(
            "`current` always means the PR attached to the branch that is "
            "checked out, and a detached worktree has no such PR",
            self.instructions,
        )

    def test_the_argument_hint_stops_selling_the_bare_form_as_the_default(self):
        """The hint is the shape a caller copies before reaching any step list.

        It used to promise the current branch's PR, which a detached worktree
        cannot supply, so the omission read as the ordinary way to call this.
        """
        self.assertIn(
            'argument-hint: "PR URL, PR number, or owner/repo#number; omit only '
            "when the PR's branch is checked out\"",
            self.instructions,
        )
        self.assertNotIn("omit to use the current branch's PR", self.instructions)


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
                MODULE.command_publish(publish_args(path))

        self.assertIn("neither dropped nor handled", str(error.exception))

    def test_refuses_to_publish_a_dirty_worktree(self):
        path = self.state_with([self.candidate()])

        with mock.patch.object(MODULE, "git", return_value=" M app.py"):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.command_publish(publish_args(path))

        self.assertIn("worktree is not clean", str(error.exception))

    def test_reports_nothing_to_publish_without_a_commit(self):
        path = self.state_with(
            [self.candidate(commit=None, rationale="already correct")]
        )

        with mock.patch.object(MODULE, "git", side_effect=["", "head1", ""]):
            MODULE.command_publish(publish_args(path))

        self.assertEqual(self.emitted[-1]["result"], "nothing_to_publish")
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["iterations"], 0)
        self.assertEqual(state["review"]["status"], "active")

    def test_refuses_to_publish_a_skipped_batch(self):
        path = self.state_with([self.candidate(status="skipped")])

        with mock.patch.object(MODULE, "git", return_value=""):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.command_publish(publish_args(path))

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
                MODULE.command_publish(publish_args(path))

        self.assertIn("unrecorded ['stray']", str(error.exception))

    def test_refuses_to_report_nothing_to_publish_over_a_stray_commit(self):
        path = self.state_with(
            [self.candidate(commit=None, rationale="already correct")]
        )

        with mock.patch.object(MODULE, "git", side_effect=["", "stray", "stray"]):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.command_publish(publish_args(path))

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
                MODULE.command_publish(publish_args(path))

        self.assertIn("missing ['newhead']", str(error.exception))

    def test_rejects_handled_candidates_without_publish_data(self):
        path = self.state_with([self.candidate(summary=None)])

        with mock.patch.object(MODULE, "git", return_value=""):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.command_publish(publish_args(path))

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
            MODULE.command_publish(publish_args(path))

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

    def test_records_the_local_validation_behind_the_push(self):
        """The state has to say what ran, or a live run proves nothing.

        The record is stamped with the head it pushed so a later reader can
        tell which publication it belongs to.
        """
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
            mock.patch.object(MODULE, "run"),
        ):
            MODULE.command_publish(
                publish_args(
                    path, validated=["check one"], rewrote=["check one"]
                )
            )

        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            [
                {
                    "head_sha": "newhead",
                    "status": "passed",
                    "commands": ["check one"],
                    "rewrote": ["check one"],
                }
            ],
            state["local_validation"],
        )
        self.assertEqual(
            state["local_validation"][-1], self.emitted[-1]["local_validation"]
        )

    def test_publishes_a_run_that_validated_nothing(self):
        """The record must never become a gate.

        A repository with no covering command still has to publish, or the
        requirement turns into a false escalation exactly where it buys
        nothing.
        """
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
            MODULE.command_publish(publish_args(path))

        run.assert_called_once()
        self.assertEqual("published", self.emitted[-1]["result"])
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("unreported", state["local_validation"][-1]["status"])

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
            MODULE.command_publish(publish_args(path))

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
                MODULE.command_publish(publish_args(path))

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
            MODULE.command_publish(publish_args(path))

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

    def test_status_reports_when_the_helper_last_wrote_its_state(self):
        """The only signal a reader has for telling working from wedged.

        Every write stamps it, so a stamp minutes old and a stamp an hour old
        are different answers to the question a person actually asks.
        """
        path = write_state(self.directory, updated_at="2026-02-03T04:05:06Z")

        MODULE.command_status(
            SimpleNamespace(state=str(path), current=False, repo_root=None)
        )

        envelope = self.emitted[-1]
        self.assertEqual("2026-02-03T04:05:06Z", envelope["last_helper_activity"])
        snapshot = json.loads(
            Path(envelope["status_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual("2026-02-03T04:05:06Z", snapshot["last_helper_activity"])

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



class PipelineBudgetTest(unittest.TestCase):
    """A stage budget belongs to an outer loop's iteration, not to a launch."""

    RECORDED = {"run": "run-a", "iteration": 2, "baseline": 3, "run_baseline": 1}

    def scope(self, state, **pipeline):
        return MODULE.pipeline_scope(state, SimpleNamespace(**pipeline))

    def test_a_standalone_invocation_is_left_exactly_as_it_was(self):
        """Absent, empty, and unusable run tokens must never read as a new run."""
        for pipeline in (
            {},
            {"pipeline_run": None, "pipeline_iteration": None},
            {"pipeline_run": "", "pipeline_iteration": 2},
            {"pipeline_run": 7, "pipeline_iteration": 2},
            {"pipeline_iteration": 2},
            {"pipeline_iteration": 2, "pipeline_max_iterations": 3},
        ):
            with self.subTest(pipeline=pipeline):
                self.assertIsNone(self.scope({"iterations": 3}, **pipeline))

    def test_the_run_token_alone_decides_whether_the_budget_is_scoped(self):
        """Enumerate every subset of the three arguments rather than assert it in prose.

        The two halves are not symmetric for a reader. An iteration with no run
        asks which run it belongs to and nothing can answer it. A run with no
        iteration still answers what the token is for, whether this loop has seen
        the run before, so it scopes on equality alone. Only the outer cap is
        optional in the other sense: leaving it out falls back rather than lifting
        the ceiling.
        """
        parts = {
            "run": {"pipeline_run": "run-a"},
            "iteration": {"pipeline_iteration": 2},
            "cap": {"pipeline_max_iterations": 3},
        }
        scoped_by_names = {
            (): False,
            ("run",): True,
            ("iteration",): False,
            ("cap",): False,
            ("run", "cap"): True,
            ("iteration", "cap"): False,
            ("run", "iteration"): True,
            ("run", "iteration", "cap"): True,
        }
        for names, scoped in scoped_by_names.items():
            with self.subTest(names=names):
                pipeline = {}
                for name in names:
                    pipeline.update(parts[name])
                scope = self.scope({"iterations": 9}, **pipeline)
                self.assertEqual(scoped, scope is not None)
                self.assertEqual(
                    scoped,
                    MODULE.absolute_iteration_cap(
                        scope, 5, pipeline.get("pipeline_max_iterations")
                    )
                    is not None,
                )

    def test_a_lone_run_token_resets_once_and_is_inert_on_every_relaunch(self):
        """This is what makes the degraded case coarser rather than launch-scoped.

        The caller mints one token per run and repeats it on every relaunch inside
        that run, so equality alone still tells a first sighting from a repeat. The
        budget therefore refreshes once when the run arrives and never again while
        it lasts, which is the stricter direction, not the unbounded one.
        """
        state = {"iterations": 5}

        first = self.scope(state, pipeline_run="run-a")
        self.assertEqual(5, first["baseline"])
        self.assertEqual(5, first["run_baseline"])

        state["pipeline_budget"] = first
        for spent in (5, 7, 40):
            with self.subTest(spent=spent):
                state["iterations"] = spent
                relaunch = self.scope(state, pipeline_run="run-a")
                self.assertEqual(5, relaunch["baseline"])
                self.assertEqual(5, relaunch["run_baseline"])

        state["iterations"] = 40
        next_run = self.scope(state, pipeline_run="run-b")
        self.assertEqual(40, next_run["baseline"])
        self.assertEqual(40, next_run["run_baseline"])

    def test_an_unusable_iteration_degrades_rather_than_refusing_the_pull_request(self):
        """Ignoring the run outright is the permanent refusal this contract removes.

        The durable count only ever climbs, so a position this loop discarded would
        leave a pull request that already reached the cap refusing every later run
        for the rest of its life. The usable half is used instead.
        """
        for iteration in (None, 0, -1, True, "2", 1.5):
            with self.subTest(iteration=iteration):
                scope = self.scope(
                    {"iterations": 5}, pipeline_run="run-a", pipeline_iteration=iteration
                )
                self.assertIsNotNone(scope)
                self.assertIsNone(
                    MODULE.exhausted_budget({"iterations": 5}, scope, 5, 10)
                )

    def test_an_iteration_with_no_run_cannot_be_completed_by_this_loops_own_state(self):
        """A run token must come from the caller, never from what this loop recorded.

        Reading it back out of an earlier budget, a head it pushed, or an
        escalation it wrote would be this loop naming its own position.
        """
        states = (
            {},
            {"iterations": 4},
            {"iterations": 4, "pipeline_budget": dict(self.RECORDED)},
            {"iterations": 4, "pr": {"head_sha": "aaaa"}, "history": [{"id": "one"}]},
            {"iterations": 4, "escalation": {"kind": "max_iterations"}},
            {"iterations": 4, "clean_at_head_sha": "aaaa"},
        )
        for state in states:
            with self.subTest(state=state):
                self.assertIsNone(
                    self.scope(dict(state), pipeline_iteration=2, pipeline_max_iterations=3)
                )

    def test_only_the_position_the_caller_passes_can_reset_the_budget(self):
        """Enumerate the inputs to a reset instead of claiming the property in prose.

        A repeat of one position stays inert no matter what this loop did in
        between: a new head, a commit it pushed, an escalation it recorded, a
        clearance, or more iterations it spent. Every one of those varies here
        while the caller's values stay the same, and neither baseline moves.
        """
        observable = (
            {},
            {"pr": {"head_sha": "new-head"}},
            {"pr": {"head_sha": "another-head"}, "attempt": {"status": "published"}},
            {"escalation": {"kind": "max_iterations"}},
            {"history": [{"id": "one"}, {"id": "two"}]},
            {"clean_at_head_sha": "new-head"},
            {"last_result": "published"},
        )
        for spent in (0, 3, 5, 40):
            for extra in observable:
                with self.subTest(spent=spent, extra=extra):
                    state = {
                        "iterations": spent,
                        "pipeline_budget": dict(self.RECORDED),
                        **extra,
                    }
                    scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=2)
                    self.assertEqual(self.RECORDED, scope)

    def test_a_stale_or_replayed_iteration_is_inert(self):
        """Strictly greater, so a repeat and a replay both buy nothing."""
        for iteration in (1, 2):
            with self.subTest(iteration=iteration):
                state = {"iterations": 9, "pipeline_budget": dict(self.RECORDED)}
                scope = self.scope(
                    state, pipeline_run="run-a", pipeline_iteration=iteration
                )
                self.assertEqual(self.RECORDED, scope)

    def test_a_genuine_advance_refreshes_only_the_per_iteration_budget(self):
        """The whole-run ceiling must survive an advance, or it bounds nothing."""
        state = {"iterations": 9, "pipeline_budget": dict(self.RECORDED)}

        scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=3)

        self.assertEqual(
            {"run": "run-a", "iteration": 3, "baseline": 9, "run_baseline": 1}, scope
        )

    def test_a_first_iteration_inside_a_run_scoped_budget_is_not_an_advance(self):
        """Nothing was recorded to advance past, so the run's own reset still stands."""
        state = {
            "iterations": 9,
            "pipeline_budget": {
                "run": "run-a",
                "iteration": None,
                "baseline": 5,
                "run_baseline": 5,
            },
        }

        scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=3)

        self.assertEqual(
            {"run": "run-a", "iteration": 3, "baseline": 5, "run_baseline": 5}, scope
        )

    def test_a_new_run_resets_both_budgets_even_when_its_iteration_went_backwards(self):
        """An outer run restarts at 1 while this state is durable per pull request.

        Comparing order alone would see the count go backwards on every later run
        and never reset again, refusing the pull request for the rest of its life.
        """
        state = {
            "iterations": 9,
            "pipeline_budget": {
                "run": "run-a",
                "iteration": 6,
                "baseline": 7,
                "run_baseline": 2,
            },
        }

        scope = self.scope(state, pipeline_run="run-b", pipeline_iteration=1)

        self.assertEqual(
            {"run": "run-b", "iteration": 1, "baseline": 9, "run_baseline": 9}, scope
        )

    def test_the_run_is_opaque_and_only_ever_compared_for_equality(self):
        """Tokens that would sort or parse are still just tokens."""
        state = {
            "iterations": 4,
            "pipeline_budget": {
                "run": "2026-05-01/7",
                "iteration": 3,
                "baseline": 2,
                "run_baseline": 0,
            },
        }

        same = self.scope(state, pipeline_run="2026-05-01/7", pipeline_iteration=3)
        self.assertEqual(2, same["baseline"])
        for other in ("2026-05-01/8", "2026-04-01/7", "7", "run", " 2026-05-01/7"):
            with self.subTest(other=other):
                scope = self.scope(state, pipeline_run=other, pipeline_iteration=3)
                self.assertEqual(4, scope["baseline"])
                self.assertEqual(4, scope["run_baseline"])

    def test_a_reset_never_rewrites_the_durable_count_itself(self):
        """Both budgets are baselines, so the per-PR iteration numbering stays monotone.

        Zeroing the count instead would restart the numbering, and an attempt id
        built from it would collide with one already folded into history, where a
        duplicate is dropped rather than recorded.
        """
        state = {"iterations": 9, "pipeline_budget": dict(self.RECORDED)}

        self.scope(state, pipeline_run="run-b", pipeline_iteration=1)

        self.assertEqual(9, state["iterations"])

    def test_the_ceiling_is_derived_from_the_callers_own_cap(self):
        scope = {"run": "run-a", "iteration": 1, "baseline": 0, "run_baseline": 0}
        self.assertEqual(15, MODULE.absolute_iteration_cap(scope, 5, 3))
        self.assertEqual(20, MODULE.absolute_iteration_cap(scope, 10, 2))

    def test_an_omitted_outer_cap_falls_back_rather_than_disabling_the_ceiling(self):
        """Only the outer cap is optional, and omitting it must not remove the bound."""
        scope = {"run": "run-a", "iteration": 1, "baseline": 0, "run_baseline": 0}
        for value in (None, 0, -1, True, "3"):
            with self.subTest(value=value):
                self.assertEqual(
                    5 * MODULE.DEFAULT_PIPELINE_MAX_ITERATIONS,
                    MODULE.absolute_iteration_cap(scope, 5, value),
                )

    def test_a_standalone_run_keeps_the_flat_per_pull_request_cap(self):
        """No arguments means the behavior this loop has always had."""
        for spent, expected in ((0, None), (4, None), (5, "iteration"), (9, "iteration")):
            with self.subTest(spent=spent):
                self.assertEqual(
                    expected,
                    MODULE.exhausted_budget({"iterations": spent}, None, 5, None),
                )

    def test_a_scoped_run_spends_against_its_baseline_and_not_the_lifetime_count(self):
        """A spent brake must not read as a permanent refusal.

        Ninety iterations over the pull request's life say nothing about the run
        that just started, which has spent none of its own budget.
        """
        scope = {"run": "run-a", "iteration": 1, "baseline": 90, "run_baseline": 90}
        self.assertIsNone(MODULE.exhausted_budget({"iterations": 90}, scope, 5, 10))
        self.assertIsNone(MODULE.exhausted_budget({"iterations": 94}, scope, 5, 10))
        self.assertEqual(
            "iteration", MODULE.exhausted_budget({"iterations": 95}, scope, 5, 10)
        )

    def test_the_whole_run_ceiling_holds_even_when_every_iteration_looks_fresh(self):
        """A caller that keeps advancing must still not spend without end."""
        scope = {"run": "run-a", "iteration": 4, "baseline": 10, "run_baseline": 0}
        self.assertEqual(
            "absolute", MODULE.exhausted_budget({"iterations": 10}, scope, 5, 10)
        )
        self.assertIsNone(MODULE.exhausted_budget({"iterations": 9}, scope, 5, 10))

    def test_preflight_takes_the_position_and_defaults_it_to_absent(self):
        parser = MODULE.build_parser()

        bare = parser.parse_args(["preflight"])
        self.assertIsNone(bare.pipeline_run)
        self.assertIsNone(bare.pipeline_iteration)
        self.assertIsNone(bare.pipeline_max_iterations)

        given = parser.parse_args(
            [
                "preflight",
                "--pipeline-run",
                "run-a",
                "--pipeline-iteration",
                "2",
                "--pipeline-max-iterations",
                "3",
            ]
        )
        self.assertEqual("run-a", given.pipeline_run)
        self.assertEqual(2, given.pipeline_iteration)
        self.assertEqual(3, given.pipeline_max_iterations)

    def test_the_helper_advertises_the_flag_an_orchestrator_probes_for(self):
        """An orchestrator reads the installed script to decide whether to send it.

        It omits the position entirely when the flag is missing, so renaming it
        would silently leave this stage unscoped rather than fail.
        """
        self.assertIn("--pipeline-run", SCRIPT.read_text(encoding="utf-8"))


class LauncherPositionInstructionsTest(unittest.TestCase):
    """The agent file has to take a position however the caller words it."""

    def setUp(self):
        self.instructions = AGENT.read_text(encoding="utf-8")

    def test_the_position_reaches_preflight_as_the_three_arguments(self):
        self.assertIn("### A Launcher's Loop Position", self.instructions)
        self.assertIn(
            "--pipeline-run <token> --pipeline-iteration <number> "
            "--pipeline-max-iterations <number>",
            self.instructions,
        )
        self.assertIn("Copy them exactly. Do not read the token", self.instructions)

    def test_keys_the_position_on_the_values_rather_than_one_spelling(self):
        """A launcher that words it differently still gets its budget scoped.

        Making one phrasing the trigger drops a position supplied any other way,
        and it drops it silently: the run reports cleanly and the budget was
        simply never scoped. What makes the widening safe is where a value came
        from, not how it was written.
        """
        self.assertIn("Read the values, not the spelling.", self.instructions)
        self.assertIn(
            "a spelling you do not recognize is still the caller's instruction",
            self.instructions,
        )
        self.assertIn("the caller may supply one and you may not", self.instructions)

    def test_the_two_halves_go_out_together_as_a_rule_on_the_sender(self):
        """The pairing binds what a launcher emits, never what this loop accepts.

        Reading it as a receiver rule would have this loop discard a position that
        arrived with a half missing, and its durable count would then refuse the
        pull request for good.
        """
        self.assertIn(
            "Omit all three only when the request names no position at all",
            self.instructions,
        )
        self.assertIn(
            "Send `--pipeline-run` and `--pipeline-iteration` together",
            self.instructions,
        )
        self.assertNotIn("the helper ignores a lone one", self.instructions)

    def test_the_loop_may_never_supply_a_value_itself(self):
        self.assertIn(
            "Never supply, guess, carry over, or reconstruct a value yourself",
            self.instructions,
        )
        self.assertIn(
            "never invent one to keep working after `max_iterations_reached`",
            self.instructions,
        )
        self.assertIn(
            "A value you produced would be this loop refreshing its own cap",
            self.instructions,
        )

    def test_the_flat_cap_is_stated_as_a_default_an_outer_loop_may_replace(self):
        self.assertIn("unless an outer loop sets its own", self.instructions)


class LocalValidationRecordTest(unittest.TestCase):
    """The record is what makes the push requirement falsifiable.

    Reading a stage's own state afterwards has to say whether it validated,
    skipped, or claimed nothing at all, because inferring that from the checks
    that fail later is exactly the guessing this replaced.
    """

    def entry(self, head="head1", **overrides):
        args = SimpleNamespace(validated=None, rewrote=None, not_validated=None)
        for key, value in overrides.items():
            setattr(args, key, value)
        return MODULE.local_validation_entry(args, head)

    def test_records_the_commands_that_ran_and_the_head_they_covered(self):
        entry = self.entry(validated=["check one", "check two"])
        self.assertEqual("passed", entry["status"])
        self.assertEqual(["check one", "check two"], entry["commands"])
        self.assertEqual([], entry["rewrote"])
        self.assertEqual("head1", entry["head_sha"])

    def test_separates_the_commands_that_rewrote_files(self):
        """A command that ran clean and one that changed files differ.

        Only the second has anything that must reach the commits being pushed.
        """
        entry = self.entry(validated=["check one"], rewrote=["check one"])
        self.assertEqual(["check one"], entry["rewrote"])
        self.assertEqual(["check one"], entry["commands"])

    def test_a_rewriting_command_counts_as_one_that_ran(self):
        """Naming a command as rewriting implies it ran.

        Folding that in keeps a malformed claim from reaching the state as a
        contradiction, and keeps it from becoming a reason to refuse.
        """
        entry = self.entry(rewrote=["check one"])
        self.assertEqual("passed", entry["status"])
        self.assertEqual(["check one"], entry["commands"])
        self.assertEqual(["check one"], entry["rewrote"])

    def test_records_the_reason_when_nothing_covering_ran(self):
        entry = self.entry(not_validated="no narrow command exists here")
        self.assertEqual("skipped", entry["status"])
        self.assertEqual("no narrow command exists here", entry["reason"])
        self.assertNotIn("commands", entry)

    def test_records_that_the_publication_claimed_nothing(self):
        """This is the value that shows the requirement being ignored.

        A run that says neither thing must be distinguishable from one that
        deliberately skipped, or a live run proves nothing either way.
        """
        self.assertEqual("unreported", self.entry()["status"])

    def test_blank_claims_are_treated_as_no_claim(self):
        entry = self.entry(validated=["  "], not_validated="   ")
        self.assertEqual("unreported", entry["status"])


class DetachedHeadTargetTest(unittest.TestCase):
    """A refusal that names no correction is a dead end for its caller.

    The resolver is right to refuse, because a commit can belong to more than
    one pull request and no tie-break belongs here. What it owes the caller is
    the one thing that gets them past it.
    """

    def test_the_refusal_names_the_correction_and_not_only_the_fault(self):
        with mock.patch.object(MODULE, "git", return_value=""):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.current_pr_target(Path("repo"))
        message = str(error.exception)
        self.assertIn("detached HEAD", message)
        self.assertIn(
            "pass the pull request explicitly as a URL or owner/repo#number",
            message,
        )


class PreflightHelpTest(unittest.TestCase):
    """`--help` is read by a caller building a call, not one recovering from it.

    An agent constructing a `preflight` invocation reads this line first. A hint
    that still promises the checked-out branch's pull request sends it to a
    resolver a detached worktree cannot satisfy, and the refusal's correction
    then arrives only after the launch it wasted.
    """

    def test_the_target_help_repeats_the_agent_file_hint(self):
        """Deriving the clause keeps one sentence across both surfaces.

        The agent file's own guard fixes what that clause says; this one stops
        the two from drifting apart.
        """
        hint = re.search(
            r'^argument-hint: "(.+)"$', AGENT.read_text(encoding="utf-8"), re.M
        )
        self.assertIsNotNone(hint)
        clause = hint.group(1).split("; ", 1)[1]
        subparsers = next(
            action
            for action in MODULE.build_parser()._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        target = next(
            action
            for action in subparsers.choices["preflight"]._actions
            if action.dest == "target"
        )
        self.assertTrue(
            target.help.endswith(f"; {clause}"),
            f"preflight target help {target.help!r} does not end with {clause!r}",
        )


if __name__ == "__main__":
    unittest.main()
