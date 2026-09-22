import argparse
import copy
from contextlib import ExitStack
import hashlib
import importlib.util
import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
import tempfile
from typing import Any
import unittest
from unittest import mock
import uuid


SCRIPT = Path(__file__).parents[1] / "scripts" / "copilot_review_loop.py"
AGENT = Path(__file__).parents[1] / "agents" / "copilot-review-loop.agent.md"
PLUGIN = Path(__file__).parents[1] / "plugin.json"
CCR_V2_REVIEW = (
    Path(__file__).parent / "fixtures" / "ccr-v2-previously-missed-review.json"
)
CCR_V2_RESOLVED_REVIEW = (
    Path(__file__).parent / "fixtures" / "ccr-v2-resolved-only-review.json"
)
LEGACY_REVIEW_DETAILS = (
    Path(__file__).parent / "fixtures" / "legacy-review-details-review.json"
)
CCA_DISABLED_RESULT = (
    Path(__file__).parent / "fixtures" / "cca-disabled-agent-task-result.json"
)
MALFORMED_VALIDATION_RESULT = (
    Path(__file__).parent
    / "fixtures"
    / "malformed-validation-agent-task-result.json"
)
MISSING_FINDING_TRAILER_RESULT = (
    Path(__file__).parent
    / "fixtures"
    / "missing-finding-trailer-agent-task-result.json"
)
EMPTY_ACTIVE_REVIEW_REQUIRED_STATE = (
    Path(__file__).parent
    / "fixtures"
    / "empty-active-review-required-state.json"
)
SOURCE_ONLY_REVIEW_REQUEST_BLOCKED_STATE = (
    Path(__file__).parent
    / "fixtures"
    / "source-only-review-request-blocked-16161-state.json"
)
COMPACT_V3_REPORT = (
    Path(__file__).parent / "fixtures" / "compact-v3-report.md"
)
FORWARD_COMPACT_V3_REPORT = (
    Path(__file__).parent / "fixtures" / "forward-compact-v3-report.md"
)
POSITIONAL_COMPACT_V4_REPORT = (
    Path(__file__).parent / "fixtures" / "positional-compact-v4-report.md"
)
FORWARD_REPOSITORY_347_REPORT = (
    Path(__file__).parent / "fixtures" / "forward-repository-347-report.md"
)
FORWARD_REPOSITORY_347_RESULT = (
    Path(__file__).parent
    / "fixtures"
    / "forward-repository-347-agent-task-result.json"
)
FLAT_IDENTITY_347_REPORT = (
    Path(__file__).parent / "fixtures" / "flat-identity-347-report.md"
)
FLAT_IDENTITY_347_RESULT = (
    Path(__file__).parent
    / "fixtures"
    / "flat-identity-347-agent-task-result.json"
)
SUPPRESSED_COLLAPSED_383_REPORT = (
    Path(__file__).parent / "fixtures" / "suppressed-collapsed-383-report.md"
)
SUPPRESSED_COLLAPSED_383_RESULT = (
    Path(__file__).parent
    / "fixtures"
    / "suppressed-collapsed-383-agent-task-result.json"
)
SUPPRESSED_COLLAPSED_383_SECOND_REPORT = (
    Path(__file__).parent
    / "fixtures"
    / "suppressed-collapsed-383-second-report.md"
)
SUPPRESSED_COLLAPSED_383_SECOND_RESULT = (
    Path(__file__).parent
    / "fixtures"
    / "suppressed-collapsed-383-second-agent-task-result.json"
)
NO_ARTIFACT_383_RESULT = (
    Path(__file__).parent
    / "fixtures"
    / "no-artifact-383-agent-task-result.json"
)
NO_ARTIFACT_20074_RESULT = (
    Path(__file__).parent
    / "fixtures"
    / "no-artifact-20074-agent-task-result.json"
)
LEGACY_VALIDATION_20050_RESULT = (
    Path(__file__).parent
    / "fixtures"
    / "legacy-validation-20050-agent-task-result.json"
)
SPEC = importlib.util.spec_from_file_location("copilot_review_loop", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
LOCAL_SOURCE_FINGERPRINT = MODULE.local_source_fingerprint


class GithubMutationPolicyTest(unittest.TestCase):
    def setUp(self):
        self.previous = MODULE.ACTIVE_GITHUB_MUTATION_POLICY
        MODULE.ACTIVE_GITHUB_MUTATION_POLICY = "source-only"
        self.addCleanup(
            setattr,
            MODULE,
            "ACTIVE_GITHUB_MUTATION_POLICY",
            self.previous,
        )

    def test_source_only_blocks_review_replies_before_github_access(self):
        with (
            mock.patch.object(MODULE, "fetch_review_comments") as fetch,
            self.assertRaisesRegex(MODULE.WorkflowError, "forbids review replies"),
        ):
            MODULE.post_missing_replies({}, [])

        fetch.assert_not_called()

    def test_pipeline_defaults_source_only_and_requires_explicit_allow(self):
        self.assertEqual(
            "source-only",
            MODULE.github_mutation_policy(
                argparse.Namespace(
                    pipeline_run="run-1",
                    github_mutation_policy=None,
                )
            ),
        )
        self.assertEqual(
            "allow",
            MODULE.github_mutation_policy(
                argparse.Namespace(
                    pipeline_run="run-1",
                    github_mutation_policy="allow",
                )
            ),
        )
        self.assertIn(
            "--github-mutation-policy source-only",
            AGENT.read_text(encoding="utf-8"),
        )


    def test_source_only_blocks_thread_resolution_before_graphql(self):
        with (
            mock.patch.object(MODULE, "graphql") as graphql,
            self.assertRaisesRegex(MODULE.WorkflowError, "thread resolution"),
        ):
            MODULE.resolve_threads([])

        graphql.assert_not_called()

    def test_source_only_blocks_review_requests_before_graphql(self):
        with (
            mock.patch.object(MODULE, "graphql") as graphql,
            self.assertRaisesRegex(MODULE.WorkflowError, "review requests"),
        ):
            MODULE.request_copilot({}, Path("state.json"), "1" * 40)

        graphql.assert_not_called()

    def test_rejects_an_unknown_policy_before_any_work(self):
        with self.assertRaisesRegex(
            MODULE.WorkflowError, "invalid GitHub mutation policy"
        ):
            MODULE.github_mutation_policy(
                argparse.Namespace(
                    pipeline_run="run-1",
                    github_mutation_policy="mixed",
                )
            )


class WindowsSubprocessTest(unittest.TestCase):
    def test_run_hides_windows_console_processes(self):
        completed = MODULE.subprocess.CompletedProcess(["git"], 0, "", "")
        with (
            mock.patch.object(MODULE, "IS_WINDOWS", True),
            mock.patch.object(
                MODULE.subprocess,
                "CREATE_NO_WINDOW",
                0x08000000,
                create=True,
            ),
            mock.patch.object(
                MODULE.subprocess, "run", return_value=completed
            ) as subprocess_run,
        ):
            MODULE.run(["git"])

        self.assertEqual(subprocess_run.call_args.kwargs["creationflags"], 0x08000000)
        environment = subprocess_run.call_args.kwargs["env"]
        self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")
        index = int(environment["GIT_CONFIG_COUNT"]) - 1
        self.assertEqual(environment[f"GIT_CONFIG_KEY_{index}"], "core.hooksPath")
        self.assertEqual(environment[f"GIT_CONFIG_VALUE_{index}"], os.devnull)

    def test_run_leaves_non_windows_process_options_unchanged(self):
        completed = MODULE.subprocess.CompletedProcess(["git"], 0, "", "")
        with (
            mock.patch.object(MODULE, "IS_WINDOWS", False),
            mock.patch.object(
                MODULE.subprocess, "run", return_value=completed
            ) as subprocess_run,
        ):
            MODULE.run(["git"])

        self.assertNotIn("creationflags", subprocess_run.call_args.kwargs)

    def test_python_child_uses_utf8_for_non_ascii_output(self):
        process = MODULE.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('\\u2014')",
            ]
        )
        self.assertEqual(process.stdout, "\N{EM DASH}")


def _literal_strings(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _literal_strings(node.body) | _literal_strings(node.orelse)
    # The writer may name a module-level string constant rather than inline the
    # literal, so the classification can live in one place. Resolve the name to
    # the value the module actually binds, so `recorded_results` still sees it.
    if isinstance(node, ast.Name):
        value = getattr(MODULE, node.id, None)
        if isinstance(value, str):
            return {value}
    return set()


def recorded_results() -> set[str]:
    """Every value the helper can record in `last_result`, read from its source.

    Derived rather than restated, so growing the writer fails the tests that
    classify these values instead of waiting for somebody to remember them.
    """

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "watcher_result"
        ):
            for argument in node.args:
                if not isinstance(argument, ast.Dict):
                    continue
                for key, value in zip(argument.keys, argument.values):
                    if isinstance(key, ast.Constant) and key.value == "result":
                        found |= _literal_strings(value)
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        assigned_names = {
            node.value.id
            for node in ast.walk(function)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name)
            for target in node.targets
            if isinstance(target, ast.Subscript)
            and isinstance(target.slice, ast.Constant)
            and target.slice.value == "last_result"
        }
        for node in ast.walk(function):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in assigned_names:
                    found |= _literal_strings(node.value)
    return found


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


class LegacyAgentInstructions:
    def setUp(self):
        self.instructions = AGENT.read_text(encoding="utf-8")

    def test_requires_evidence_and_reuses_unchanged_review_decisions(self):
        section = _agent_section(
            self.instructions,
            "## Investigation And Batching",
        )

        self.assertIn(
            "find concrete evidence that the current code fails, is unsafe, "
            "breaks an explicit requirement, or misses a known use case",
            section,
        )
        self.assertIn("A suggestion from Copilot is not evidence", section)
        self.assertIn(
            "Do not investigate a repeated concern again when the relevant code "
            "and evidence have not changed",
            section,
        )
        self.assertIn(
            "Reuse the earlier analysis and outcome",
            section,
        )

    def test_documents_the_helper_activity_stamp_without_overselling_it(self):
        """A reader who thinks the stamp proves liveness stops checking further.

        The helper writes only when a subcommand runs, so an hour of silence is
        as consistent with hard thinking as with a hang.
        """
        self.assertIn("`last_helper_activity`", AGENT.read_text(encoding="utf-8"))
        self.assertIn(
            "the moment this helper last wrote its state",
            AGENT.read_text(encoding="utf-8"),
        )
        self.assertIn("not proof the stage is alive", AGENT.read_text(encoding="utf-8"))
        self.assertIn(
            "the agent driving it can think for a long time between two of them",
            AGENT.read_text(encoding="utf-8"),
        )

    def test_names_the_session_from_preflight_metadata_idempotently(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "tools: [read, edit, search, execute, todo, rename_session]",
            instructions,
        )
        self.assertIn("## Session Naming", instructions)
        self.assertIn(
            "ensure the session name is `Copilot Review Loop: <PR number> - <PR title>`",
            instructions,
        )
        self.assertIn(
            "If the harness has already supplied that exact name",
            instructions,
        )
        self.assertIn("do not call `rename_session`", instructions)
        self.assertIn("Otherwise call `rename_session` once", instructions)
        self.assertIn(
            "accept that result and continue without retrying or reporting it as "
            "retrospective friction",
            instructions,
        )
        self.assertIn("Never use an interim number-only name", instructions)
        self.assertNotIn("call `rename_session` again", instructions)
        self.assertNotIn("immediately call `rename_session`", instructions)

    def test_bare_pr_reference_starts_the_full_review_loop(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            'description: "Explicit invocation only: never select automatically;',
            instructions,
        )
        self.assertIn(
            "## Activation: Bare PR References Run The Full Loop",
            instructions,
        )
        self.assertIn(
            "asks you to run the full Copilot Review Loop",
            instructions,
        )
        self.assertIn(
            "Choose the bundled helper command at once and start its `preflight` "
            "workflow",
            instructions,
        )
        self.assertIn(
            "Never defer to the generic `github-pr-diff-review` skill for these "
            "inputs, and never call it or pass the work to it",
            instructions,
        )

    def test_targetless_requests_resolve_the_current_branch_pr(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("name: Copilot Review Loop", instructions)
        self.assertIn(
            "`status --current --repo-root <workspace>`",
            instructions,
        )
        self.assertIn(
            "the PR attached to the branch that is checked out",
            instructions,
        )
        self.assertIn(
            "Never list, rank, or pick saved state files",
            instructions,
        )
        self.assertIn(
            "do not fall back to another PR",
            instructions,
        )
        self.assertIn(
            "run `preflight --repo-root <workspace>` with no target",
            instructions,
        )

    def test_scoped_to_copilot_review_comments_only(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "This agent handles Copilot review comments only.",
            instructions,
        )
        self.assertIn("`no_copilot_comments`", instructions)
        self.assertNotIn("push all", instructions)
        self.assertNotIn("--all-queues", instructions)
        self.assertNotIn("Workspace Inline Comments", instructions)

    def test_documents_that_the_helper_drops_human_threads(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "The helper drops every review thread a non-Copilot author started before "
            "it builds the queue",
            instructions,
        )
        self.assertIn(
            "Leave their comments to the user",
            instructions,
        )
        self.assertIn(
            "drop every thread a non-Copilot author started",
            instructions,
        )

    def test_documents_the_first_review_bootstrap(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "request the very first Copilot review when the PR has never had one",
            instructions,
        )
        self.assertIn(
            "the helper adds Copilot as a reviewer, checks that GitHub recorded the "
            "request",
            instructions,
        )

    def test_documents_the_clean_at_head_marker(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "`preflight`, `watch`, and `status` all report `clean_at_head_sha`",
            instructions,
        )
        self.assertIn("Publishing clears it", instructions)

    def test_documents_an_absent_copilot_review_as_needing_a_person(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "the wait ended with no usable Copilot review, so stop and report a run "
            "that needs a person rather than another attempt",
            instructions,
        )
        self.assertIn(
            "waited for a Copilot review and none arrived. This needs a person, not "
            "another attempt.",
            instructions,
        )
        self.assertIn("Do not ask to be run again in that outcome.", instructions)
        self.assertIn("never let it read like an ordinary uneventful run", instructions)

    def test_a_user_stopped_watch_is_not_reported_as_needing_a_person(self):
        """The user is already present, so those outcomes stay ordinary stop conditions."""
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "`head_changed`, `cancelled_locally`, or `stopped`: stop, and include that "
            "exact outcome in the final compact index.",
            instructions,
        )

    def test_documents_the_stage_outcome_vocabulary(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("`status` also reports `stage_outcome`", instructions)
        self.assertIn(
            "`cleared`, `skipped`, `no_progress`, `escalated`, or `carried`",
            instructions,
        )
        self.assertIn(
            "It never says whether this stage is green, because `clean_at_head_sha` "
            "alone says that",
            instructions,
        )
        self.assertIn("do not set, quote, or work around either one", instructions)
        self.assertIn(
            "the field is left out entirely when there is no state or no recorded "
            "ending",
            instructions,
        )

    def test_accepts_a_pr_target_for_an_unchecked_out_branch(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "PR URL or `owner/repo#number`",
            instructions,
        )
        self.assertIn("not checked out yet", instructions)

    def test_documents_marketplace_helper_paths(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("${COPILOT_HOME:-${USERPROFILE//\\\\//}/.copilot}", instructions)
        self.assertIn(
            "installed-plugins/trask-plugins/copilot-review-loop",
            instructions,
        )
        self.assertIn("$env:COPILOT_HOME", instructions)
        self.assertNotIn("~/.copilot/agents/", instructions)
        self.assertNotIn("pr-review-comments", instructions)

    def test_runs_autonomously_until_a_stop_condition(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "Never wait for `next`, `commit`, `looks good`, `publish`, or `push etc`",
            instructions,
        )
        self.assertIn(
            "preflight -> investigate -> batch -> commit -> publish -> watch",
            instructions,
        )
        self.assertIn("maximum is 5 iterations per invocation", instructions)
        self.assertNotIn("## Approval And Advancement", instructions)
        self.assertNotIn("## Revision, Revert, And Skip", instructions)

    def test_publish_detects_remote_head_divergence_before_push(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "compare the live remote PR head with the preflight pin directly before it "
            "pushes",
            instructions,
        )
        self.assertIn(
            "If `publish` returns `head_changed`, stop without retrying or pushing",
            instructions,
        )
        self.assertIn(
            "the run stopped to avoid overwriting the newer update",
            instructions,
        )
        self.assertIn("run the review loop again from the latest head", instructions)

    def test_empty_queue_without_clean_head_review_requests_review(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "`review_required`: the queue is empty, but the current head has no clean "
            "Copilot review",
            instructions,
        )
        self.assertIn("`publish --state <path> --no-comments`", instructions)
        self.assertIn(
            "An empty queue is clean only when `head_review_clean` is true",
            instructions,
        )

    def test_uses_pinned_head_ci_as_review_evidence(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "a CI log and a generated report file for the exact pinned PR head as "
            "first-class evidence",
            instructions,
        )
        self.assertIn("never use a result from another head", instructions)
        self.assertIn(
            "Pass all paths after one `--paths` flag, or repeat the flag; the helper "
            "keeps every value",
            instructions,
        )

    def test_documents_plans_required_batch_and_comment_flags(self):
        instructions = AGENT.read_text(encoding="utf-8")
        invocation = (
            "`plan --state <path> --batch <id> --comments <ids...> --label <label> "
            "[--paths <paths...>] [--validation <command>]`"
        )

        self.assertGreaterEqual(instructions.count(invocation), 2)
        self.assertIn(
            "`--batch` and `--comments` are required option names",
            instructions,
        )
        self.assertIn(
            "Always spell out the required `--batch` and `--comments` flags",
            instructions,
        )
        self.assertIn(
            "never pass the batch ID or a comment ID positionally",
            instructions,
        )

    def test_documents_records_required_batch_and_comment_flags(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "`record --state <path> --batch <id> --comments <ids...> "
            "--summary <summary> --reply-file <path>`",
            instructions,
        )
        self.assertIn(
            "either `--commit <sha>` or the no-code `--rationale <text>`", instructions
        )
        self.assertIn(
            "`skip --state <path> --batch <id> --comments <ids...> --rationale <text>`",
            instructions,
        )

    def test_documents_deterministic_active_watcher_handling(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "`await-watch --state <path>`: wait deterministically for an already "
            "running watcher",
            instructions,
        )
        self.assertIn("`watcher_cancellation_pending`", instructions)
        self.assertIn(
            "run the exact `wait_action` (`await-watch --state <path>`)",
            instructions,
        )
        self.assertIn(
            "You can safely run the returned `cancel_action` again", instructions
        )
        self.assertIn(
            "Never retry preflight blindly while the watcher is active",
            instructions,
        )

    def test_watcher_runs_synchronously_without_terminal_notification_handoff(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "terminal parameter `mode: sync`; leave out both `timeout` and "
            "`isBackground` entirely",
            instructions,
        )
        self.assertIn(
            "Never use `mode: async`, `isBackground: true`, or `timeout: 0`",
            instructions,
        )
        self.assertIn(
            "consume its final JSON result directly from that same call",
            instructions,
        )
        self.assertIn(
            "Do not send a final response while the watcher is active",
            instructions,
        )

    def test_final_response_links_the_exact_copilot_review(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "Render ordinary Markdown, never a fenced code block", instructions
        )
        self.assertIn(
            "[<short-sha> <short batch summary>](<pr.url>/changes/<full-sha>)",
            instructions,
        )
        self.assertNotIn("/commits/<full-sha>", instructions)
        self.assertIn(
            "[Copilot review <id>](<review-url>)",
            instructions,
        )
        self.assertIn(
            "build the same link from `head_review_id` and `head_review_url`",
            instructions,
        )
        self.assertIn(
            "Never print a bare review ID when its URL is available",
            instructions,
        )

    def test_final_response_uses_the_current_run_iteration_count(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "Set a run-local iteration counter to 0 before the first preflight",
            instructions,
        )
        self.assertIn(
            "After `published`, add exactly one to the run-local iteration counter",
            instructions,
        )
        self.assertIn(
            "`<n>` is the run-local iteration counter, not the helper's cumulative "
            "stored iteration count",
            instructions,
        )
        self.assertIn(
            "exits clean during its first preflight reports `0 iterations`",
            instructions,
        )
        self.assertIn(
            "begins with four stored iterations and publishes once reports `1 "
            "iteration`",
            instructions,
        )
        self.assertIn("`preflight --completed-run-iterations <n>`", instructions)
        self.assertIn(
            "a stored iteration from an earlier invocation never uses up the current "
            "invocation's five-iteration budget",
            instructions,
        )

    def test_documents_durable_commit_and_reply_formats(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "Copilot comment:\n\n<original Copilot comment, verbatim>",
            instructions,
        )
        self.assertIn(
            "repeat the label and the comment block for each original comment",
            instructions,
        )
        self.assertIn("do not add path attribution", instructions)
        self.assertIn("Analysis: <technical analysis and rationale>", instructions)
        self.assertIn("Upsides: <concrete benefits>", instructions)
        self.assertIn("Downsides: <concrete costs", instructions)
        self.assertIn("Addressed in <sha>.", instructions)
        self.assertIn("No code change.", instructions)
        self.assertIn("without the `Copilot comment:` section", instructions)

    def test_documents_file_based_commit_message_authoring(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "Write the whole commit message to a temporary UTF-8 file outside the "
            "repository and commit it with `git commit -F <path>`",
            instructions,
        )
        self.assertIn(
            "Never build the message with `git commit -m`, and never use a shell "
            "escape sequence",
            instructions,
        )
        self.assertIn(
            "read the message back with `git log -1 --pretty=%B`", instructions
        )

    def test_documents_suppressed_comment_behavior(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("latest Copilot review", instructions)
        self.assertIn("Never reply to or resolve a suppressed comment", instructions)
        self.assertIn("Derive them again on every iteration", instructions)

    def test_documents_independent_reply_publication(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "post each thread reply as its own published comment and never twice",
            instructions,
        )
        self.assertIn(
            "Each reply is published on its own rather than bundled into one review",
            instructions,
        )
        self.assertIn(
            "verification fails when any reply is left in a review nobody submitted",
            instructions,
        )

    def test_closes_every_run_with_a_categorized_retrospective(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("## Copilot Review Loop Agent Retrospective", instructions)
        self.assertIn("**Copilot Review Loop Agent Retrospective**", instructions)
        self.assertIn(
            "Silence is the normal outcome, and a run that went smoothly reports "
            "nothing",
            instructions,
        )
        self.assertIn(
            "Produce the retrospective on every terminal outcome, including a clean "
            "loop, a validation stop you could not fix, `max_iterations_reached`, "
            "`no_copilot_comments`, a helper error, and any watcher stop condition "
            "such as `head_changed` or `review_dismissed`",
            instructions,
        )
        for category in (
            "- **Agent**:",
            "- **Helper**:",
            "- **General instructions**:",
            "- **Repository**:",
        ):
            self.assertIn(category, instructions)
        self.assertIn("Report only friction you actually hit in this run", instructions)
        self.assertIn(
            "The **Copilot Review Loop Agent Retrospective** is the only content "
            "allowed after the `**Outcome:**` line",
            instructions,
        )
        self.assertIn(
            "The retrospective is advice, and it belongs in chat only", instructions
        )
        self.assertIn(
            "never turn it into a thread reply, a commit, or any other change to GitHub",
            instructions,
        )
        self.assertIn(
            "leave the label out entirely when there is nothing to report", instructions
        )
        self.assertIn("Emit exactly one terminal response", instructions)
        self.assertIn("must be the very last block", instructions)
        self.assertIn("stop immediately after its last list item", instructions)
        self.assertIn(
            "never emit a short final response and then a fuller report",
            instructions,
        )
        self.assertIn("never send a recap after the retrospective", instructions)

    def test_sends_the_terminal_response_as_the_last_message(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("The terminal response is the run's last message", instructions)
        self.assertIn(
            "send it in a message that calls no tool, and never follow it with a "
            "recap or a second summary",
            instructions,
        )
        self.assertIn(
            "Emit exactly one terminal response and make it the last message of the "
            "run",
            instructions,
        )
        self.assertIn("Finish every tool call the run needs", instructions)
        self.assertIn(
            "then send the whole thing in one message that calls no tool", instructions
        )
        self.assertIn(
            "attach any part of it to a message that also calls a tool", instructions
        )
        self.assertIn("Once you send it the run is over", instructions)
        self.assertIn(
            "never send another message because a tool result, a reminder, or a turn "
            "boundary invites one",
            instructions,
        )
        self.assertIn(
            "never open with a narrative recap of what the run did", instructions
        )
        self.assertIn(
            "render the `**Outcome:**` line at most once, and never begin a second "
            "report",
            instructions,
        )
        self.assertIn(
            "**Outcome:** \\`head_changed\\` after <n> iteration(s): the pull request "
            "changed during publishing from expected head \\`<expected-head>\\` to "
            "actual head \\`<actual-head>\\`. This run stopped without pushing to "
            "avoid overwriting the newer update. Run the review loop again from the "
            "latest head.",
            instructions,
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
            "Copilot's next review and the repository's own checks stay the "
            "only evidence",
            self.instructions,
        )

    def test_routes_a_no_target_request_around_a_detached_worktree(self):
        """The pipeline detaches the worktree, so no branch resolves there.

        A reader who copies the bare no-target form under a pipeline reaches a
        resolver that refuses on purpose, so both steps have to name what to
        pass instead of leaving the refusal as the answer.
        """
        self.assertIn(
            "`--current` reaches that state through the checked-out branch, and "
            "a detached worktree names no branch, so pass `--state <path>` "
            "there and skip the lookup.",
            self.instructions,
        )
        self.assertIn(
            "the pipeline detaches each stage's worktree at the PR head, because "
            "the PR branch is usually checked out in another worktree already",
            self.instructions,
        )
        self.assertIn(
            "has to name the PR as a URL or `owner/repo#number`; the bare form "
            "belongs to an attached checkout alone.",
            self.instructions,
        )

    def test_the_current_rule_admits_a_detached_worktree_has_no_pull_request(self):
        """The rule still rightly forbids guessing a state file.

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
            "from a worktree attached to the PR's branch\"",
            self.instructions,
        )
        self.assertNotIn("omit to use the current branch's PR", self.instructions)


class AgentTaskCoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.repo_root = self.directory / "repo"
        self.repo_root.mkdir()
        self.copilot_home = self.directory / "copilot-home"
        environment_patch = mock.patch.dict(
            os.environ, {"COPILOT_HOME": str(self.copilot_home)}
        )
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        self.head = "1" * 40
        self.base = "2" * 40
        self.artifact = "3" * 40
        self.fix = "5" * 40
        self.validation = [
            {
                "command": "python -m unittest tests.test_feature",
                "outcome": "passed",
            }
        ]
        self.comment = {
            "id": 17,
            "source": "thread",
            "thread_id": "PRRT_thread",
            "review_id": 29,
            "url": "https://github.com/owner/repo/pull/7#discussion_r17",
            "author": "copilot-pull-request-reviewer[bot]",
            "author_bot_id": "BOT_1",
            "path": "src/app.py",
            "position": 4,
            "original_position": 4,
            "line": 7,
            "original_line": 7,
            "body": "Handle the empty value.",
            "status": "pending",
            "batch": None,
            "commit": None,
            "rationale": None,
            "summary": None,
            "reply_id": None,
            "resolved": False,
        }
        self.preflight = {
            "repository_root": str(self.repo_root),
            "identity": {"branch": "feature", "head": self.head, "status": ""},
            "pr": {
                "pr_node_id": "PR_7",
                "owner": "owner",
                "repo": "repo",
                "number": 7,
                "repo_name": "owner/repo",
                "pr_url": "https://github.com/owner/repo/pull/7",
                "url": "https://github.com/owner/repo/pull/7",
                "title": "Current title",
                "body": "Current body",
                "state": "OPEN",
                "is_draft": False,
                "upstream_owner": "owner",
                "upstream_repo": "repo",
                "head_owner": "owner",
                "head_repo": "repo",
                "head_repository": "owner/repo",
                "head_branch": "feature",
                "head_sha": self.head,
                "base_branch": "main",
                "base_sha": self.base,
                "cross_repository": False,
            },
            "viewer": {
                "login": "viewer",
                "repository_role": "write",
                "permissions": {
                    "admin": False,
                    "maintain": False,
                    "push": True,
                    "triage": True,
                    "pull": True,
                },
            },
            "comments": [self.comment],
            "comment_identities": [MODULE.comment_identity(self.comment)],
            "skipped_authors": [],
            "head_review_clean": False,
            "head_review_id": 29,
            "copilot_bot_id": "BOT_1",
        }
        self.source_fingerprint = {
            "branch": "feature",
            "head": self.head,
            "status": "",
            "refs": {"refs/heads/feature": self.head},
            "refs_sha256": MODULE.sha256_text(
                json.dumps(
                    {"refs/heads/feature": self.head},
                    separators=(",", ":"),
                    sort_keys=True,
                )
            ),
        }
        self.github_fingerprint = {
            "pr_sha256": "a" * 64,
            "threads_sha256": "b" * 64,
            "reviews_sha256": "c" * 64,
            "head_ref_sha": self.head,
            "base_ref_sha": self.base,
        }
        source_patch = mock.patch.object(
            MODULE,
            "local_source_fingerprint",
            return_value=self.source_fingerprint,
        )
        github_patch = mock.patch.object(
            MODULE,
            "github_decision_fingerprint",
            return_value=self.github_fingerprint,
        )
        source_patch.start()
        github_patch.start()
        self.addCleanup(source_patch.stop)
        self.addCleanup(github_patch.stop)
        hosted_patch = mock.patch.object(
            MODULE, "run_hosted_decision_worker", side_effect=self.hosted_worker_result,
        )
        self.hosted_worker = hosted_patch.start()
        self.addCleanup(hosted_patch.stop)

    def hosted_worker_result(self, **arguments):
        bundle = self.hosted_bundle(**arguments, session_id="hosted-session")
        result = self.result()
        result.update(
            schema=MODULE.CANDIDATE_AGENT_TASK_RESULT_SCHEMA,
            mode="code_candidate", candidate={}, completion={},
        )
        result["task"]["id"] = "hosted-task"
        bundle["result"] = result
        bundle["remote"]["task_id"] = "hosted-task"
        bundle["remote"]["requires_apply"] = True
        arguments["result_path"].write_text(json.dumps(result), encoding="utf-8")
        return bundle

    def source_for_preflight(self, preflight):
        identity = preflight["identity"]
        refs = {f"refs/heads/{identity['branch']}": identity["head"]}
        return {
            **identity,
            "refs": refs,
            "refs_sha256": MODULE.sha256_text(
                json.dumps(refs, separators=(",", ":"), sort_keys=True)
            ),
        }

    def hosted_bundle(self, **arguments):
        preflight = arguments["preflight"]
        run_id = arguments["run_id"]
        session_id = arguments["session_id"]
        decision_path = arguments["decision_path"]
        result_path = arguments["result_path"]
        canonical_path = arguments["canonical_path"]
        source = self.source_for_preflight(preflight)
        github = arguments["before_github"]
        decision = {
            "decisions": [
                {
                    "finding_id": MODULE.decision_finding_id(run_id, position),
                    "disposition": "no_change",
                    "reason": "The current implementation already handles this case.",
                    "proposed_reply": (
                        "No change is needed because the case is already handled."
                    ),
                }
                for position, _identity in enumerate(
                    preflight["comment_identities"]
                )
            ],
        }
        decision_content = json.dumps(decision, indent=2, sort_keys=True) + "\n"
        decision_path.write_text(decision_content, encoding="utf-8", newline="\n")
        remote = {
            "request_id": run_id,
            "task_id": session_id,
            "task_url": None,
            "generated_branch": source["branch"],
            "generated_head": source["head"],
            "commits": [],
            "final_local_head": source["head"],
            "requires_apply": False,
            "report_path": str(canonical_path),
            "report_sha256": "",
            "structural_attestation": True,
        }
        report = MODULE.validate_copilot_review_report(
            decision_content,
            request_id=run_id,
            preflight=preflight,
            remote=remote,
            paths_by_commit={},
            active_local_decisions=True,
        )
        canonical_content = MODULE.render_canonical_review_report(report)
        canonical_path.write_text(canonical_content, encoding="utf-8", newline="\n")
        remote["report_sha256"] = MODULE.sha256_text(canonical_content)
        return {
            "remote": remote,
            "report": report,
            "report_content": canonical_content,
            "paths_by_commit": {},
        }

    def result(self, commits=None):
        commits = [] if commits is None else commits
        return {
            "schema": MODULE.AGENT_TASK_RESULT_SCHEMA,
            "status": "success",
            "mode": "apply_with_report",
            "repository": {"name_with_owner": "owner/repo"},
            "pull_request": MODULE.expected_cloud_pull_request(self.preflight),
            "requested_model": "gpt-5.6-sol",
            "policy": {
                "id": "marketplace-agent-apply-report-worker",
                "version": 3,
                "sha256": MODULE.AGENT_TASK_POLICY_SHA256,
            },
            "task": {
                "id": "task-1",
                "url": "https://github.com/owner/repo/agent-tasks/task-1",
                "state": "completed",
                "base_ref": "feature",
                "base_sha": self.head,
            },
            "generated": {
                "branch": "copilot/agent-task",
                "head_sha": self.artifact,
                "commits": commits,
            },
            "application": {
                "status": "not_applied",
                "final_local_head": self.head,
            },
            "report": {
                "path": ".github/agent-task-reports/request-1.md",
                "commit": self.artifact,
                "sha256": "4" * 64,
            },
            "attestation": {
                "kind": "dispatcher_structural",
                "structural_complete": True,
            },
            "error": None,
        }

    def preflight_for_result(self, result):
        preflight = copy.deepcopy(self.preflight)
        pull_request = result["pull_request"]
        owner, repo = result["repository"]["name_with_owner"].split("/", 1)
        head_owner, head_repo = pull_request["head_repository"].split("/", 1)
        preflight["identity"].update(
            {
                "branch": pull_request["head_ref"],
                "head": pull_request["head_sha"],
            }
        )
        preflight["pr"].update(
            {
                "owner": owner,
                "repo": repo,
                "number": pull_request["number"],
                "repo_name": result["repository"]["name_with_owner"],
                "pr_url": pull_request["url"],
                "url": pull_request["url"],
                "upstream_owner": owner,
                "upstream_repo": repo,
                "head_owner": head_owner,
                "head_repo": head_repo,
                "head_repository": pull_request["head_repository"],
                "head_branch": pull_request["head_ref"],
                "head_sha": pull_request["head_sha"],
                "base_branch": pull_request["base_ref"],
                "base_sha": pull_request["base_sha"],
                "cross_repository": (
                    pull_request["head_repository"]
                    != pull_request["base_repository"]
                ),
            }
        )
        return preflight

    def task_creation_failure(self):
        result = self.result()
        result.update(
            {
                "status": "error",
                "task": {
                    "id": None,
                    "url": None,
                    "state": None,
                    "base_ref": None,
                    "base_sha": None,
                },
                "generated": {"branch": None, "head_sha": None, "commits": []},
                "application": {
                    "status": "not_applied",
                    "final_local_head": self.head,
                },
                "report": None,
                "attestation": {
                    "kind": "dispatcher_structural",
                    "structural_complete": False,
                },
                "error": {
                    "code": "api_failure",
                    "message": (
                        "start Agent Task failed with HTTP 409: user or repo does "
                        "not have CCA enabled; the request cannot be completed"
                    ),
                },
            }
        )
        return result

    def terminal_validation_failure(self):
        result = self.result()
        result["schema"] = MODULE.LEGACY_AGENT_TASK_RESULT_SCHEMA
        result["policy"] = MODULE.LEGACY_AGENT_TASK_POLICY_V5
        result.pop("attestation")
        result.update(
            {
                "status": "error",
                "application": {
                    "status": "not_applied",
                    "final_local_head": self.head,
                },
                "report": {**result["report"], "sha256": None},
                "worker_receipt": {
                    "path": ".github/agent-task-validations/request-1.json",
                    "commit": self.artifact,
                    "sha256": None,
                },
                "validation": {"complete": False, "outcomes": []},
                "error": {
                    "code": "validation_incomplete",
                    "message": "marketplace worker validation outcome is malformed",
                },
            }
        )
        return result

    def remote(self, commits=None):
        return MODULE.validate_success_result(
            self.result(commits),
            preflight=self.preflight,
            requested_model="gpt-5.6-sol",
        )

    def receipt(self):
        return json.dumps(self.validation)

    def report(self, commits=None):
        commits = [] if commits is None else commits
        identity = self.preflight["comment_identities"][0]
        fixed = bool(commits)
        return json.dumps(
            {
                "schema": MODULE.POSITIONAL_COPILOT_REVIEW_REPORT_SCHEMA,
                "request_id": "request-1",
                "repository": "owner/repo",
                "pull_request": {
                    "number": 7,
                    "head_sha": self.head,
                    "base_sha": self.base,
                    "title_sha256": MODULE.sha256_text("Current title"),
                    "body_sha256": MODULE.sha256_text("Current body"),
                },
                "outcome": "addressed" if fixed else "no_changes",
                "comments": [
                    {
                        **identity,
                        "disposition": "fixed" if fixed else "no_change",
                        "reason": "The focused test confirms the correct behavior.",
                        "commit": commits[0] if fixed else None,
                        "reply": "Fixed and covered by the focused test."
                        if fixed
                        else "The current behavior already handles this case.",
                        "changed_paths": ["src/app.py"] if fixed else [],
                    }
                ],
            }
        )

    def local_decision(self, *, contract_id=None):
        return {
            "decisions": [
                {
                    "finding_id": contract_id
                    if contract_id is not None
                    else MODULE.decision_finding_id("run-1", 0),
                    "disposition": "no_change",
                    "reason": "The current implementation already handles this case.",
                    "proposed_reply": (
                        "No change is needed because the case is already handled."
                    ),
                }
            ],
        }

    def run_actual_local_worker(
        self,
        writer,
        *,
        requested_model="gpt-5.6-sol",
        returncode=0,
        stdout="",
        stderr="",
    ):
        prompt_path = self.directory / "prompt.txt"
        decision_path = self.directory / "decisions.json"
        result_path = self.directory / "result.json"
        canonical_path = self.directory / "canonical.json"
        prompt_path.write_text("pinned prompt\n", encoding="utf-8", newline="\n")
        copilot_home = self.directory / "copilot-home"

        def run(command, **kwargs):
            if writer is not None:
                writer(
                    prompt_path=prompt_path,
                    decision_path=decision_path,
                    command=command,
                    kwargs=kwargs,
                )
            events_path = (
                copilot_home
                / "session-state"
                / command[command.index("--session-id") + 1]
                / "events.jsonl"
            )
            events_path.parent.mkdir(parents=True, exist_ok=True)
            model = command[command.index("--model") + 1]
            events = [
                {
                    "type": "session.start",
                    "data": {
                        "sessionId": "local-session",
                        "selectedModel": model,
                        "reasoningEffort": "high",
                    },
                },
                {
                    "type": "assistant.message",
                    "data": {"model": model, "content": "complete"},
                },
            ]
            events_path.write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
                newline="\n",
            )
            return MODULE.subprocess.CompletedProcess(
                command, returncode, stdout, stderr
            )

        with (
            mock.patch.dict(os.environ, {"COPILOT_HOME": str(copilot_home)}),
            mock.patch.object(
                MODULE, "run_owned_local_worker", side_effect=run
            ) as runner,
        ):
            bundle = RUN_LOCAL_DECISION_WORKER(
                repo_root=self.repo_root,
                target=MODULE.parse_target("owner/repo#7"),
                preflight=self.preflight,
                prompt_path=prompt_path,
                decision_path=decision_path,
                result_path=result_path,
                canonical_path=canonical_path,
                run_id="run-1",
                session_id="local-session",
                requested_model=requested_model,
                before_source=self.source_fingerprint,
                before_github=self.github_fingerprint,
            )
        return bundle, runner, {
            "prompt": prompt_path,
            "decision": decision_path,
            "result": result_path,
            "canonical": canonical_path,
            "copilot_home": copilot_home,
        }

    def test_owned_local_worker_timeout_terminates_and_reaps(self):
        process = mock.Mock()
        process.pid = 42
        process.returncode = 1
        process.poll.return_value = 1
        process.communicate.side_effect = [
            MODULE.subprocess.TimeoutExpired(["copilot"], 0.01),
            ("", ""),
        ]
        owner = mock.Mock()
        with (
            mock.patch.object(
                MODULE,
                "popen_owned_local_worker",
                return_value=(process, owner),
            ),
            self.assertRaisesRegex(
                MODULE.WorkflowError,
                "timed out after 0.01 seconds",
            ),
        ):
            MODULE.run_owned_local_worker(
                ["copilot"],
                cwd=self.repo_root,
                input_text="prompt",
                timeout=0.01,
            )

        owner.terminate.assert_called_once()
        process.wait.assert_called_once_with(
            timeout=MODULE.LOCAL_DECISION_TERMINATION_TIMEOUT_SECONDS
        )
        owner.close.assert_called_once()


    def write_valid_local_decision(self, *, decision_path, **_kwargs):
        decision_path.write_text(
            json.dumps(self.local_decision(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )















    def test_source_fingerprint_matches_recovery_probe_shape(self):
        shared_refs = {
            "refs/heads/feature": self.head,
            "refs/copilot/workspace-diffs/other-session/head": "2" * 40,
            "refs/heads/other-session": "3" * 40,
            "refs/prefetch/remotes/origin/other-session": "4" * 40,
        }

        def git(_root, *arguments):
            if arguments == ("branch", "--show-current"):
                return "feature"
            if arguments == ("rev-parse", "HEAD"):
                return self.head
            if arguments == ("status", "--porcelain=v1"):
                return ""
            if arguments == (
                "for-each-ref",
                "--format=%(refname)%09%(objectname)",
            ):
                return "\n".join(
                    f"{name}\t{sha}" for name, sha in shared_refs.items()
                )
            if arguments == (
                "rev-parse",
                "--verify",
                "refs/heads/feature",
            ):
                return self.head
            raise AssertionError(arguments)

        with mock.patch.object(MODULE, "git", side_effect=git):
            stored = LOCAL_SOURCE_FINGERPRINT(self.repo_root)
        projected_probe = {
            key: stored[key]
            for key in ("branch", "head", "status", "refs_sha256")
        }

        self.assertEqual(projected_probe, stored)

    def test_source_transition_derives_parent_paths_and_exact_patch_digest(self):
        before = {
            **self.source_fingerprint,
            "worktree": str(self.repo_root.resolve()),
        }
        after = {
            **before,
            "head": self.fix,
            "refs": {"refs/heads/feature": self.fix},
        }
        patch = b"diff --git a/src/app.py b/src/app.py\n+fixed\n"

        def git(_root, *arguments):
            if arguments == (
                "rev-list",
                "--reverse",
                f"{self.head}..{self.fix}",
            ):
                return self.fix
            if arguments == ("rev-list", "--parents", "-n", "1", self.fix):
                return f"{self.fix} {self.head}"
            if arguments == ("show", "-s", "--format=%B", self.fix):
                return "Fix finding"
            raise AssertionError(arguments)

        with (
            mock.patch.object(MODULE, "git", side_effect=git),
            mock.patch.object(
                MODULE, "git_z_paths", return_value=["src/app.py"]
            ),
            mock.patch.object(
                MODULE,
                "run_bytes",
                return_value=MODULE.subprocess.CompletedProcess(
                    ["git"], 0, patch, b""
                ),
            ),
        ):
            evidence = MODULE.local_source_transition_evidence(
                self.repo_root,
                before=before,
                after=after,
            )

        self.assertEqual(
            [
                {
                    "sha": self.fix,
                    "parent": self.head,
                    "paths": ["src/app.py"],
                    "patch_sha256": hashlib.sha256(patch).hexdigest(),
                }
            ],
            evidence,
        )

    def test_source_transition_rejects_worktree_drift(self):
        before = {
            **self.source_fingerprint,
            "worktree": str(self.repo_root.resolve()),
        }
        after = {
            **before,
            "worktree": str((self.directory / "other-worktree").resolve()),
        }

        with self.assertRaisesRegex(
            MODULE.WorkflowError,
            "changed the branch or working tree",
        ):
            MODULE.local_source_transition_evidence(
                self.repo_root,
                before=before,
                after=after,
            )







    def test_agent_definition_is_thin_and_version_is_bumped(self):
        instructions = AGENT.read_text(encoding="utf-8")
        self.assertIn("agent-task <target>", instructions)
        self.assertIn("model: gpt-5.6-sol", instructions)
        self.assertIn("Do not pass a model argument", instructions)
        self.assertIn("Use the verified `session_title`", instructions)
        self.assertNotIn("--execution-handle", instructions)
        self.assertNotIn("--pipeline-run", instructions)
        self.assertNotIn("tools: [read", instructions)
        self.assertNotIn("tools: [edit", instructions)
        self.assertEqual(json.loads(PLUGIN.read_text())["version"], "1.1.78")
        self.assertEqual(3, MODULE.LOCAL_DECISION_RESULT_SCHEMA["version"])
        self.assertEqual(2, MODULE.DECISION_COPILOT_REVIEW_REPORT_SCHEMA["version"])
        self.assertEqual(
            "marketplace-local-review-decision-worker@3",
            MODULE.LOCAL_DECISION_POLICY,
        )

    def test_successful_retained_preparation_clears_prior_failure(self):
        task = {
            "status": "validated_pending_import",
            "error": "stale validation failure",
            "failed_at": "2026-09-16T17:45:33Z",
            "recovery_files": ["prompt.txt", "result.json"],
            "result_file": "result.json",
        }

        MODULE.clear_agent_task_failure(task)

        self.assertEqual(
            {
                "status": "validated_pending_import",
                "result_file": "result.json",
            },
            task,
        )

    def test_report_parser_accepts_markdown_with_one_json_payload(self):
        content = "# Result\n\nReadable summary.\n\n```json\n{\"ok\":true}\n```"
        self.assertEqual(
            {"ok": True},
            MODULE.parse_markdown_report(content, description="test report"),
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "exactly one"):
            MODULE.parse_markdown_report("# Result", description="test report")

    def test_prompt_is_self_contained_versioned_and_treats_inputs_as_untrusted(self):
        prompt = MODULE.build_worker_prompt(
            self.preflight,
            request_id="request-1",
            iteration_allowance=1,
            prior_history=[],
        )
        self.assertIn("decision object as UTF-8 JSON", prompt)
        self.assertIn("hosted worker prompt version 11", prompt)
        self.assertIn("decision-report schema version 3", prompt)
        self.assertNotIn("commit_index", prompt)
        self.assertIn("attaches the complete verified candidate history", prompt)
        self.assertIn("validation in this hosted task", prompt)
        self.assertIn("never runs repository programs or candidate validation commands", prompt)
        self.assertIn("opaque coordinator-generated values", prompt)
        self.assertIn("joins each decision", prompt)
        self.assertIn("negative ID is intentional", prompt)
        self.assertIn('"iteration_allowance": 1', prompt)
        self.assertIn("Do not sleep, poll, watch", prompt)
        self.assertIn('"thread_id": "PRRT_thread"', prompt)
        self.assertIn(
            MODULE.decision_finding_id("request-1", 0),
            prompt,
        )
        self.assertNotIn('"contract_id"', prompt)
        self.assertNotIn('"finding_key"', prompt)
        self.assertIn(
            "Do not put commit indexes, commit SHAs, parents, changed paths",
            prompt,
        )
        self.assertIn("untrusted data", prompt)
        self.assertIn("create an Agent Task", prompt)
        self.assertIn(f"`{MODULE.HOSTED_DECISION_PATH}`", prompt)
        self.assertNotIn("{{LOCAL_DECISION_PATH}}", prompt)
        self.assertNotIn("MARKETPLACE_VALIDATION_PATH", prompt)
        MODULE.require_no_credentials(prompt, source="prompt")

    def test_prompt_bounds_prior_history_without_dropping_current_findings(self):
        history = [
            {"iteration": index, "detail": "x" * 2_000}
            for index in range(MODULE.MAX_PROMPT_HISTORY_ENTRIES + 10)
        ]
        original = copy.deepcopy(history)
        prompt = MODULE.build_worker_prompt(
            self.preflight,
            request_id="request-1",
            iteration_allowance=1,
            prior_history=history,
        )
        marker = MODULE.decision_finding_id("request-1", 0)
        self.assertIn(marker, prompt)
        self.assertIn(
            f'"total_entries": {len(history)}',
            prompt,
        )
        self.assertNotIn('"iteration": 0', prompt)
        self.assertIn(
            f'"iteration": {len(history) - 1}',
            prompt,
        )
        self.assertEqual(original, history)

    def test_agent_task_model_gate_is_internal_and_rejects_caller_flags(self):
        parser = MODULE.build_parser()
        args = parser.parse_args(["agent-task"])

        self.assertEqual(args.model, "sol")
        rejected = (
            ("--model", "sol"),
            ("--model", "astra"),
            ("--repo-root", "repo"),
            ("--state", "state.json"),
            ("--pipeline-run", "run"),
            ("--execution-handle", "handle.json"),
            ("--watch-interval", "1"),
            ("--invocation-run", "run"),
        )
        for flag, value in rejected:
            with (
                self.subTest(flag=flag),
                mock.patch.object(sys, "stderr"),
                self.assertRaises(SystemExit),
            ):
                parser.parse_args(["agent-task", flag, value])

    def test_execution_runtime_uses_the_current_agent_session(self):
        runtime = mock.Mock()
        runtime.entrypoint.return_value = 17
        with (
            mock.patch.object(sys, "argv", ["helper", "execution-status"]),
            mock.patch.dict(
                os.environ,
                {"COPILOT_AGENT_SESSION_ID": "session-1"},
                clear=False,
            ),
            mock.patch.object(MODULE, "_load_execution", return_value=runtime),
        ):
            self.assertEqual(17, MODULE.execution_main())

        runtime.entrypoint.assert_called_once_with(
            MODULE.main, MODULE.__dict__, commands=("agent-task", "pipeline")
        )
        with (
            mock.patch.object(
                sys,
                "argv",
                ["helper", "agent-task", "7", "--model", "sol"],
            ),
            mock.patch.object(MODULE, "main", return_value=23),
            mock.patch.object(MODULE, "_load_execution") as load_execution,
        ):
            self.assertEqual(23, MODULE.execution_main())
        load_execution.assert_not_called()

    def test_local_coordinator_waits_for_stable_actionable_feedback(self):
        state_path = self.directory / "stable-state.json"
        arguments = self.arguments(state_path)
        arguments.stability_polls = 2
        arguments.debounce_seconds = 0
        arguments.wait_timeout = 10
        arguments.interval = 0
        arguments.max_interval = 0
        arguments.poll_jitter = 0
        with (
            mock.patch.object(
                MODULE,
                "agent_task_preflight",
                side_effect=[self.preflight, self.preflight],
            ) as preflight,
            mock.patch.object(MODULE.time, "sleep"),
        ):
            result = MODULE.wait_for_stable_review_preflight(
                arguments,
                repo_root=self.repo_root,
                target=MODULE.parse_target("owner/repo#7"),
                state_path=state_path,
            )

        self.assertEqual(
            MODULE.review_snapshot_sha256(result),
            MODULE.review_snapshot_sha256(self.preflight),
        )
        self.assertEqual(preflight.call_count, 2)
        self.assertEqual(
            MODULE.load_state(state_path)["coordinator"]["status"],
            "ready",
        )

    def test_local_coordinator_does_not_redispatch_a_consumed_feedback_snapshot(self):
        state_path = self.directory / "deduplicated-state.json"
        consumed_id = MODULE.review_snapshot_sha256(self.preflight)
        fresh = copy.deepcopy(self.preflight)
        fresh["head_review_id"] = 9002
        fresh["comments"][0]["id"] = 18
        fresh["comment_identities"] = [
            MODULE.comment_identity(fresh["comments"][0])
        ]
        MODULE.save_state(
            state_path,
            {
                "version": MODULE.STATE_VERSION,
                "coordinator": {
                    "processed_snapshots": [
                        {
                            "snapshot_sha256": consumed_id,
                            "head_sha": self.head,
                            "task_id": "task-old",
                        }
                    ]
                },
            },
        )
        arguments = self.arguments(state_path)
        arguments.stability_polls = 2
        arguments.debounce_seconds = 0
        arguments.wait_timeout = 10
        arguments.interval = 0
        arguments.max_interval = 0
        arguments.poll_jitter = 0
        with (
            mock.patch.object(
                MODULE,
                "agent_task_preflight",
                side_effect=[self.preflight, self.preflight, fresh, fresh],
            ) as preflight,
            mock.patch.object(MODULE.time, "sleep"),
        ):
            result = MODULE.wait_for_stable_review_preflight(
                arguments,
                repo_root=self.repo_root,
                target=MODULE.parse_target("owner/repo#7"),
                state_path=state_path,
            )

        self.assertEqual(result["comment_identities"][0]["id"], 18)
        self.assertEqual(preflight.call_count, 4)

    def test_restart_resumes_a_timed_out_review_request_without_a_new_task(self):
        state_path = self.directory / "requested-state.json"
        MODULE.save_state(
            state_path,
            {
                "version": MODULE.STATE_VERSION,
                "created_at": MODULE.utc_now(),
                "iterations": 1,
                "history": [],
                "monitoring": {
                    "status": "requested",
                    "head_sha": self.head,
                    "baseline_review_id": 10,
                    "result": {"result": "timeout"},
                },
            },
        )
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=self.repo_root),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value=MODULE.parse_target("owner/repo#7"),
            ),
            mock.patch.object(
                MODULE, "continue_after_review_request"
            ) as continuation,
            mock.patch.object(MODULE, "agent_task_preflight") as preflight,
            mock.patch.object(MODULE, "discover_cloud_task") as discover,
        ):
            arguments = self.arguments(state_path)
            arguments.request_review_only = True
            MODULE.command_agent_task(arguments)

        continuation.assert_called_once()
        self.assertTrue(continuation.call_args.args[0].request_review_only)
        preflight.assert_not_called()
        discover.assert_not_called()

    def test_validates_success_and_no_op_receipts(self):
        remote = self.remote()
        report = MODULE.validate_copilot_review_report(
            self.report(),
            request_id="request-1",
            preflight=self.preflight,
            remote=remote,
            paths_by_commit={},
        )
        self.assertEqual(report["outcome"], "no_changes")

    def test_finding_key_covers_every_full_identity_field(self):
        identity = self.preflight["comment_identities"][0]
        original = MODULE.decision_finding_key(identity)

        for field, value in identity.items():
            changed = copy.deepcopy(identity)
            if value is None:
                changed[field] = "present"
            elif isinstance(value, int):
                changed[field] = value + 1
            else:
                changed[field] = f"{value}-changed"
            with self.subTest(field=field):
                self.assertNotEqual(
                    original,
                    MODULE.decision_finding_key(changed),
                )

    def test_decision_list_restores_suppressed_identities_by_opaque_id(self):
        preflight = copy.deepcopy(self.preflight)
        preflight["pr"].update(
            {
                "number": 383,
                "pr_url": "https://github.com/open-telemetry/shared-workflows/pull/383",
                "repo_name": "open-telemetry/shared-workflows",
                "head_repository": "open-telemetry/shared-workflows",
                "head_branch": "trask-lock-free-dashboard-publisher",
                "head_sha": "19852bf67b646585381f3ea5cfe798ca46b8bc0f",
                "base_branch": "main",
                "base_sha": "55fb421179d32aef3b36c7f6503f57193561d14c",
            }
        )
        identities = [
            {
                "id": comment_id,
                "source": "suppressed",
                "thread_id": None,
                "review_id": 5225525187,
                "url": (
                    "https://github.com/open-telemetry/shared-workflows/"
                    "pull/383#pullrequestreview-5225525187"
                ),
                "path": ".github/scripts/pull-request-dashboard/state_branch.py",
                "line": line,
                "original_line": line,
                "body_sha256": digest,
                "author": "copilot-pull-request-reviewer[bot]",
            }
            for comment_id, line, digest in (
                (
                    -5225525187000,
                    242,
                    "ef35af361d8d647e848018190cc9748b0c0d2942b80da5fd86dc5a175c87db00",
                ),
                (
                    -5225525187001,
                    254,
                    "9518a33cbf1c7ea443c9126306fe5a91a6727f325305cf4fe33073ef445ab24c",
                ),
            )
        ]
        preflight["comment_identities"] = identities
        finding_ids = [
            MODULE.decision_finding_id("request-383", position)
            for position in range(len(identities))
        ]
        payload = {
            "decisions": [
                {
                    "finding_id": finding_id,
                    "disposition": "no_change",
                    "reason": f"Verified finding {index} against the pinned source.",
                    "proposed_reply": f"The pinned source handles finding {index}.",
                }
                for index, finding_id in reversed(
                    list(enumerate(finding_ids))
                )
            ],
        }

        report = MODULE.validate_copilot_review_report(
            json.dumps(payload),
            request_id="request-383",
            preflight=preflight,
            remote={"commits": [], "requires_apply": False},
            paths_by_commit={},
            active_local_decisions=True,
        )

        self.assertEqual(MODULE.COPILOT_REVIEW_REPORT_SCHEMA, report["schema"])
        self.assertEqual(
            identities,
            [
                {key: item[key] for key in identity}
                for item, identity in zip(report["comments"], identities)
            ],
        )
        self.assertEqual(
            ["The pinned source handles finding 0.", "The pinned source handles finding 1."],
            [item["reply"] for item in report["comments"]],
        )

    def test_decision_list_blocks_stale_unknown_duplicate_and_unreviewed_ids(self):
        finding_id = MODULE.decision_finding_id("request-1", 0)
        decision = {
            "finding_id": finding_id,
            "disposition": "no_change",
            "reason": "Verified against the pinned source.",
            "proposed_reply": "The pinned source already handles this finding.",
        }
        payload = {"decisions": [decision]}

        for value, message in (
            ([], "stale or incomplete"),
            (
                [{**decision, "finding_id": MODULE.decision_finding_id("old-run", 0)}],
                "unknown, duplicate, or unreviewed",
            ),
            ([decision, decision], "stale or incomplete"),
        ):
            malformed = copy.deepcopy(payload)
            malformed["decisions"] = value
            with self.subTest(value=value):
                with self.assertRaisesRegex(MODULE.WorkflowError, message):
                    MODULE.validate_copilot_review_report(
                        json.dumps(malformed),
                        request_id="request-1",
                        preflight=self.preflight,
                        remote={"commits": [], "requires_apply": False},
                        paths_by_commit={},
                        active_local_decisions=True,
                    )
        two_findings = copy.deepcopy(self.preflight)
        second_identity = copy.deepcopy(
            two_findings["comment_identities"][0]
        )
        second_identity["id"] = 18
        second_identity["thread_id"] = "PRRT_second"
        two_findings["comment_identities"].append(second_identity)
        with self.assertRaisesRegex(MODULE.WorkflowError, "malformed decision"):
            MODULE.validate_copilot_review_report(
                json.dumps({"decisions": [decision, decision]}),
                request_id="request-1",
                preflight=two_findings,
                remote={"commits": [], "requires_apply": False},
                paths_by_commit={},
                active_local_decisions=True,
            )

    def test_active_decision_list_rejects_legacy_identity_and_evidence_fields(self):
        identity = self.preflight["comment_identities"][0]
        legacy = {
            "schema": MODULE.LEGACY_DECISION_COPILOT_REVIEW_REPORT_SCHEMA,
            "contract_id": MODULE.decision_report_contract(self.preflight),
            "decisions": [
                {
                    "finding_key": MODULE.decision_finding_key(identity),
                    "disposition": "no_change",
                    "reason": "No change is needed.",
                    "commit": None,
                    "reply": "No change is needed.",
                    "changed_paths": [],
                }
            ],
        }
        with self.assertRaisesRegex(MODULE.WorkflowError, "legacy"):
            MODULE.validate_copilot_review_report(
                json.dumps(legacy),
                request_id="request-1",
                preflight=self.preflight,
                remote={"commits": [], "requires_apply": False},
                paths_by_commit={},
                active_local_decisions=True,
            )

        fixed = {
            "finding_id": MODULE.decision_finding_id("request-1", 0),
            "disposition": "fixed",
        }
        for field, value in (
            ("commit", self.fix),
            ("changed_paths", ["src/app.py"]),
            ("repository", "owner/repo"),
            ("pull_request", 7),
            ("head_sha", self.head),
            ("finding_fingerprint", "a" * 64),
            ("validation_complete", True),
            ("session_id", "local-session"),
            ("github_outcome", "replied"),
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(MODULE.WorkflowError, "malformed decision"),
            ):
                MODULE.validate_copilot_review_report(
                    json.dumps({"decisions": [{**fixed, field: value}]}),
                    request_id="request-1",
                    preflight=self.preflight,
                    remote={"commits": [self.fix], "requires_apply": False},
                    paths_by_commit={self.fix: ["src/app.py"]},
                    active_local_decisions=True,
                )

    def test_decision_list_uses_coordinator_owned_commit_and_paths(self):
        payload = {
            "decisions": [
                {
                    "finding_id": MODULE.decision_finding_id("request-1", 0),
                    "disposition": "fixed",
                }
            ],
        }
        remote = self.remote([self.fix])

        report = MODULE.validate_copilot_review_report(
            json.dumps(payload),
            request_id="request-1",
            preflight=self.preflight,
            remote=remote,
            paths_by_commit={self.fix: ["src/app.py"]},
            active_local_decisions=True,
        )
        self.assertEqual(self.fix, report["comments"][0]["commit"])
        self.assertEqual(["src/app.py"], report["comments"][0]["changed_paths"])

        payload["decisions"][0]["commit"] = self.fix
        with self.assertRaisesRegex(MODULE.WorkflowError, "malformed decision"):
            MODULE.validate_copilot_review_report(
                json.dumps(payload),
                request_id="request-1",
                preflight=self.preflight,
                remote=remote,
                paths_by_commit={self.fix: ["src/app.py"]},
                active_local_decisions=True,
            )

        payload["decisions"][0] = {
            "finding_id": MODULE.decision_finding_id("request-1", 0),
            "disposition": "fixed",
        }
        second_fix = "6" * 40
        with self.assertRaisesRegex(MODULE.WorkflowError, "at most one"):
            MODULE.validate_copilot_review_report(
                json.dumps(payload),
                request_id="request-1",
                preflight=self.preflight,
                remote={
                    "commits": [self.fix, second_fix],
                    "requires_apply": False,
                },
                paths_by_commit={
                    self.fix: ["src/app.py"],
                    second_fix: ["src/other.py"],
                },
                active_local_decisions=True,
            )

    def test_both_exact_383_malformed_reports_fail_closed(self):
        preflight = copy.deepcopy(self.preflight)
        preflight["identity"] = {
            "branch": "trask-lock-free-dashboard-publisher",
            "head": "19852bf67b646585381f3ea5cfe798ca46b8bc0f",
            "status": "",
        }
        preflight["pr"].update(
            {
                "number": 383,
                "pr_url": "https://github.com/open-telemetry/shared-workflows/pull/383",
                "repo_name": "open-telemetry/shared-workflows",
                "head_repository": "open-telemetry/shared-workflows",
                "head_branch": "trask-lock-free-dashboard-publisher",
                "head_sha": "19852bf67b646585381f3ea5cfe798ca46b8bc0f",
                "base_branch": "main",
                "base_sha": "55fb421179d32aef3b36c7f6503f57193561d14c",
            }
        )
        preflight["comment_identities"] = [
            {
                "id": comment_id,
                "source": "suppressed",
                "thread_id": None,
                "review_id": 5225525187,
                "url": (
                    "https://github.com/open-telemetry/shared-workflows/"
                    "pull/383#pullrequestreview-5225525187"
                ),
                "path": ".github/scripts/pull-request-dashboard/state_branch.py",
                "line": line,
                "original_line": line,
                "body_sha256": digest,
                "author": "copilot-pull-request-reviewer[bot]",
            }
            for comment_id, line, digest in (
                (
                    -5225525187000,
                    242,
                    "ef35af361d8d647e848018190cc9748b0c0d2942b80da5fd86dc5a175c87db00",
                ),
                (
                    -5225525187001,
                    254,
                    "9518a33cbf1c7ea443c9126306fe5a91a6727f325305cf4fe33073ef445ab24c",
                ),
            )
        ]

        for result_path, report_path in (
            (
                SUPPRESSED_COLLAPSED_383_RESULT,
                SUPPRESSED_COLLAPSED_383_REPORT,
            ),
            (
                SUPPRESSED_COLLAPSED_383_SECOND_RESULT,
                SUPPRESSED_COLLAPSED_383_SECOND_REPORT,
            ),
        ):
            result = MODULE.load_agent_task_result(result_path)
            remote = MODULE.validate_success_result(
                result,
                preflight=preflight,
                requested_model="gpt-5.6-sol",
            )
            content = report_path.read_text(encoding="utf-8")
            self.assertEqual(result["report"]["sha256"], MODULE.sha256_text(content))
            with self.subTest(task_id=result["task"]["id"]):
                with self.assertRaises(MODULE.WorkflowError):
                    MODULE.validate_copilot_review_report(
                        content,
                        request_id=remote["request_id"],
                        preflight=preflight,
                        remote=remote,
                        paths_by_commit={},
                    )

    def test_validates_fix_paths_commits_and_order(self):
        remote = self.remote([self.fix])
        report = MODULE.validate_copilot_review_report(
            self.report([self.fix]),
            request_id="request-1",
            preflight=self.preflight,
            remote=remote,
            paths_by_commit={self.fix: ["src/app.py"]},
        )
        self.assertEqual(remote["commits"], [self.fix])
        self.assertNotIn("fix_commits", report)
        with self.assertRaisesRegex(MODULE.WorkflowError, "unexpected paths"):
            MODULE.validate_copilot_review_report(
                self.report([self.fix]),
                request_id="request-1",
                preflight=self.preflight,
                remote=remote,
                paths_by_commit={self.fix: ["src/other.py"]},
            )

    def test_exact_compact_v3_report_recovers_omitted_original_line(self):
        commit = "7f1f402d9f7d5a925367dea4a1e00ad446e9d9a3"
        preflight = copy.deepcopy(self.preflight)
        preflight["pr"].update(
            {
                "number": 377,
                "repo_name": "open-telemetry/shared-workflows",
                "head_sha": "c546c4902433040a05262cb22fa5587ae829de62",
                "base_sha": "ad5b9918d6eca8cc999d7034757aee727b2631ea",
            }
        )
        preflight["comment_identities"] = [
            {
                "body_sha256": "279da450bebd45cee558de6794a34c47824b222787ea592ce85c3fc7db275d42",
                "id": 4023137951,
                "line": 466,
                "original_line": 466,
                "path": ".github/scripts/github-actions-queue/collect.py",
                "review_id": 5219253546,
                "source": "thread",
                "thread_id": "PRRT_kwDOTENyc86iz-Sk",
                "url": "https://github.com/open-telemetry/shared-workflows/pull/377#discussion_r4023137951",
            }
        ]
        content = COMPACT_V3_REPORT.read_text(encoding="utf-8")
        self.assertEqual(
            MODULE.sha256_text(content),
            "8da1ae73ca152e8480649f17d1b28225bdf13a100b70caac98895e0894b6f42c",
        )

        report = MODULE.validate_copilot_review_report(
            content,
            request_id="c7393d4d-bfd3-482a-87ac-baf3794c2c3a",
            preflight=preflight,
            remote={"commits": [commit], "requires_apply": True},
            paths_by_commit={
                commit: [
                    ".github/scripts/github-actions-queue/collect.py",
                    ".github/scripts/github-actions-queue/test_collect.py",
                ]
            },
        )

        self.assertEqual(
            report["comments"][0]["original_line"],
            466,
        )
        self.assertEqual(report["comments"][0]["commit"], commit)

    def test_exact_forward_compact_v3_report_recovers_equal_original_line(self):
        commit = "2d88ec12d35da8d0db74f695471daf29c22f4b68"
        preflight = copy.deepcopy(self.preflight)
        preflight["pr"].update(
            {
                "number": 377,
                "repo_name": "open-telemetry/shared-workflows",
                "head_sha": "4076ad1e7b825752d99231ba1634ad0067c6d83b",
                "base_sha": "ad5b9918d6eca8cc999d7034757aee727b2631ea",
            }
        )
        preflight["comment_identities"] = [
            {
                "body_sha256": (
                    "a697bd0ef3e4293f41aa4b542c0867fa038323b2fd96cf6094ff6c8fef670ab1"
                ),
                "id": 4024467893,
                "line": 115,
                "original_line": 115,
                "path": ".github/workflows/github-actions-queue-collector.yml",
                "review_id": 5220865496,
                "source": "thread",
                "thread_id": "PRRT_kwDOTENyc86i3W2z",
                "url": (
                    "https://github.com/open-telemetry/shared-workflows/"
                    "pull/377#discussion_r4024467893"
                ),
            }
        ]
        content = FORWARD_COMPACT_V3_REPORT.read_text(encoding="utf-8")
        self.assertEqual(
            "0e4a679644537c8f4fbf3a582c78a1964e2eb955fd60ac7c1c49fd2115521506",
            MODULE.sha256_text(content),
        )

        report = MODULE.validate_copilot_review_report(
            content,
            request_id="1c52aa75-cb58-415d-8a47-c0392a8f1cf2",
            preflight=preflight,
            remote={"commits": [commit], "requires_apply": True},
            paths_by_commit={
                commit: [".github/workflows/github-actions-queue-collector.yml"]
            },
        )

        self.assertEqual(115, report["comments"][0]["original_line"])
        self.assertEqual(commit, report["comments"][0]["commit"])

    def test_exact_347_forward_repository_report_recovers_thread_ids_and_followup(self):
        primary = "773083f6dc7e86684107ae5adba4b3cb0a2aa22b"
        followup = "8c15ae92f010174cc4b0877582dc3e889396550d"
        content = FORWARD_REPOSITORY_347_REPORT.read_text(encoding="utf-8")
        payload = MODULE.parse_markdown_report(content, description="test report")
        ids = {
            "PRRT_kwDOTENyc86io4cl": 4018692884,
            "PRRT_kwDOTENyc86io4c6": 4018692920,
            "PRRT_kwDOTENyc86io4dJ": 4018692943,
            "PRRT_kwDOTENyc86io4dV": 4018692968,
        }
        preflight = copy.deepcopy(self.preflight)
        preflight["pr"].update(
            {
                "number": 347,
                "repo_name": "open-telemetry/shared-workflows",
                "pr_url": "https://github.com/open-telemetry/shared-workflows/pull/347",
                "url": "https://github.com/open-telemetry/shared-workflows/pull/347",
                "head_owner": "open-telemetry",
                "head_repo": "shared-workflows",
                "head_repository": "open-telemetry/shared-workflows",
                "head_branch": "trask-fix-dashboard-publisher-contention",
                "base_branch": "main",
                "head_sha": "14cf2a9a1ee281423501ec0a1b69e9236c5a3816",
                "base_sha": "ad5b9918d6eca8cc999d7034757aee727b2631ea",
            }
        )
        preflight["comment_identities"] = [
            {
                "id": ids[item["thread_id"]],
                "author": item["author"],
                "body_sha256": item["body_sha256"],
                "line": item["current_line"],
                "original_line": item["original_line"],
                "path": item["path"],
                "review_id": item["review_id"],
                "side": item["diff_side"],
                "source": item["source"],
                "thread_id": item["thread_id"],
                "url": item["url"],
            }
            for item in payload["comments"]
        ]
        paths_by_commit = {
            primary: [
                ".github/scripts/pull-request-dashboard/netlify/lib/dashboard-queue.mjs",
                ".github/scripts/pull-request-dashboard/process_queue_batch.py",
                ".github/scripts/pull-request-dashboard/state_branch.py",
                ".github/scripts/pull-request-dashboard/test_dashboard_queue.mjs",
                ".github/scripts/pull-request-dashboard/test_process_queue_batch.py",
                ".github/scripts/pull-request-dashboard/test_state_branch.py",
            ],
            followup: [
                ".github/scripts/pull-request-dashboard/test_dashboard_queue.mjs"
            ],
        }
        remote = MODULE.validate_success_result(
            MODULE.load_agent_task_result(FORWARD_REPOSITORY_347_RESULT),
            preflight=preflight,
            requested_model="gpt-5.6-sol",
        )
        self.assertEqual([primary, followup], remote["commits"])
        self.assertTrue(remote["requires_apply"])
        self.assertEqual(
            "4aa7fa8bf3e84374af5051d448104001481f9078caf87421c8bc7b6ed56b8833",
            MODULE.sha256_text(content),
        )

        report = MODULE.validate_copilot_review_report(
            content,
            request_id="da11f083-e3a0-405c-ba50-7acfb1e51091",
            preflight=preflight,
            remote=remote,
            paths_by_commit=paths_by_commit,
        )

        self.assertEqual(list(ids.values()), [item["id"] for item in report["comments"]])
        self.assertEqual(
            [primary] * 4,
            [item["commit"] for item in report["comments"]],
        )
        malformed = copy.deepcopy(payload)
        malformed["pull_request"]["head_sha"] = "9" * 40
        bad_position = copy.deepcopy(payload)
        bad_position["comments"][0]["current_line"] += 1
        missing_field = copy.deepcopy(payload)
        missing_field["comments"][0].pop("author")
        duplicate_thread = copy.deepcopy(payload)
        duplicate_thread["comments"][1]["thread_id"] = duplicate_thread["comments"][0][
            "thread_id"
        ]
        for candidate in (
            malformed,
            bad_position,
            missing_field,
            duplicate_thread,
        ):
            with self.subTest(candidate=candidate), self.assertRaises(
                MODULE.WorkflowError
            ):
                MODULE.validate_copilot_review_report(
                    f"```json\n{json.dumps(candidate)}\n```",
                    request_id="da11f083-e3a0-405c-ba50-7acfb1e51091",
                    preflight=preflight,
                    remote=remote,
                    paths_by_commit=paths_by_commit,
                )
        with self.assertRaisesRegex(
            MODULE.WorkflowError, "supplemental fix commit"
        ):
            MODULE.validate_copilot_review_report(
                content,
                request_id="da11f083-e3a0-405c-ba50-7acfb1e51091",
                preflight=preflight,
                remote=remote,
                paths_by_commit={
                    **paths_by_commit,
                    followup: ["unreported.py"],
                },
            )

    def test_exact_347_flat_identity_report_recovers_retained_task(self):
        commit = "75866ae2d80645888b08e3fd6148030faefb61b5"
        content = FLAT_IDENTITY_347_REPORT.read_text(encoding="utf-8")
        payload = MODULE.parse_markdown_report(content, description="test report")
        preflight = copy.deepcopy(self.preflight)
        preflight["pr"].update(
            {
                "number": 347,
                "repo_name": "open-telemetry/shared-workflows",
                "pr_url": "https://github.com/open-telemetry/shared-workflows/pull/347",
                "url": "https://github.com/open-telemetry/shared-workflows/pull/347",
                "head_owner": "open-telemetry",
                "head_repo": "shared-workflows",
                "head_repository": "open-telemetry/shared-workflows",
                "head_branch": "trask-fix-dashboard-publisher-contention",
                "base_branch": "main",
                "head_sha": "f1e7ea3dabd0fab27c6fadc2d257c97ce574e106",
                "base_sha": "55fb421179d32aef3b36c7f6503f57193561d14c",
            }
        )
        preflight["comment_identities"] = [
            {
                "id": item["comment"],
                "author": item["author"],
                "body_sha256": item["body_sha256"],
                "line": item["current_line"],
                "original_line": item["original_line"],
                "path": item["path"],
                "review_id": item["review"],
                "side": item["side"],
                "source": item["source"],
                "thread_id": item["thread"],
                "url": item["url"],
            }
            for item in payload["comments"]
        ]
        remote = MODULE.validate_success_result(
            MODULE.load_agent_task_result(FLAT_IDENTITY_347_RESULT),
            preflight=preflight,
            requested_model="gpt-5.6-sol",
        )
        paths_by_commit = {
            commit: [
                ".github/scripts/pull-request-dashboard/process_queue_batch.py",
                ".github/scripts/pull-request-dashboard/state_branch.py",
                ".github/scripts/pull-request-dashboard/test_process_queue_batch.py",
                ".github/scripts/pull-request-dashboard/test_state_branch.py",
            ]
        }

        self.assertEqual(
            "5ce26e7ec80ae94c4c7a2245a6f3c259e73f4d0880ea4f4e3bd5a0518b2d5d31",
            MODULE.sha256_text(content),
        )
        self.assertEqual(
            "5667cd5870d64090109e45c17baf3325299995d12d52492b987705206d99f3ca",
            MODULE.sha256_text(
                FLAT_IDENTITY_347_RESULT.read_text(encoding="utf-8")
            ),
        )
        report = MODULE.validate_copilot_review_report(
            content,
            request_id="045b336c-74ed-4c30-ad1a-2d00f735908e",
            preflight=preflight,
            remote=remote,
            paths_by_commit=paths_by_commit,
        )

        self.assertEqual(
            [commit] * 3,
            [item["commit"] for item in report["comments"]],
        )
        self.assertEqual(
            [4028817771, 4028817841, 4028817901],
            [item["id"] for item in report["comments"]],
        )
        stale = copy.deepcopy(payload)
        stale["head_sha"] = "9" * 40
        wrong_comment = copy.deepcopy(payload)
        wrong_comment["comments"][0]["comment"] = 1
        duplicate_thread = copy.deepcopy(payload)
        duplicate_thread["comments"][1]["thread"] = duplicate_thread["comments"][0][
            "thread"
        ]
        extra_field = copy.deepcopy(payload)
        extra_field["comments"][0]["reason"] = "not part of this producer shape"
        for candidate in (stale, wrong_comment, duplicate_thread, extra_field):
            with self.subTest(candidate=candidate), self.assertRaises(
                MODULE.WorkflowError
            ):
                MODULE.validate_copilot_review_report(
                    f"```json\n{json.dumps(candidate)}\n```",
                    request_id="045b336c-74ed-4c30-ad1a-2d00f735908e",
                    preflight=preflight,
                    remote=remote,
                    paths_by_commit=paths_by_commit,
                )

    def test_forward_compact_v3_report_rejects_lost_position_identity(self):
        payload = MODULE.parse_markdown_report(
            FORWARD_COMPACT_V3_REPORT.read_text(encoding="utf-8"),
            description="test report",
        )
        preflight = copy.deepcopy(self.preflight)
        preflight["pr"].update(
            {
                "number": 377,
                "repo_name": "open-telemetry/shared-workflows",
                "head_sha": payload["head_sha"],
            }
        )
        item = payload["comments"][0]
        identity = {
            "body_sha256": item["body_sha256"],
            "id": item["comment_id"],
            "line": item["line"],
            "original_line": item["line"],
            "path": item["path"],
            "review_id": item["review_id"],
            "source": "thread",
            "thread_id": item["thread_id"],
            "url": item["url"],
        }
        preflight["comment_identities"] = [identity]
        common = {
            "content": FORWARD_COMPACT_V3_REPORT.read_text(encoding="utf-8"),
            "request_id": "request-1",
            "remote": {
                "commits": [item["commit"]],
                "requires_apply": True,
            },
            "paths_by_commit": {item["commit"]: item["changed_paths"]},
        }
        cases = []
        distinct_original = copy.deepcopy(preflight)
        distinct_original["comment_identities"][0]["original_line"] = 114
        cases.append(distinct_original)
        sided = copy.deepcopy(preflight)
        sided["comment_identities"][0]["side"] = "RIGHT"
        cases.append(sided)
        for case in cases:
            with self.subTest(identity=case["comment_identities"][0]):
                with self.assertRaisesRegex(
                    MODULE.WorkflowError, "omitted distinct position"
                ):
                    MODULE.validate_copilot_review_report(
                        preflight=case,
                        **common,
                    )
        malformed_payloads = []
        wrong_head = copy.deepcopy(payload)
        wrong_head["head_sha"] = "9" * 40
        malformed_payloads.append(wrong_head)
        wrong_pr = copy.deepcopy(payload)
        wrong_pr["pull_request"] = 378
        malformed_payloads.append(wrong_pr)
        wrong_comment = copy.deepcopy(payload)
        wrong_comment["comments"][0]["comment_id"] = 18
        malformed_payloads.append(wrong_comment)
        wrong_commit = copy.deepcopy(payload)
        wrong_commit["comments"][0]["commit"] = "9" * 40
        malformed_payloads.append(wrong_commit)
        extra_position = copy.deepcopy(payload)
        extra_position["comments"][0]["original_line"] = 115
        malformed_payloads.append(extra_position)
        for malformed in malformed_payloads:
            with self.subTest(payload=malformed):
                with self.assertRaises(MODULE.WorkflowError):
                    MODULE.validate_copilot_review_report(
                        json.dumps(malformed),
                        request_id="request-1",
                        preflight=preflight,
                        remote=common["remote"],
                        paths_by_commit=common["paths_by_commit"],
                    )

    def test_canonical_report_preserves_diff_side_and_history_contract(self):
        preflight = copy.deepcopy(self.preflight)
        comment = {**self.comment, "side": "RIGHT"}
        preflight["comment_identities"] = [MODULE.comment_identity(comment)]
        content = json.loads(self.report())
        report = MODULE.validate_copilot_review_report(
            json.dumps(content),
            request_id="request-1",
            preflight=self.preflight,
            remote=self.remote(),
            paths_by_commit={},
        )
        prompt = MODULE.build_worker_prompt(
            preflight,
            request_id="request-1",
            iteration_allowance=1,
            prior_history=[],
        )

        self.assertEqual(
            MODULE.POSITIONAL_COPILOT_REVIEW_REPORT_SCHEMA,
            report["schema"],
        )
        self.assertIn('"side": "RIGHT"', prompt)
        self.assertIn('"original_line": 7', prompt)
        self.assertIn('"source": "thread"', prompt)
        self.assertIn('"author": "copilot-pull-request-reviewer[bot]"', prompt)
        self.assertIn('"head_ref": "feature"', prompt)
        self.assertIn('"base_ref": "main"', prompt)
        self.assertIn("linear single-parent code commits", prompt)
        self.assertIn("without squashing or rewriting", prompt)

    def test_canonical_report_v3_validates_refs_and_separate_author(self):
        preflight = copy.deepcopy(self.preflight)
        preflight["comment_identities"][0]["author"] = (
            "copilot-pull-request-reviewer[bot]"
        )
        content = json.loads(self.report())
        content["schema"] = MODULE.COPILOT_REVIEW_REPORT_SCHEMA
        content["pull_request"].update(
            {"head_ref": "feature", "base_ref": "main"}
        )
        content["comments"][0]["author"] = (
            "copilot-pull-request-reviewer[bot]"
        )

        report = MODULE.validate_copilot_review_report(
            f"```json\n{json.dumps(content)}\n```",
            request_id="request-1",
            preflight=preflight,
            remote={"commits": [], "requires_apply": True},
            paths_by_commit={},
        )

        self.assertEqual(
            "copilot-pull-request-reviewer[bot]",
            report["comments"][0]["author"],
        )
        self.assertEqual("thread", report["comments"][0]["source"])

    def test_exact_positional_compact_report_recovers_bot_source_alias(self):
        commit = "8f66336f18bbb637f105548ec82e1de7a4f611a0"
        path = ".github/scripts/github-actions-queue/collect.py"
        preflight = copy.deepcopy(self.preflight)
        preflight["pr"].update(
            {
                "number": 377,
                "repo_name": "open-telemetry/shared-workflows",
                "head_sha": "2d88ec12d35da8d0db74f695471daf29c22f4b68",
                "base_sha": "ad5b9918d6eca8cc999d7034757aee727b2631ea",
            }
        )
        preflight["comments"] = [
            {
                "id": 4024801108,
                "author": "copilot-pull-request-reviewer",
            }
        ]
        preflight["comment_identities"] = [
            {
                "body_sha256": (
                    "155337067d355b313ecf0916975458687f2282a0b1c171f69f55b11013194603"
                ),
                "id": 4024801108,
                "line": 478,
                "original_line": 478,
                "side": "RIGHT",
                "path": path,
                "review_id": 5221258792,
                "source": "thread",
                "thread_id": "PRRT_kwDOTENyc86i4Npm",
                "url": (
                    "https://github.com/open-telemetry/shared-workflows/"
                    "pull/377#discussion_r4024801108"
                ),
            }
        ]
        content = POSITIONAL_COMPACT_V4_REPORT.read_text(encoding="utf-8")
        self.assertEqual(
            "7ed1633ebe69527359b040b22ef40273f33568a207631064b0b419cfe33e01bd",
            MODULE.sha256_text(content),
        )

        report = MODULE.validate_copilot_review_report(
            content,
            request_id="08769fb7-efd9-4cb7-bb5a-d231223e0f64",
            preflight=preflight,
            remote={"commits": [commit], "requires_apply": True},
            paths_by_commit={
                commit: [
                    path,
                    ".github/scripts/github-actions-queue/test_collect.py",
                ]
            },
        )

        self.assertEqual("thread", report["comments"][0]["source"])
        self.assertEqual("RIGHT", report["comments"][0]["side"])
        self.assertEqual(commit, report["comments"][0]["commit"])

        future = copy.deepcopy(preflight)
        future["comment_identities"][0]["author"] = (
            "copilot-pull-request-reviewer"
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "retained structural"):
            MODULE.validate_copilot_review_report(
                content,
                request_id="08769fb7-efd9-4cb7-bb5a-d231223e0f64",
                preflight=future,
                remote={"commits": [commit], "requires_apply": True},
                paths_by_commit={
                    commit: [
                        path,
                        ".github/scripts/github-actions-queue/test_collect.py",
                    ]
                },
            )

        wrong_source = MODULE.parse_markdown_report(
            content, description="test report"
        )
        wrong_source["comments"][0]["source"] = "thread"
        with self.assertRaisesRegex(MODULE.WorkflowError, "mismatched comment"):
            MODULE.validate_copilot_review_report(
                f"```json\n{json.dumps(wrong_source)}\n```",
                request_id="08769fb7-efd9-4cb7-bb5a-d231223e0f64",
                preflight=preflight,
                remote={"commits": [commit], "requires_apply": True},
                paths_by_commit={
                    commit: [
                        path,
                        ".github/scripts/github-actions-queue/test_collect.py",
                    ]
                },
            )

    def test_preserve_artifacts_keeps_prompt_and_result_after_completion(self):
        task_state = {
            "prompt_file": "prompt.txt",
            "result_file": "result.json",
            "recovery_command": "resume",
            "recovery_files": ["result.json"],
        }
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt.txt"
            result = Path(directory) / "result.json"
            prompt.write_text("prompt", encoding="utf-8")
            result.write_text("result", encoding="utf-8")

            MODULE.finalize_agent_task_artifacts(
                task_state,
                {prompt, result},
                preserve=True,
            )

            self.assertTrue(prompt.is_file())
            self.assertTrue(result.is_file())
            expected_manifest = [
                {
                    "path": str(prompt),
                    "sha256": MODULE.sha256_file(prompt),
                    "size": prompt.stat().st_size,
                },
                {
                    "path": str(result),
                    "sha256": MODULE.sha256_file(result),
                    "size": result.stat().st_size,
                },
            ]
        self.assertFalse(task_state["artifacts_removed"])
        self.assertTrue(task_state["artifacts_preserved"])
        self.assertEqual(task_state["prompt_file"], "prompt.txt")
        self.assertEqual(task_state["result_file"], "result.json")
        self.assertEqual(expected_manifest, task_state["preserved_artifacts"])
        self.assertNotIn("recovery_command", task_state)

    def test_compact_v3_report_rejects_any_available_identity_drift(self):
        commit = self.fix
        identity = self.preflight["comment_identities"][0]
        item = {
            "body_sha256": identity["body_sha256"],
            "changed_paths": ["src/app.py"],
            "comment_id": identity["id"],
            "disposition": "fixed",
            "fix_commit": commit,
            "line": identity["line"],
            "path": identity["path"],
            "review_id": identity["review_id"],
            "source": "copilot-pull-request-reviewer",
            "thread_id": identity["thread_id"],
            "url": identity["url"],
        }
        for key, value in (
            ("body_sha256", "0" * 64),
            ("comment_id", 18),
            ("line", 8),
            ("path", "src/other.py"),
            ("review_id", 30),
            ("thread_id", "PRRT_other"),
            ("url", "https://github.com/owner/repo/pull/7#discussion_r18"),
            ("source", "another-author"),
            ("fix_commit", "9" * 40),
        ):
            malformed = {**item, key: value}
            with self.subTest(key=key), self.assertRaises(MODULE.WorkflowError):
                MODULE.validate_copilot_review_report(
                    json.dumps({"comments": [malformed]}),
                    request_id="request-1",
                    preflight=self.preflight,
                    remote={"commits": [commit], "requires_apply": True},
                    paths_by_commit={commit: ["src/app.py"]},
                )

    def test_rejects_missing_unknown_or_ambiguous_report_commit_mapping(self):
        remote = self.remote([self.fix])
        report = json.loads(self.report([self.fix]))
        cases = [
            (None, "fixed comment does not name"),
            ("9" * 40, "fixed comment does not name"),
            ([self.fix, "9" * 40], "fixed comment does not name"),
        ]
        for commit, message in cases:
            malformed = copy.deepcopy(report)
            malformed["comments"][0]["commit"] = commit
            with self.subTest(commit=commit):
                with self.assertRaisesRegex(MODULE.WorkflowError, message):
                    MODULE.validate_copilot_review_report(
                        json.dumps(malformed),
                        request_id="request-1",
                        preflight=self.preflight,
                        remote=remote,
                        paths_by_commit={self.fix: ["src/app.py"]},
                    )

    def test_decision_report_recovers_exact_historical_source_fix(self):
        preflight = copy.deepcopy(self.preflight)
        preflight["pr"]["head_sha"] = self.fix
        preflight["identity"]["head"] = self.fix
        preflight["comment_identities"][0]["line"] = 9
        finding_key = MODULE.decision_finding_key(
            preflight["comment_identities"][0]
        )
        historical = {
            "schema": MODULE.HISTORICAL_SOURCE_FIX_SCHEMA,
            "publication": {
                "task_id": "session-1",
                "source_head_sha": self.head,
                "published_head_sha": self.fix,
                "completed_at": "2026-09-17T06:33:59Z",
                "record_sha256": "a" * 64,
            },
            "owner": {
                "run_id": "request-1",
                "policy": MODULE.LOCAL_DECISION_POLICY,
                "model": MODULE.LOCAL_DECISION_MODEL,
                "reasoning_effort": MODULE.LOCAL_DECISION_REASONING_EFFORT,
                "record_sha256": "b" * 64,
            },
            "commits": [
                {"sha": self.fix, "changed_paths": ["src/app.py"]}
            ],
            "findings": [
                {"finding_key": finding_key, "commit": self.fix}
            ],
            "report": {
                "path": "prior-report.json",
                "sha256": "c" * 64,
                "size": 1,
            },
            "result": {
                "path": "prior-result.json",
                "sha256": "d" * 64,
                "size": 1,
            },
        }
        decision = {
            "decisions": [
                {
                    "finding_id": MODULE.decision_finding_id("request-1", 0),
                    "disposition": "fixed",
                }
            ],
        }
        with self.assertRaisesRegex(
            MODULE.WorkflowError,
            "no coordinator-owned source transition",
        ):
            MODULE.validate_copilot_review_report(
                json.dumps(decision),
                request_id="request-1",
                preflight=preflight,
                remote={"commits": [], "requires_apply": False},
                paths_by_commit={},
                active_local_decisions=True,
            )

        preflight["historical_fixes"] = historical
        report = MODULE.validate_copilot_review_report(
            json.dumps(decision),
            request_id="request-1",
            preflight=preflight,
            remote={"commits": [], "requires_apply": False},
            paths_by_commit={},
            active_local_decisions=True,
        )

        self.assertEqual("addressed", report["outcome"])
        self.assertEqual(self.fix, report["comments"][0]["commit"])
        changed = copy.deepcopy(preflight)
        changed["historical_fixes"]["findings"][0]["finding_key"] = "e" * 64
        with self.assertRaisesRegex(
            MODULE.WorkflowError,
            "stale identity",
        ):
            MODULE.validate_copilot_review_report(
                json.dumps(decision),
                request_id="request-1",
                preflight=changed,
                remote={"commits": [], "requires_apply": False},
                paths_by_commit={},
                active_local_decisions=True,
            )

    def test_rejects_malformed_mismatched_and_credential_artifacts(self):
        bad = self.result()
        bad["policy"]["sha256"] = "0" * 64
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.validate_success_result(
                bad,
                preflight=self.preflight,
                requested_model="gpt-5.6-sol",
            )
        incomplete = self.result()
        incomplete["attestation"]["structural_complete"] = False
        with self.assertRaisesRegex(MODULE.WorkflowError, "attestation"):
            MODULE.validate_success_result(
                incomplete,
                preflight=self.preflight,
                requested_model="gpt-5.6-sol",
            )
        report = json.loads(self.report())
        report["comments"][0]["thread_id"] = "PRRT_stale"
        with self.assertRaisesRegex(MODULE.WorkflowError, "mismatched comment"):
            MODULE.validate_copilot_review_report(
                json.dumps(report),
                request_id="request-1",
                preflight=self.preflight,
                remote=self.remote(),
                paths_by_commit={},
            )
        with self.assertRaisesRegex(MODULE.WorkflowError, "credentials"):
            MODULE.require_no_credentials(
                "Authorization: Bearer github_pat_abcdefghijklmnop",
                source="artifact",
            )

    def test_rejects_stale_head_threads_and_local_drift(self):
        live = dict(self.preflight["pr"])
        live["head_sha"] = "9" * 40
        with self.assertRaisesRegex(MODULE.WorkflowError, "drifted"):
            MODULE.require_live_pr_snapshot(
                self.preflight["pr"], live, expected_head=self.head
            )
        with (
            mock.patch.object(MODULE, "fetch_copilot_threads", return_value=([], [])),
            mock.patch.object(MODULE, "fetch_reviews", return_value=[]),
            self.assertRaisesRegex(MODULE.WorkflowError, "identity drifted"),
        ):
            MODULE.require_live_comments(self.preflight)

    def test_waits_for_its_own_published_head_but_rejects_other_drift(self):
        final = {**self.preflight["pr"], "head_sha": self.fix}
        with (
            mock.patch.object(
                MODULE,
                "metadata_for",
                side_effect=[self.preflight["pr"], final],
            ) as metadata,
            mock.patch.object(MODULE.time, "sleep") as sleep,
        ):
            actual = MODULE.wait_for_live_pr_snapshot(
                MODULE.parse_target("owner/repo#7"),
                self.preflight["pr"],
                expected_head=self.fix,
            )
        self.assertEqual(actual["head_sha"], self.fix)
        self.assertEqual(metadata.call_count, 2)
        sleep.assert_called_once()

        drifted = {**self.preflight["pr"], "title": "Changed elsewhere"}
        with (
            mock.patch.object(MODULE, "metadata_for", return_value=drifted),
            mock.patch.object(MODULE.time, "sleep") as sleep,
            self.assertRaisesRegex(MODULE.WorkflowError, "drifted"),
        ):
            MODULE.wait_for_live_pr_snapshot(
                MODULE.parse_target("owner/repo#7"),
                self.preflight["pr"],
                expected_head=self.fix,
            )
        sleep.assert_not_called()

    def test_reply_recovery_rejects_new_unresolved_copilot_threads(self):
        def thread(comment: dict[str, Any]) -> dict[str, Any]:
            return {
                "id": comment["thread_id"],
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": comment["id"],
                            "url": comment["url"],
                            "body": comment["body"],
                            "path": comment["path"],
                            "position": comment["position"],
                            "originalPosition": comment["original_position"],
                            "line": comment["line"],
                            "originalLine": comment["original_line"],
                            "author": {
                                "login": comment["author"],
                                "id": comment["author_bot_id"],
                            },
                            "pullRequestReview": {
                                "databaseId": comment["review_id"]
                            },
                        }
                    ]
                },
            }

        added = {
            **self.comment,
            "id": 18,
            "thread_id": "PRRT_added",
            "url": "https://github.com/owner/repo/pull/7#discussion_r18",
            "body": "Handle the second case.",
        }
        with (
            mock.patch.object(
                MODULE,
                "fetch_copilot_threads",
                return_value=([thread(self.comment), thread(added)], []),
            ),
            mock.patch.object(MODULE, "fetch_reviews", return_value=[]),
            self.assertRaisesRegex(MODULE.WorkflowError, "identity drifted"),
        ):
            MODULE.require_live_comments(self.preflight, allow_resolved=True)

    def test_post_publish_inventory_accepts_exact_resolved_line_shift(self):
        thread = {
            "id": self.comment["thread_id"],
            "isResolved": True,
            "comments": {
                "nodes": [
                    {
                        "databaseId": self.comment["id"],
                        "url": self.comment["url"],
                        "body": self.comment["body"],
                        "path": self.comment["path"],
                        "position": None,
                        "originalPosition": self.comment["original_position"],
                        "line": 9,
                        "originalLine": self.comment["original_line"],
                        "author": {
                            "login": self.comment["author"],
                            "id": self.comment["author_bot_id"],
                        },
                        "pullRequestReview": {
                            "databaseId": self.comment["review_id"]
                        },
                    }
                ]
            },
        }
        with (
            mock.patch.object(
                MODULE, "fetch_copilot_threads", return_value=([thread], [])
            ),
            mock.patch.object(MODULE, "fetch_reviews", return_value=[]),
        ):
            selected = MODULE.require_live_comments(
                self.preflight, allow_resolved=True
            )
        self.assertEqual(selected[0]["id"], self.comment["id"])
        self.assertEqual(9, selected[0]["line"])
        self.assertTrue(selected[0]["resolved"])

        thread["comments"]["nodes"][0]["body"] = "Changed review body"
        with (
            mock.patch.object(
                MODULE, "fetch_copilot_threads", return_value=([thread], [])
            ),
            mock.patch.object(MODULE, "fetch_reviews", return_value=[]),
            self.assertRaisesRegex(MODULE.WorkflowError, "identity drifted"),
        ):
            MODULE.require_live_comments(self.preflight, allow_resolved=True)

        thread["comments"]["nodes"][0]["body"] = self.comment["body"]
        thread["comments"]["nodes"][0]["line"] = self.comment["line"] + 1
        with (
            mock.patch.object(
                MODULE, "fetch_copilot_threads", return_value=([thread], [])
            ),
            mock.patch.object(MODULE, "fetch_reviews", return_value=[]),
            self.assertRaisesRegex(MODULE.WorkflowError, "identity drifted"),
        ):
            MODULE.require_live_comments(self.preflight, allow_resolved=False)

    def test_rejects_merge_artifacts_and_unexpected_history(self):
        remote = self.remote()
        with (
            mock.patch.object(
                MODULE,
                "git",
                side_effect=[self.artifact, f"{self.artifact} {self.head} {'9' * 40}"],
            ),
            self.assertRaisesRegex(MODULE.WorkflowError, "merge"),
        ):
            MODULE.validate_generated_history(
                self.repo_root, base_sha=self.head, remote=remote
            )
        with (
            mock.patch.object(MODULE, "git", return_value=""),
            self.assertRaisesRegex(MODULE.WorkflowError, "unexpected"),
        ):
            MODULE.validate_generated_history(
                self.repo_root, base_sha=self.head, remote=remote
            )


    def test_cleanup_removes_retained_external_task_artifacts(self):
        state_path = self.directory / "cleanup-state.json"
        prompt = self.directory / "prompt.txt"
        result = self.directory / "result.json"
        decision = self.directory / "decision.json"
        canonical = self.directory / "canonical.json"
        prompt.write_text("prompt", encoding="utf-8")
        result.write_text("result", encoding="utf-8")
        decision.write_text("decision", encoding="utf-8")
        canonical.write_text("canonical", encoding="utf-8")
        MODULE.save_state(
            state_path,
            {
                "version": MODULE.STATE_VERSION,
                "created_at": MODULE.utc_now(),
                "repo_root": str(self.repo_root),
                "monitoring": {"status": "completed"},
                "agent_task": {
                    "prompt_file": str(prompt),
                    "result_file": str(result),
                    "decision_file": str(decision),
                    "canonical_report_file": str(canonical),
                    "preserved_artifacts": [
                        {
                            "path": str(decision),
                            "sha256": MODULE.sha256_file(decision),
                            "size": decision.stat().st_size,
                        }
                    ],
                    "recovery_files": [str(prompt), str(result)],
                },
            },
        )
        with mock.patch.object(MODULE, "emit"):
            MODULE.command_cleanup(SimpleNamespace(state=str(state_path)))
        self.assertFalse(state_path.exists())
        self.assertFalse(prompt.exists())
        self.assertFalse(result.exists())
        self.assertFalse(decision.exists())
        self.assertFalse(canonical.exists())



    def test_windows_run_bytes_hides_console_processes(self):
        completed = MODULE.subprocess.CompletedProcess(["git"], 0, b"", b"")
        with (
            mock.patch.object(MODULE, "IS_WINDOWS", True),
            mock.patch.object(
                MODULE.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True
            ),
            mock.patch.object(
                MODULE.subprocess, "run", return_value=completed
            ) as subprocess_run,
        ):
            MODULE.run_bytes(["git"])
        self.assertEqual(subprocess_run.call_args.kwargs["creationflags"], 0x08000000)

    def test_budget_supports_multiple_iterations_and_stops_at_cap(self):
        state = {"iterations": 0, "budget_scope": "standalone"}
        MODULE.charge_iteration(state)
        MODULE.charge_iteration(state)
        self.assertEqual(state["iterations"], 2)
        self.assertIsNone(MODULE.exhausted_budget(2, 2, 5, None))
        self.assertEqual(MODULE.exhausted_budget(5, 5, 5, None), "iteration")
        pipeline = {"run": "flight", "iteration": 1, "baseline": 0, "run_baseline": 0}
        self.assertEqual(MODULE.absolute_iteration_cap(pipeline, 5, 3), 5)

    def test_clean_no_op_wins_at_cap_but_comments_do_not_start_another_task(self):
        clean_path = self.directory / "clean-at-cap.json"
        capped_path = self.directory / "comments-at-cap.json"
        for path in (clean_path, capped_path):
            MODULE.save_state(
                path,
                {
                    "version": MODULE.STATE_VERSION,
                    "created_at": MODULE.utc_now(),
                    "iterations": 5,
                    "history": [],
                },
            )
        clean = {
            **self.preflight,
            "comments": [],
            "comment_identities": [],
            "head_review_clean": True,
        }
        emitted = []
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=self.repo_root),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value=MODULE.parse_target("owner/repo#7"),
            ),
            mock.patch.object(MODULE, "agent_task_preflight", return_value=clean),
            mock.patch.object(MODULE, "discover_cloud_task") as discover,
            mock.patch.object(MODULE, "emit", emitted.append),
        ):
            MODULE.command_agent_task(self.arguments(clean_path, max_iterations=5))
        self.assertEqual(emitted[-1]["result"], "no_unresolved_comments")
        self.assertEqual(
            emitted[-1]["session_title"],
            f"Copilot Review Loop: 7 - {clean['pr']['title']}",
        )
        discover.assert_not_called()

        emitted.clear()
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=self.repo_root),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value=MODULE.parse_target("owner/repo#7"),
            ),
            mock.patch.object(
                MODULE, "agent_task_preflight", return_value=self.preflight
            ),
            mock.patch.object(MODULE, "discover_cloud_task") as discover,
            mock.patch.object(MODULE, "emit", emitted.append),
        ):
            MODULE.command_agent_task(self.arguments(capped_path, max_iterations=5))
        self.assertEqual(emitted[-1]["result"], "max_iterations_reached")
        discover.assert_not_called()

    def test_agent_uses_foreground_execution_without_a_required_watch_loop(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("with `mode: async`", instructions)
        self.assertIn("only when the user explicitly requests continuation after client exit", instructions)
        self.assertIn("Do not poll", instructions)
        self.assertIn("hash-verified terminal result", instructions)
        self.assertIn("frozen-head policy skip", instructions)

    def arguments(self, state_path, *, resume=False, max_iterations=5):
        return SimpleNamespace(
            target="owner/repo#7",
            repo_root=str(self.repo_root),
            state=str(state_path),
            resume=resume,
            model="sol",
            max_iterations=max_iterations,
            pipeline_run=None,
            pipeline_iteration=None,
            pipeline_max_iterations=None,
            watch_interval=0.01,
            cancellation_grace=0.01,
            prepare_only=False,
            apply_prepared=False,
            request_review_only=False,
            preserve_artifacts=False,
            recover_terminal_local=None,
            recovery_manifest_sha256=None,
            rescope_prepared_publish_only=False,
            publish_prepared_only=False,
        )


    def dead_local_owner_case(self, state_path):
        run_id = "d" * 32
        session_id = "f40839bc-8282-4b2b-aedb-eea76c34f74b"
        prompt_path = self.directory / "dead-local-prompt.txt"
        result_path = self.directory / "dead-local-result.json"
        decision_path = self.directory / "dead-local-decisions.json"
        canonical_path = self.directory / "dead-local-canonical.json"
        prompt_path.write_text("exact prompt\n", encoding="utf-8", newline="\n")
        events_path = (
            self.copilot_home / "session-state" / session_id / "events.jsonl"
        )
        events_path.parent.mkdir(parents=True)
        events_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session.start",
                            "data": {
                                "sessionId": session_id,
                                "selectedModel": MODULE.LOCAL_DECISION_MODEL,
                                "reasoningEffort": (
                                    MODULE.LOCAL_DECISION_REASONING_EFFORT
                                ),
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant.message",
                            "data": {"model": MODULE.LOCAL_DECISION_MODEL},
                        }
                    ),
                    "",
                ]
            ),
            encoding="utf-8",
            newline="\n",
        )
        lock_path = events_path.parent / "inuse.4242.lock"
        lock_path.write_text("4242", encoding="ascii")
        source_before = MODULE.local_source_owner_fingerprint(
            self.source_fingerprint
        )
        state = {
            "version": MODULE.STATE_VERSION,
            "created_at": "2026-09-17T00:00:00Z",
            "iterations": 0,
            "history": [],
            "managed_task_history": [],
            "repo_root": str(self.repo_root),
            "pr": copy.deepcopy(self.preflight["pr"]),
            "monitoring": {"status": "completed"},
            "agent_task": {
                "canonical_report_file": str(canonical_path),
                "decision_file": str(decision_path),
                "github_before": copy.deepcopy(self.github_fingerprint),
                "local_session_id": session_id,
                "model": MODULE.LOCAL_DECISION_MODEL,
                "policy": MODULE.LOCAL_DECISION_POLICY,
                "preflight": copy.deepcopy(self.preflight),
                "producer": "local",
                "prompt_file": str(prompt_path),
                "prompt_sha256": MODULE.sha256_file(prompt_path),
                "reasoning_effort": MODULE.LOCAL_DECISION_REASONING_EFFORT,
                "recovery_command": "legacy command",
                "remaining_iterations": 5,
                "result_file": str(result_path),
                "resume_attempts": 0,
                "run_id": run_id,
                "source_before": source_before,
                "started_at": "2026-09-17T00:00:00Z",
                "status": "running",
                "worker_command": MODULE.local_decision_command(
                    self.repo_root,
                    session_id=session_id,
                    run_id=run_id,
                    pr_number=7,
                ),
            },
        }
        MODULE.save_state(state_path, state)
        current_source = {
            "branch": "feature",
            "head": self.fix,
            "status": "",
            "refs_sha256": "f" * 64,
        }
        current_preflight = copy.deepcopy(self.preflight)
        current_preflight["identity"]["head"] = self.fix
        current_preflight["pr"]["head_sha"] = self.fix
        current_preflight["pr"]["base_sha"] = self.artifact
        return {
            "state": state,
            "state_path": state_path,
            "session_id": session_id,
            "events_path": events_path,
            "lock_path": lock_path,
            "current_source": current_source,
            "current_preflight": current_preflight,
            "target": {
                "owner": "owner",
                "repo": "repo",
                "number": 7,
                "pr_url": "https://github.com/owner/repo/pull/7",
            },
        }

    def dead_local_owner_patches(self, case):
        return (
            mock.patch.object(
                MODULE,
                "local_source_fingerprint",
                return_value=case["current_source"],
            ),
            mock.patch.object(
                MODULE,
                "agent_task_preflight",
                return_value=case["current_preflight"],
            ),
            mock.patch.object(
                MODULE, "base_revision_is_ancestor", return_value=True
            ),
            mock.patch.object(
                MODULE,
                "git",
                side_effect=lambda _root, command, *args: (
                    self.fix + "\n" if command == "rev-list" else "src/app.py\n"
                ),
            ),
            mock.patch.object(MODULE, "process_is_running", return_value=False),
        )





    def terminal_local_recovery_case(self, state_path):
        prompt_path = self.directory / "terminal-prompt.txt"
        decision_path = self.directory / "terminal-decisions.json"
        result_path = self.directory / "terminal-result.json"
        canonical_path = self.directory / "terminal-canonical.json"
        prompt_path.write_text("pinned prompt\n", encoding="utf-8", newline="\n")
        decision = self.local_decision()
        decision["decisions"][0].update(
            {
                "disposition": "fixed",
                "commit": self.fix,
                "changed_paths": ["src/app.py"],
            }
        )
        decision_path.write_text(
            json.dumps(decision, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        session_id = "terminal-local-session"
        events_path = (
            self.copilot_home / "session-state" / session_id / "events.jsonl"
        )
        events_path.parent.mkdir(parents=True)
        events_path.write_text(
            json.dumps(
                {
                    "type": "session.start",
                    "data": {
                        "sessionId": session_id,
                        "selectedModel": "gpt-5.6-sol",
                        "reasoningEffort": "high",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        before_source = copy.deepcopy(self.source_fingerprint)
        before_source["refs"].update(
            {
                "refs/copilot/workspace-diffs/other-session/head": "6" * 40,
                "refs/heads/other-session": "7" * 40,
                "refs/prefetch/remotes/origin/other-session": "8" * 40,
            }
        )
        after_source = copy.deepcopy(before_source)
        after_source["head"] = self.fix
        after_source["refs"]["refs/heads/feature"] = self.fix
        del after_source["refs"][
            "refs/copilot/workspace-diffs/other-session/head"
        ]
        del after_source["refs"]["refs/heads/other-session"]
        after_source["refs"][
            "refs/prefetch/remotes/origin/other-session"
        ] = "9" * 40
        state = {
            "version": MODULE.STATE_VERSION,
            "agent_task": {
                "status": "failed",
                "task_id_status": "terminal_unusable",
                "producer": "local",
                "policy": MODULE.LOCAL_DECISION_POLICY,
                "model": MODULE.LOCAL_DECISION_MODEL,
                "reasoning_effort": MODULE.LOCAL_DECISION_REASONING_EFFORT,
                "run_id": "terminal-owner",
                "local_session_id": session_id,
                "remaining_iterations": 5,
                "preflight": self.preflight,
                "prompt_file": str(prompt_path),
                "prompt_sha256": MODULE.sha256_file(prompt_path),
                "decision_file": str(decision_path),
                "result_file": str(result_path),
                "canonical_report_file": str(canonical_path),
                "source_before": before_source,
                "source_after": after_source,
                "github_before": self.github_fingerprint,
                "github_after": self.github_fingerprint,
                "worker_command": MODULE.local_decision_command(
                    self.repo_root,
                    session_id=session_id,
                    run_id="terminal-owner",
                    pr_number=7,
                ),
                "error": "local decision worker changed an unexpected Git ref",
            },
        }
        MODULE.save_state(state_path, state)
        manifest_path = self.directory / "terminal-recovery.json"
        manifest = {
            "schema": MODULE.TERMINAL_LOCAL_RECOVERY_MANIFEST_SCHEMA,
            "helper_sha256": MODULE.sha256_file(SCRIPT),
            "state": {
                "path": str(state_path),
                "sha256": MODULE.sha256_file(state_path),
            },
            "target": self.preflight["pr"]["pr_url"],
            "repo_root": str(self.repo_root),
            "owner": "terminal-owner",
            "session_id": session_id,
            "prompt": {
                "path": str(prompt_path),
                "sha256": MODULE.sha256_file(prompt_path),
                "size": prompt_path.stat().st_size,
            },
            "decisions": {
                "path": str(decision_path),
                "sha256": MODULE.sha256_file(decision_path),
                "size": decision_path.stat().st_size,
            },
            "events": {
                "path": str(events_path),
                "sha256": MODULE.sha256_file(events_path),
                "size": events_path.stat().st_size,
            },
            "findings": [
                {
                    key: decision["decisions"][0][key]
                    for key in (
                        "finding_key",
                        "disposition",
                        "commit",
                        "changed_paths",
                    )
                }
            ],
            "source_before": MODULE.local_source_owner_fingerprint(
                before_source
            ),
            "source_after": MODULE.local_source_owner_fingerprint(after_source),
            "github": self.github_fingerprint,
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        arguments = self.arguments(state_path)
        arguments.prepare_only = True
        arguments.preserve_artifacts = True
        arguments.recover_terminal_local = str(manifest_path)
        arguments.recovery_manifest_sha256 = MODULE.sha256_file(manifest_path)
        attestation = {
            "status": "complete",
            "session_id": session_id,
            "events_path": str(events_path),
            "events_sha256": MODULE.sha256_file(events_path),
            "startup_model": "gpt-5.6-sol",
            "startup_reasoning_effort": "high",
            "observed_models": ["gpt-5.6-sol"],
            "assistant_message_count": 42,
        }
        return arguments, manifest, attestation

    def test_exact_cca_disabled_result_is_a_trusted_task_creation_failure(self):
        result = MODULE.load_agent_task_result(CCA_DISABLED_RESULT)
        preflight = {
            "identity": {
                "branch": "trask-actions-queue-events",
                "head": "ba1cdf0d96365a55af87e62c2f476245af685bbb",
                "status": "",
            },
            "pr": {
                "number": 377,
                "pr_url": "https://github.com/open-telemetry/shared-workflows/pull/377",
                "repo_name": "open-telemetry/shared-workflows",
                "head_repository": "open-telemetry/shared-workflows",
                "head_branch": "trask-actions-queue-events",
                "head_sha": "ba1cdf0d96365a55af87e62c2f476245af685bbb",
                "base_branch": "main",
                "base_sha": "ad5b9918d6eca8cc999d7034757aee727b2631ea",
            },
        }

        failure = MODULE.validate_task_creation_failure_result(
            result,
            preflight=preflight,
            requested_model="gpt-5.6-sol",
            allow_legacy_policy=True,
        )

        self.assertEqual("api_failure", failure["code"])
        self.assertIn("CCA enabled", failure["message"])
        result["application"]["final_local_head"] = "0" * 40
        with self.assertRaisesRegex(MODULE.WorkflowError, "mismatched identity"):
            MODULE.validate_task_creation_failure_result(
                result,
                preflight=preflight,
                requested_model="gpt-5.6-sol",
                allow_legacy_policy=True,
            )


    def test_exact_v4_validation_failure_is_terminal_but_not_accepted_as_v5(self):
        result = MODULE.load_agent_task_result(MALFORMED_VALIDATION_RESULT)
        preflight = {
            "identity": {
                "branch": "trask-actions-queue-events",
                "head": "ba1cdf0d96365a55af87e62c2f476245af685bbb",
                "status": "",
            },
            "pr": {
                "number": 377,
                "pr_url": "https://github.com/open-telemetry/shared-workflows/pull/377",
                "repo_name": "open-telemetry/shared-workflows",
                "head_repository": "open-telemetry/shared-workflows",
                "head_branch": "trask-actions-queue-events",
                "head_sha": "ba1cdf0d96365a55af87e62c2f476245af685bbb",
                "base_branch": "main",
                "base_sha": "ad5b9918d6eca8cc999d7034757aee727b2631ea",
            },
        }

        failure = MODULE.validate_terminal_validation_failure_result(
            result,
            preflight=preflight,
            requested_model="gpt-5.6-sol",
            allow_legacy_policy=True,
        )

        self.assertEqual("validation_incomplete", failure["code"])
        with self.assertRaisesRegex(MODULE.WorkflowError, "mismatched policy"):
            MODULE.validate_terminal_validation_failure_result(
                result,
                preflight=preflight,
                requested_model="gpt-5.6-sol",
            )


    def test_exact_v1_missing_trailer_failure_is_terminal(self):
        result = MODULE.load_agent_task_result(MISSING_FINDING_TRAILER_RESULT)
        preflight = copy.deepcopy(self.preflight)
        preflight["identity"].update(
            {
                "branch": "trask-actions-queue-events",
                "head": "ba1cdf0d96365a55af87e62c2f476245af685bbb",
            }
        )
        preflight["pr"].update(
            {
                "number": 377,
                "pr_url": "https://github.com/open-telemetry/shared-workflows/pull/377",
                "repo_name": "open-telemetry/shared-workflows",
                "head_repository": "open-telemetry/shared-workflows",
                "head_branch": "trask-actions-queue-events",
                "head_sha": "ba1cdf0d96365a55af87e62c2f476245af685bbb",
                "base_branch": "main",
                "base_sha": "ad5b9918d6eca8cc999d7034757aee727b2631ea",
            }
        )

        failure = MODULE.validate_terminal_structural_failure_result(
            result,
            preflight=preflight,
            requested_model="gpt-5.6-sol",
        )

        self.assertEqual("malformed_history", failure["code"])
        self.assertIn("Finding: correlation", failure["message"])
        result["policy"]["version"] = 2
        with self.assertRaisesRegex(MODULE.WorkflowError, "mismatched policy"):
            MODULE.validate_terminal_structural_failure_result(
                result,
                preflight=preflight,
                requested_model="gpt-5.6-sol",
            )
        result = MODULE.load_agent_task_result(MISSING_FINDING_TRAILER_RESULT)
        result["error"]["message"] = "generated history is not linear"
        with self.assertRaisesRegex(MODULE.WorkflowError, "unexpected error detail"):
            MODULE.validate_terminal_structural_failure_result(
                result,
                preflight=preflight,
                requested_model="gpt-5.6-sol",
            )

    def test_exact_completed_no_artifact_results_are_terminal(self):
        for result_path in (NO_ARTIFACT_383_RESULT, NO_ARTIFACT_20074_RESULT):
            result = MODULE.load_agent_task_result(result_path)
            preflight = self.preflight_for_result(result)
            with self.subTest(task_id=result["task"]["id"]):
                failure = MODULE.validate_terminal_no_artifact_result(
                    result,
                    preflight=preflight,
                    requested_model="gpt-5.6-sol",
                )
                self.assertEqual("malformed_history", failure["code"])
                self.assertEqual([], result["generated"]["commits"])
                self.assertIsNone(result["report"]["commit"])

        malformed = MODULE.load_agent_task_result(NO_ARTIFACT_383_RESULT)
        malformed["generated"]["head_sha"] = "f" * 40
        with self.assertRaisesRegex(MODULE.WorkflowError, "malformed identity"):
            MODULE.validate_terminal_no_artifact_result(
                malformed,
                preflight=self.preflight_for_result(
                    MODULE.load_agent_task_result(NO_ARTIFACT_383_RESULT)
                ),
                requested_model="gpt-5.6-sol",
            )

    def test_exact_20050_legacy_validation_result_is_terminal(self):
        result = MODULE.load_agent_task_result(LEGACY_VALIDATION_20050_RESULT)
        failure = MODULE.validate_terminal_validation_failure_result(
            result,
            preflight=self.preflight_for_result(result),
            requested_model="gpt-5.6-sol",
            allow_legacy_policy=True,
        )

        self.assertEqual("validation_incomplete", failure["code"])
        self.assertEqual(
            MODULE.LEGACY_AGENT_TASK_POLICY_V4,
            result["policy"],
        )

    def test_exact_retained_terminal_results_accept_only_forward_base_ancestry(self):
        cases = (
            (
                NO_ARTIFACT_20074_RESULT,
                "2515ed4055bb1802c7d21d7a01882b92b6d5c675",
                MODULE.validate_terminal_no_artifact_result,
                {},
            ),
            (
                LEGACY_VALIDATION_20050_RESULT,
                "74090e90d391511a317984deb88c063ddd3ae02d",
                MODULE.validate_terminal_validation_failure_result,
                {"allow_legacy_policy": True},
            ),
        )
        for result_path, retained_base, validator, options in cases:
            result = MODULE.load_agent_task_result(result_path)
            preflight = self.preflight_for_result(result)
            task_base = preflight["pr"]["base_sha"]
            preflight["pr"]["base_sha"] = retained_base
            is_ancestor = mock.Mock(return_value=True)

            terminal_preflight = MODULE.terminal_result_preflight(
                result,
                preflight=preflight,
                repo_root=self.repo_root,
                is_ancestor=is_ancestor,
            )
            failure = validator(
                result,
                preflight=terminal_preflight,
                requested_model="gpt-5.6-sol",
                **options,
            )

            with self.subTest(task_id=result["task"]["id"]):
                self.assertEqual(result["error"]["code"], failure["code"])
                self.assertEqual(task_base, terminal_preflight["pr"]["base_sha"])
                self.assertEqual(retained_base, preflight["pr"]["base_sha"])
                is_ancestor.assert_called_once_with(
                    self.repo_root,
                    task_base,
                    retained_base,
                )

        result = MODULE.load_agent_task_result(NO_ARTIFACT_20074_RESULT)
        preflight = self.preflight_for_result(result)
        preflight["pr"]["base_sha"] = "f" * 40
        with self.assertRaisesRegex(MODULE.WorkflowError, "not an ancestor"):
            MODULE.terminal_result_preflight(
                result,
                preflight=preflight,
                repo_root=self.repo_root,
                is_ancestor=lambda _root, _ancestor, _descendant: False,
            )

        mismatched = MODULE.load_agent_task_result(NO_ARTIFACT_20074_RESULT)
        mismatched["pull_request"]["head_sha"] = "f" * 40
        with self.assertRaisesRegex(MODULE.WorkflowError, "does not match"):
            MODULE.terminal_result_preflight(
                mismatched,
                preflight=self.preflight_for_result(
                    MODULE.load_agent_task_result(NO_ARTIFACT_20074_RESULT)
                ),
                repo_root=self.repo_root,
            )


    def test_completed_owner_is_archived_before_a_new_findings_task(self):
        state_path = self.directory / "completed-owner-state.json"
        helper = self.directory / "cloud_task.py"
        helper.write_text("# helper\n", encoding="utf-8")
        completed = {
            "run_id": "completed-owner",
            "status": "completed",
            "task": {"id": "completed-task"},
            "published_head_sha": "previous-head",
        }
        MODULE.save_state(
            state_path,
            {
                "version": MODULE.STATE_VERSION,
                "created_at": MODULE.utc_now(),
                "iterations": 1,
                "history": [],
                "managed_task_history": [{"run_id": "legacy-owner"}],
                "pr": self.preflight["pr"],
                "agent_task": completed,
            },
        )
        arguments = self.arguments(state_path)

        def stop_after_dispatch(**_kwargs):
            raise RuntimeError("stop after dispatch")

        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=self.repo_root),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value=MODULE.parse_target("owner/repo#7"),
            ),
            mock.patch.object(
                MODULE,
                "wait_for_stable_review_preflight",
                return_value=self.preflight,
            ),
            mock.patch.object(
                MODULE,
                "require_live_comments",
                return_value=self.preflight["comments"],
            ),
            mock.patch.object(MODULE, "discover_cloud_task", return_value=helper),
            mock.patch.object(MODULE.secrets, "token_hex", return_value="new-owner"),
            mock.patch.object(MODULE, "run_hosted_decision_worker", side_effect=stop_after_dispatch),
            self.assertRaisesRegex(RuntimeError, "stop after dispatch"),
        ):
            MODULE.command_agent_task(arguments)

        restarted = MODULE.load_state(state_path)
        self.assertEqual(
            ["legacy-owner", "completed-owner"],
            [item["run_id"] for item in restarted["managed_task_history"]],
        )
        self.assertEqual(completed, restarted["managed_task_history"][1])
        self.assertEqual("new-owner", restarted["agent_task"]["run_id"])

    def test_fresh_review_comments_resume_the_managed_iteration(self):
        state_path = self.directory / "watch-state.json"
        events = []
        MODULE.save_state(
            state_path,
            {
                "version": MODULE.STATE_VERSION,
                "created_at": MODULE.utc_now(),
                "iterations": 1,
                "pr": self.preflight["pr"],
                "monitoring": {
                    "status": "requested",
                    "result": None,
                },
            },
        )

        def complete_watch(_args):
            events.append("watch")
            state = MODULE.load_state(state_path)
            state["monitoring"] = {
                "status": "completed",
                "result": {"result": MODULE.WATCHER_REVIEW_COMMENTS},
            }
            MODULE.save_state(state_path, state)
            events.append("terminal_review")

        arguments = self.arguments(state_path, resume=True)
        arguments.pipeline_run = "pipeline-run"
        arguments.pipeline_iteration = 2
        arguments.pipeline_max_iterations = 2
        with (
            mock.patch.object(MODULE, "command_watch", side_effect=complete_watch),
            mock.patch.object(MODULE, "wait_for_fresh_copilot_state") as fresh,
            mock.patch.object(
                MODULE, "command_agent_task",
                side_effect=lambda _args: events.append("next_iteration"),
            ) as next_iteration,
        ):
            MODULE.continue_after_review_request(arguments, state_path)
        fresh.assert_called_once()
        next_arguments = next_iteration.call_args.args[0]
        self.assertTrue(next_arguments.resume)
        self.assertEqual(next_arguments.max_iterations, 5)
        self.assertEqual("pipeline-run", next_arguments.pipeline_run)
        self.assertEqual(2, next_arguments.pipeline_iteration)
        self.assertEqual(["watch", "terminal_review", "next_iteration"], events)

    def test_post_apply_review_only_monitor_persists_findings_without_managed_task(
        self,
    ):
        state_path = self.directory / "review-only-watch-state.json"
        MODULE.save_state(
            state_path,
            {
                "version": MODULE.STATE_VERSION,
                "created_at": MODULE.utc_now(),
                "iterations": 1,
                "repo_root": str(self.repo_root),
                "pr": self.preflight["pr"],
                "monitoring": {
                    "status": "requested",
                    "result": None,
                },
            },
        )

        def complete_watch(_args):
            state = MODULE.load_state(state_path)
            state["monitoring"] = {
                "status": "completed",
                "result": {
                    "result": MODULE.WATCHER_REVIEW_COMMENTS,
                    "review_id": 29,
                    "comment_ids": [self.comment["id"]],
                },
            }
            MODULE.save_state(state_path, state)

        arguments = self.arguments(state_path)
        arguments.request_review_only = True
        arguments.apply_prepared = True
        emitted = []
        with (
            mock.patch.object(MODULE, "command_watch", side_effect=complete_watch),
            mock.patch.object(MODULE, "wait_for_fresh_copilot_state") as fresh,
            mock.patch.object(
                MODULE,
                "wait_for_stable_review_preflight",
                return_value=self.preflight,
            ) as stable,
            mock.patch.object(MODULE, "command_agent_task") as next_iteration,
            mock.patch.object(MODULE, "emit", emitted.append),
        ):
            MODULE.continue_after_review_request(arguments, state_path)

        fresh.assert_called_once()
        stable.assert_called_once()
        next_iteration.assert_not_called()
        saved = MODULE.load_state(state_path)
        self.assertEqual(
            "review_comments_pending_preparation", saved["last_result"]
        )
        self.assertEqual([self.comment], saved["queue"]["comments"])
        self.assertEqual(
            "review_comments_pending_preparation", emitted[-1]["result"]
        )
        self.assertEqual(
            self.preflight["comment_identities"],
            emitted[-1]["comment_identities"],
        )

    def test_review_only_clean_result_stops_without_managed_task(self):
        state_path = self.directory / "review-only-clean-state.json"
        MODULE.save_state(
            state_path,
            {
                "version": MODULE.STATE_VERSION,
                "created_at": MODULE.utc_now(),
                "iterations": 1,
                "repo_root": str(self.repo_root),
                "pr": self.preflight["pr"],
                "monitoring": {
                    "status": "requested",
                    "head_sha": self.head,
                    "result": None,
                },
            },
        )

        def complete_watch(_args):
            state = MODULE.load_state(state_path)
            state["clean_at_head_sha"] = self.head
            state["monitoring"] = {
                "status": "completed",
                "head_sha": self.head,
                "result": {
                    "result": MODULE.WATCHER_REVIEW_CLEAN,
                    "review_id": 29,
                    "clean_at_head_sha": self.head,
                },
            }
            MODULE.save_state(state_path, state)

        arguments = self.arguments(state_path)
        arguments.request_review_only = True
        emitted = []
        with (
            mock.patch.object(
                MODULE, "command_watch", side_effect=complete_watch
            ) as watch,
            mock.patch.object(MODULE, "command_agent_task") as next_iteration,
            mock.patch.object(MODULE, "emit", emitted.append),
        ):
            MODULE.continue_after_review_request(arguments, state_path)

        self.assertTrue(watch.call_args.args[0].resume_on_timeout)
        next_iteration.assert_not_called()
        saved = MODULE.load_state(state_path)
        self.assertEqual(self.head, saved["clean_at_head_sha"])
        self.assertEqual("loop_completed", emitted[-1]["result"])
        self.assertEqual("cleared", emitted[-1]["stage_outcome"])

    def test_review_only_existing_findings_do_not_request_or_dispatch(self):
        state_path = self.directory / "review-only-existing-comments.json"
        arguments = self.arguments(state_path)
        arguments.request_review_only = True
        emitted = []
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(MODULE, "resolve_repo_root", return_value=self.repo_root),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value=MODULE.parse_target("owner/repo#7"),
            ),
            mock.patch.object(
                MODULE,
                "wait_for_stable_review_preflight",
                return_value=self.preflight,
            ),
            mock.patch.object(MODULE, "request_copilot") as request,
            mock.patch.object(MODULE, "discover_cloud_task") as discover,
            mock.patch.object(MODULE, "run") as helper_run,
            mock.patch.object(MODULE, "emit", emitted.append),
        ):
            MODULE.command_agent_task(arguments)

        request.assert_not_called()
        discover.assert_not_called()
        helper_run.assert_not_called()
        saved = MODULE.load_state(state_path)
        self.assertEqual(
            "review_comments_pending_preparation", saved["last_result"]
        )
        self.assertEqual([self.comment], saved["queue"]["comments"])
        self.assertEqual(
            "review_comments_pending_preparation", emitted[-1]["result"]
        )

    def test_publication_checks_comments_before_and_after_push(self):
        source = SCRIPT.read_text(encoding="utf-8")
        coordinator = source.index("def command_agent_task")
        before_push = source.index("require_live_comments(", coordinator)
        push = source.index(
            "f\"HEAD:{pr['head_branch']}\"",
            before_push,
        )
        after_push = source.index("require_live_comments(", push)
        reply = source.index("post_missing_replies(", after_push)
        resolve = source.index("resolve_threads(", reply)
        self.assertLess(before_push, push)
        self.assertLess(push, after_push)
        self.assertLess(after_push, reply)
        self.assertLess(reply, resolve)


class TerminalCoordinatorContractTest(unittest.TestCase):
    def setUp(self):
        self.target = MODULE.parse_target(
            "open-telemetry/opentelemetry-java-instrumentation#16161"
        )
        self.state = json.loads(
            EMPTY_ACTIVE_REVIEW_REQUIRED_STATE.read_text(encoding="utf-8")
        )
        self.head = self.state["pr"]["head_sha"]
        self.preflight = {
            "repository_root": "repo",
            "identity": {
                "branch": "grpc-server-address",
                "head": self.head,
                "status": "",
            },
            "pr": {
                **self.state["pr"],
                "pr_url": (
                    "https://github.com/open-telemetry/"
                    "opentelemetry-java-instrumentation/pull/16161"
                ),
            },
            "viewer": {
                "login": "viewer",
                "repository_role": "write",
                "permissions": {
                    "admin": False,
                    "maintain": False,
                    "push": True,
                    "triage": True,
                    "pull": True,
                },
            },
            "comments": [],
            "comment_identities": [],
            "skipped_authors": [],
            "head_review_clean": True,
            "head_review_id": 1952601,
            "copilot_bot_id": "BOT_1",
        }

    def arguments(self, state_path):
        return SimpleNamespace(
            target=(
                "open-telemetry/"
                "opentelemetry-java-instrumentation#16161"
            ),
            repo_root="repo",
            state=str(state_path),
            resume=False,
            model="sol",
            max_iterations=5,
            pipeline_run=None,
            pipeline_iteration=None,
            pipeline_max_iterations=None,
            watch_interval=0.01,
            cancellation_grace=0.01,
            prepare_only=False,
            apply_prepared=False,
            request_review_only=False,
            preserve_artifacts=False,
            recover_terminal_local=None,
            recovery_manifest_sha256=None,
            rescope_prepared_publish_only=False,
            publish_prepared_only=False,
        )

    def unreviewed_preflight(self):
        incident = json.loads(
            SOURCE_ONLY_REVIEW_REQUEST_BLOCKED_STATE.read_text(encoding="utf-8")
        )
        return {
            **copy.deepcopy(self.preflight),
            "repository_root": incident["repo_root"],
            "identity": {
                "branch": incident["pr"]["head_branch"],
                "head": incident["pr"]["head_sha"],
                "status": "",
            },
            "pr": copy.deepcopy(incident["pr"]),
            "comments": [],
            "comment_identities": [],
            "head_review_clean": False,
            "head_review_id": None,
            "copilot_bot_id": None,
        }

    def run_clean_coordinator(self, state_path, *, historical_fixes=None):
        emitted = []
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(
                MODULE,
                "resolve_repo_root",
                return_value=Path("repo"),
            ),
            mock.patch.object(
                MODULE, "resolve_target", return_value=self.target
            ),
            mock.patch.object(
                MODULE,
                "wait_for_stable_review_preflight",
                return_value=copy.deepcopy(self.preflight),
            ),
            mock.patch.object(
                MODULE,
                "agent_task_preflight",
                return_value=copy.deepcopy(self.preflight),
            ),
            mock.patch.object(
                MODULE,
                "historical_source_fixes",
                return_value=historical_fixes,
            ),
            mock.patch.object(MODULE, "emit", emitted.append),
        ):
            MODULE.command_agent_task(self.arguments(state_path))
        return emitted

    def test_exact_zero_iteration_active_empty_queue_is_not_success(self):
        self.assertEqual(
            "coordinator returned without validated current-head clearance",
            MODULE.terminal_agent_task_clearance_error(self.state, self.target),
        )


    def test_policy_skip_fails_closed_for_unsafe_variants(self):
        incident = json.loads(
            SOURCE_ONLY_REVIEW_REQUEST_BLOCKED_STATE.read_text(encoding="utf-8")
        )
        preflight = self.unreviewed_preflight()
        cases = {}

        active = copy.deepcopy(incident)
        active["agent_task"] = {"status": "running", "run_id": "owner"}
        cases["active task"] = (active, preflight, preflight)

        queued = copy.deepcopy(incident)
        queued["queue"]["comments"] = [{"id": 17, "status": "pending"}]
        cases["queued comments"] = (queued, preflight, preflight)

        monitored = copy.deepcopy(incident)
        monitored["monitoring"] = {"status": "requested", "head_sha": self.head}
        cases["active monitoring"] = (monitored, preflight, preflight)

        actionable = copy.deepcopy(preflight)
        actionable["comments"] = [copy.deepcopy(self.state["queue"])]
        actionable["comment_identities"] = [{"id": 17}]
        cases["fresh actionable work"] = (incident, actionable, actionable)

        drifted = copy.deepcopy(preflight)
        drifted["pr"]["head_sha"] = "f" * 40
        drifted["identity"]["head"] = "f" * 40
        cases["head drift"] = (incident, preflight, drifted)

        wrong_error = copy.deepcopy(incident)
        wrong_error["coordinator"]["detail"] = "another coordinator failure"
        cases["different coordinator error"] = (
            wrong_error,
            preflight,
            preflight,
        )

        previous_policy = MODULE.ACTIVE_GITHUB_MUTATION_POLICY
        MODULE.ACTIVE_GITHUB_MUTATION_POLICY = "source-only"
        try:
            self.assertEqual(
                self.head,
                MODULE.source_only_policy_skip_head(
                    incident,
                    preflight,
                    copy.deepcopy(preflight),
                    self.target,
                    pipeline_run=incident["pipeline_budget"]["run"],
                    state_sha256=MODULE.SOURCE_ONLY_LEGACY_POLICY_BLOCK_SHA256,
                ),
            )
            for name, (state, first, confirmation) in cases.items():
                with self.subTest(name=name), self.assertRaises(MODULE.WorkflowError):
                    MODULE.source_only_policy_skip_head(
                        state,
                        first,
                        confirmation,
                        self.target,
                        pipeline_run=incident["pipeline_budget"]["run"],
                        state_sha256=MODULE.SOURCE_ONLY_LEGACY_POLICY_BLOCK_SHA256,
                    )
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "pipeline owner identity drifted"
            ):
                MODULE.source_only_policy_skip_head(
                    incident,
                    preflight,
                    preflight,
                    self.target,
                    pipeline_run="another-run",
                    state_sha256=MODULE.SOURCE_ONLY_LEGACY_POLICY_BLOCK_SHA256,
                )
            MODULE.ACTIVE_GITHUB_MUTATION_POLICY = "allow"
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "requires exact source-only"
            ):
                MODULE.source_only_policy_skip_head(
                    incident,
                    preflight,
                    preflight,
                    self.target,
                    pipeline_run=incident["pipeline_budget"]["run"],
                    state_sha256=MODULE.SOURCE_ONLY_LEGACY_POLICY_BLOCK_SHA256,
                )
        finally:
            MODULE.ACTIVE_GITHUB_MUTATION_POLICY = previous_policy

    def test_policy_skip_rejects_unpinned_legacy_state_and_replayed_proof(self):
        incident = json.loads(
            SOURCE_ONLY_REVIEW_REQUEST_BLOCKED_STATE.read_text(encoding="utf-8")
        )
        preflight = self.unreviewed_preflight()
        previous_policy = MODULE.ACTIVE_GITHUB_MUTATION_POLICY
        MODULE.ACTIVE_GITHUB_MUTATION_POLICY = "source-only"
        try:
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "hash-bound policy block"
            ):
                MODULE.source_only_policy_skip_head(
                    incident,
                    preflight,
                    copy.deepcopy(preflight),
                    self.target,
                    pipeline_run=incident["pipeline_budget"]["run"],
                    state_sha256="0" * 64,
                )
            with tempfile.TemporaryDirectory() as directory:
                state_path = Path(directory) / "state.json"
                args = self.arguments(state_path)
                args.pipeline_run = incident["pipeline_budget"]["run"]
                args.pipeline_iteration = 1
                args.pipeline_max_iterations = 2
                state = MODULE.record_source_only_policy_skip(
                    None,
                    preflight=preflight,
                    args=args,
                    repo_root=Path(preflight["repository_root"]),
                    state_path=state_path,
                )
            drifted = copy.deepcopy(preflight)
            drifted["pr"]["head_sha"] = "f" * 40
            drifted["identity"]["head"] = "f" * 40
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "replayed against a different preflight"
            ):
                MODULE.source_only_policy_skip_head(
                    state,
                    drifted,
                    copy.deepcopy(drifted),
                    self.target,
                    pipeline_run=incident["pipeline_budget"]["run"],
                    state_sha256=None,
                )
            changed_viewer = copy.deepcopy(preflight)
            changed_viewer["viewer"]["login"] = "another-viewer"
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "replayed against a different preflight"
            ):
                MODULE.source_only_policy_skip_head(
                    state,
                    changed_viewer,
                    copy.deepcopy(changed_viewer),
                    self.target,
                    pipeline_run=incident["pipeline_budget"]["run"],
                    state_sha256=None,
                )
        finally:
            MODULE.ACTIVE_GITHUB_MUTATION_POLICY = previous_policy

    def test_allow_policy_still_requests_a_missing_review(self):
        preflight = self.unreviewed_preflight()
        previous_policy = MODULE.ACTIVE_GITHUB_MUTATION_POLICY
        with tempfile.TemporaryDirectory() as directory:
            state_path = MODULE.cli_path(str(Path(directory) / "state.json"))
            args = self.arguments(state_path)
            args.pipeline_run = "allow-run"
            args.pipeline_iteration = 1
            args.pipeline_max_iterations = 2
            args.github_mutation_policy = "allow"
            try:
                with (
                    mock.patch.object(MODULE, "require_tools"),
                    mock.patch.object(
                        MODULE,
                        "resolve_repo_root",
                        return_value=Path(preflight["repository_root"]),
                    ),
                    mock.patch.object(
                        MODULE, "resolve_target", return_value=self.target
                    ),
                    mock.patch.object(
                        MODULE,
                        "wait_for_stable_review_preflight",
                        return_value=copy.deepcopy(preflight),
                    ),
                    mock.patch.object(
                        MODULE, "remote_head", return_value=self.head
                    ),
                    mock.patch.object(
                        MODULE,
                        "request_copilot",
                        return_value={
                            "status": "requested",
                            "head_sha": self.head,
                        },
                    ) as request,
                    mock.patch.object(
                        MODULE, "continue_after_review_request"
                    ) as continuation,
                    mock.patch.object(MODULE, "emit"),
                ):
                    MODULE.command_agent_task(args)
            finally:
                MODULE.ACTIVE_GITHUB_MUTATION_POLICY = previous_policy
            saved = MODULE.load_state(state_path)

        request.assert_called_once()
        continuation.assert_called_once_with(args, state_path)
        self.assertEqual("review_required", saved["last_result"])
        self.assertEqual("active", saved["queue"]["status"])
        self.assertNotIn("policy_skip", saved)


    def test_main_persists_the_exact_artifact_as_a_coordinator_error(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            MODULE.save_state(state_path, copy.deepcopy(self.state))
            args = SimpleNamespace(command="agent-task")

            def incomplete(current):
                current._coordinator_state_path = state_path
                current._coordinator_target = self.target

            args.function = incomplete
            parser = mock.Mock()
            parser.parse_args.return_value = args
            emitted = []
            with (
                mock.patch.object(MODULE, "build_parser", return_value=parser),
                mock.patch.object(MODULE, "emit", emitted.append),
            ):
                result = MODULE.main()

            saved = MODULE.load_state(state_path)
            exit_payload = emitted[-1]
            emitted.clear()
            with mock.patch.object(MODULE, "emit", emitted.append):
                MODULE.command_status(
                    SimpleNamespace(
                        current=False,
                        state=str(state_path),
                        repo_root=None,
                    )
                )

        self.assertEqual(1, result)
        self.assertEqual("terminal_state_not_clear", exit_payload["reason"])
        self.assertEqual("ready", emitted[-1]["result"])
        self.assertEqual("coordinator_error", saved["last_result"])
        self.assertIsNone(saved["clean_at_head_sha"])
        self.assertEqual("blocked", saved["coordinator"]["status"])
        self.assertEqual(
            "coordinator_error", saved["escalation"]["reason"]
        )
        self.assertEqual("blocked", emitted[-1]["coordinator"]["status"])
        self.assertEqual(
            "coordinator_error", emitted[-1]["escalation"]["reason"]
        )

    def test_main_returns_zero_only_for_a_validated_terminal_clear_state(self):
        cleared = copy.deepcopy(self.state)
        cleared["clean_at_head_sha"] = self.head
        cleared["last_result"] = "no_unresolved_comments"
        cleared["queue"]["status"] = "clean"
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            MODULE.save_state(state_path, cleared)
            args = SimpleNamespace(command="agent-task")

            def complete(current):
                current._coordinator_state_path = state_path
                current._coordinator_target = self.target

            args.function = complete
            parser = mock.Mock()
            parser.parse_args.return_value = args
            with (
                mock.patch.object(MODULE, "build_parser", return_value=parser),
                mock.patch.object(MODULE, "emit"),
            ):
                result = MODULE.main()

        self.assertEqual(0, result)

    def test_pipeline_alias_rejects_success_output_without_terminal_state(self):
        argv = [str(SCRIPT), "pipeline", "owner/repo#7",
                "--state", "missing-state.json", "--pipeline-run", "1" * 32,
                "--pipeline-iteration", "1", "--pipeline-max-iterations", "2"]
        emitted = []
        with (
            mock.patch.object(MODULE.sys, "argv", argv),
            mock.patch.object(
                MODULE, "command_agent_task",
                side_effect=lambda _args: MODULE.emit({"result": "success"}),
            ),
            mock.patch.object(MODULE, "emit", emitted.append),
        ):
            self.assertEqual(1, MODULE.main())
        self.assertEqual("success", emitted[0]["result"])
        self.assertEqual("error", emitted[-1]["result"])
        self.assertIn("terminal state identity", emitted[-1]["error"])

    def test_nonzero_exit_preserves_an_existing_terminal_reason(self):
        capped = copy.deepcopy(self.state)
        capped["last_result"] = "max_iterations_reached"
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            MODULE.save_state(state_path, capped)
            args = SimpleNamespace(
                _coordinator_state_path=state_path,
                _coordinator_target=self.target,
            )

            MODULE.persist_agent_task_coordinator_error(
                args,
                MODULE.WorkflowError("terminal state is not clear"),
            )
            saved = MODULE.load_state(state_path)

        self.assertEqual("max_iterations_reached", saved["last_result"])
        self.assertNotIn("coordinator", saved)
        self.assertEqual("nonzero", saved["terminal_exit"]["status"])

    def test_revalidated_empty_queue_clears_only_at_the_frozen_head(self):
        self.assertEqual(
            self.head,
            MODULE.empty_queue_clearance_head(
                self.state,
                self.preflight,
                copy.deepcopy(self.preflight),
                self.target,
            ),
        )
        cleared = copy.deepcopy(self.state)
        cleared["clean_at_head_sha"] = self.head
        cleared["last_result"] = "no_unresolved_comments"
        cleared["queue"]["status"] = "clean"
        self.assertIsNone(
            MODULE.terminal_agent_task_clearance_error(cleared, self.target)
        )

    def test_clean_preflight_does_not_replace_persisted_queue_work(self):
        retained = copy.deepcopy(self.state)
        retained["queue"]["comments"] = [{"id": 1, "status": "pending"}]
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            MODULE.save_state(state_path, retained)

            with self.assertRaisesRegex(
                MODULE.WorkflowError,
                "malformed or has work",
            ):
                self.run_clean_coordinator(state_path)
            saved = MODULE.load_state(state_path)

        self.assertEqual(retained["queue"], saved["queue"])
        self.assertEqual("review_required", saved["last_result"])

    def test_clean_preflight_preserves_terminal_monitoring_state(self):
        retained = copy.deepcopy(self.state)
        retained["monitoring"] = {
            "status": "completed",
            "head_sha": self.head,
            "result": {"result": "review_comments"},
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            MODULE.save_state(state_path, retained)

            self.run_clean_coordinator(
                state_path,
                historical_fixes=[{"commit": "f" * 40}],
            )
            saved = MODULE.load_state(state_path)

        self.assertEqual(retained["monitoring"], saved["monitoring"])
        self.assertEqual(self.head, saved["clean_at_head_sha"])
        self.assertEqual("clean", saved["queue"]["status"])

    def test_empty_queue_clearance_rejects_unsafe_state_and_identity(self):
        cases = {}

        nonempty = copy.deepcopy(self.state)
        nonempty["queue"]["comments"] = [{"id": 1, "status": "pending"}]
        cases["nonempty work"] = (nonempty, self.preflight, self.preflight)

        owned = copy.deepcopy(self.state)
        owned["agent_task"] = {"status": "running", "run_id": "owner"}
        cases["active owner"] = (owned, self.preflight, self.preflight)

        monitored = copy.deepcopy(self.state)
        monitored["monitoring"] = {"status": "running", "pid": 123}
        cases["active monitoring"] = (
            monitored,
            self.preflight,
            self.preflight,
        )

        requesting = copy.deepcopy(self.state)
        requesting["monitoring"] = {"status": "requesting"}
        cases["requesting review"] = (
            requesting,
            self.preflight,
            self.preflight,
        )

        invalid_monitoring = copy.deepcopy(self.state)
        invalid_monitoring["monitoring"] = {"status": "unknown"}
        cases["malformed monitoring"] = (
            invalid_monitoring,
            self.preflight,
            self.preflight,
        )

        missing_identity = copy.deepcopy(self.preflight)
        missing_identity["identity"] = {}
        cases["missing identity"] = (
            self.state,
            missing_identity,
            missing_identity,
        )

        malformed = copy.deepcopy(self.state)
        malformed["queue"] = []
        cases["malformed state"] = (
            malformed,
            self.preflight,
            self.preflight,
        )

        unbound_owner = {
            "version": MODULE.STATE_VERSION,
            "coordinator": {"status": "stabilizing"},
        }
        cases["unbound coordinator owner"] = (
            unbound_owner,
            self.preflight,
            self.preflight,
        )

        replayed = copy.deepcopy(self.state)
        replayed["pr"]["base_sha"] = "e" * 40
        cases["stored identity drift"] = (
            replayed,
            self.preflight,
            self.preflight,
        )

        invalid_base = copy.deepcopy(self.preflight)
        invalid_base["pr"]["base_sha"] = "invalid"
        cases["invalid base identity"] = (
            self.state,
            invalid_base,
            invalid_base,
        )

        wrong_target = copy.deepcopy(self.preflight)
        wrong_target["pr"]["number"] = 20075
        cases["mismatched target"] = (
            self.state,
            wrong_target,
            wrong_target,
        )

        drifted = copy.deepcopy(self.preflight)
        drifted["pr"]["head_sha"] = "f" * 40
        drifted["identity"]["head"] = "f" * 40
        cases["head drift"] = (self.state, self.preflight, drifted)

        for name, (state, preflight, confirmation) in cases.items():
            with self.subTest(name=name), self.assertRaises(MODULE.WorkflowError):
                MODULE.empty_queue_clearance_head(
                    state,
                    preflight,
                    confirmation,
                    self.target,
                )

    def test_load_state_rejects_a_non_object_document(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text("[]\n", encoding="utf-8")

            with self.assertRaisesRegex(
                MODULE.WorkflowError,
                "does not contain a JSON object",
            ):
                MODULE.load_state(state_path)


class DetachedPipelineCheckoutTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        MODULE.git(self.repo, "init", "-b", "feature")
        MODULE.git(self.repo, "config", "user.name", "Test")
        MODULE.git(self.repo, "config", "user.email", "test@example.com")
        (self.repo / "code.txt").write_text("initial\n", encoding="utf-8")
        MODULE.git(self.repo, "add", "code.txt")
        MODULE.git(self.repo, "commit", "-m", "Initial")
        self.head = MODULE.git(self.repo, "rev-parse", "HEAD")
        MODULE.git(self.repo, "checkout", "--detach", self.head)
        self.target = MODULE.parse_target("owner/repo#7")
        self.pr = {
            **self.target,
            "state": "OPEN", "is_draft": False, "title": "Title", "body": "",
            "head_branch": "feature", "head_sha": self.head,
            "base_branch": "main", "base_sha": "2" * 40,
            "head_owner": "owner", "head_repo": "repo",
            "head_repository": "owner/repo",
            "upstream_owner": "owner", "upstream_repo": "repo",
        }
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, kwargs in (
            ("require_tools", {}),
            ("metadata_for", {"side_effect": lambda _target: dict(self.pr)}),
            ("remote_head", {"side_effect": lambda *_args: self.pr["head_sha"]}),
            ("find_push_remote", {"return_value": "origin"}),
            ("fetch_copilot_threads", {"return_value": ([], [])}),
            ("fetch_reviews", {"return_value": []}),
            ("gh_json", {"return_value": {
                "login": "viewer", "role_name": "write",
                "permissions": {"admin": False, "maintain": False,
                                "push": True, "triage": True, "pull": True},
            }}),
        ):
            self.stack.enter_context(mock.patch.object(MODULE, name, **kwargs))
        policy = MODULE.ACTIVE_GITHUB_MUTATION_POLICY
        self.addCleanup(setattr, MODULE, "ACTIVE_GITHUB_MUTATION_POLICY", policy)




    def test_detached_clearance_keeps_exact_head_and_pipeline_guards(self):
        preflight = MODULE.agent_task_preflight(
            self.repo, self.target, allow_detached=True
        )
        preflight.update(head_review_clean=True, head_review_id=123)
        self.assertEqual(
            self.head,
            MODULE.empty_queue_clearance_head(
                None, preflight, preflight, self.target, allow_detached=True
            ),
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "ownership identity"):
            MODULE.empty_queue_clearance_head(None, preflight, preflight, self.target)
        preflight["identity"]["head"] = "3" * 40
        with self.assertRaisesRegex(MODULE.WorkflowError, "ownership identity"):
            MODULE.empty_queue_clearance_head(
                None, preflight, preflight, self.target, allow_detached=True
            )



    def test_detached_standalone_wrong_head_and_dirty_checkouts_fail(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "requires a Pipeline"):
            MODULE.agent_task_preflight(self.repo, self.target)
        self.pr["head_sha"] = "3" * 40
        with self.assertRaisesRegex(MODULE.WorkflowError, "HEAD mismatch"):
            MODULE.agent_task_preflight(self.repo, self.target, allow_detached=True)
        self.pr["head_sha"] = self.head
        (self.repo / "code.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.WorkflowError, "not clean"):
            MODULE.agent_task_preflight(self.repo, self.target, allow_detached=True)


    def test_detached_transition_rejects_branch_and_worktree_drift(self):
        before = MODULE.local_source_fingerprint(self.repo)
        for changed in (
            {**before, "branch": "feature"},
            {**before, "worktree": str(self.root)},
            {**before, "status": " M code.txt"},
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                MODULE.WorkflowError, "changed the branch or working tree"
            ):
                MODULE.local_source_transition_evidence(
                    self.repo, before=before, after=changed
                )
        with self.assertRaisesRegex(MODULE.WorkflowError, "malformed worktree"):
            MODULE.local_source_owner_fingerprint(
                {key: value for key, value in before.items() if key != "worktree"}
            )



class ParseTargetTest(unittest.TestCase):
    def test_ignores_a_pasted_review_fragment(self):
        target = MODULE.parse_target(
            "https://github.com/open-telemetry/opentelemetry-java-instrumentation/"
            "pull/19233#pullrequestreview-4708244602"
        )

        self.assertEqual(target["number"], 19233)
        self.assertEqual(
            target["pr_url"],
            "https://github.com/open-telemetry/opentelemetry-java-instrumentation/pull/19233",
        )

    def test_ignores_a_pasted_comment_fragment(self):
        target = MODULE.parse_target(
            "https://github.com/open-telemetry/opentelemetry-java-instrumentation/"
            "pull/19233#discussion_r3590845592"
        )

        self.assertEqual(target["number"], 19233)

    def test_parses_short_pr_target(self):
        target = MODULE.parse_target(
            "open-telemetry/opentelemetry-java-instrumentation#19233"
        )

        self.assertEqual(target["owner"], "open-telemetry")
        self.assertEqual(target["number"], 19233)

    def test_rejects_a_non_pull_request_target(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.parse_target("https://github.com/open-telemetry/repo/issues/7")

    def test_resolve_target_falls_back_to_the_current_pr(self):
        target = MODULE.parse_target("https://github.com/open-telemetry/repo/pull/42")

        with mock.patch.object(
            MODULE, "current_pr_target", return_value=target
        ) as current:
            self.assertEqual(MODULE.resolve_target(None, Path("repo")), target)
            self.assertEqual(
                MODULE.resolve_target("open-telemetry/repo#43", Path("repo"))["number"],
                43,
            )

        current.assert_called_once_with(Path("repo"))


class CliPathTest(unittest.TestCase):
    def test_converts_git_bash_drive_path_on_windows(self):
        self.assertEqual(
            MODULE.normalize_cli_path("/c/src/repo", windows=True),
            "C:/src/repo",
        )

    def test_resolve_repo_root_uses_converted_path(self):
        completed = mock.Mock(stdout="C:/src/repo\n")
        with (
            mock.patch.object(MODULE, "cli_path", return_value=Path(r"C:\src\repo")),
            mock.patch.object(MODULE, "run", return_value=completed) as run,
        ):
            MODULE.resolve_repo_root("/c/src/repo")

        self.assertEqual(run.call_args.args[0][:3], ["git", "-C", "C:\\src\\repo"])


class MetadataTest(unittest.TestCase):
    def test_includes_the_pr_title(self):
        target = MODULE.parse_target("owner/repo#42")
        metadata = {
            "id": "PR_1",
            "number": 42,
            "title": "Fix the review loop",
            "url": target["pr_url"],
            "headRepositoryOwner": {"login": "owner"},
            "headRepository": {"name": "repo"},
            "headRefName": "branch",
            "headRefOid": "head",
            "baseRefName": "main",
            "baseRefOid": "frozen",
        }

        with (
            mock.patch.object(MODULE, "gh_json", return_value=metadata) as gh_json,
            mock.patch.object(MODULE, "base_ref_tip", return_value="live-tip"),
        ):
            result = MODULE.metadata_for(target)

        self.assertEqual(result["title"], "Fix the review loop")
        self.assertEqual(result["head_repository"], "owner/repo")
        self.assertIn("title", gh_json.call_args.args[0][-1].split(","))

    def test_normalizes_same_repository_and_fork_head_identity(self):
        self.assertEqual(
            "owner/repo",
            MODULE.head_repository_identity(
                {"head_owner": "owner", "head_repo": "repo"}
            ),
        )
        self.assertEqual(
            "contributor/fork",
            MODULE.head_repository_identity(
                {
                    "head_owner": "contributor",
                    "head_repo": "fork",
                    "head_repository": "contributor/fork",
                }
            ),
        )

    def test_rejects_missing_or_inconsistent_head_repository_identity(self):
        for metadata in (
            {"head_owner": "owner"},
            {"head_repo": "repo"},
            {
                "head_owner": "contributor",
                "head_repo": "fork",
                "head_repository": "owner/repo",
            },
        ):
            with self.subTest(metadata=metadata), self.assertRaisesRegex(
                MODULE.WorkflowError,
                "head repository identity",
            ):
                MODULE.head_repository_identity(metadata)

    def test_live_metadata_reaches_github_decision_fingerprint(self):
        target = MODULE.parse_target("owner/repo#42")
        raw = {
            "id": "PR_1",
            "number": 42,
            "title": "Fix the review loop",
            "body": "Pinned body",
            "state": "OPEN",
            "isDraft": False,
            "url": target["pr_url"],
            "headRepositoryOwner": {"login": "contributor"},
            "headRepository": {"name": "fork"},
            "headRefName": "feature",
            "headRefOid": "1" * 40,
            "baseRefName": "main",
        }
        expected = {
            "pr_node_id": "PR_1",
            "number": 42,
            "title": "Fix the review loop",
            "body": "Pinned body",
            "state": "OPEN",
            "is_draft": False,
            "url": target["pr_url"],
            "pr_url": target["pr_url"],
            "repo_name": "owner/repo",
            "upstream_owner": "owner",
            "upstream_repo": "repo",
            "head_owner": "contributor",
            "head_repo": "fork",
            "head_repository": "contributor/fork",
            "head_branch": "feature",
            "head_sha": "1" * 40,
            "base_branch": "main",
            "base_sha": "2" * 40,
        }
        with (
            mock.patch.object(MODULE, "gh_json", return_value=raw),
            mock.patch.object(MODULE, "base_ref_tip", return_value="2" * 40),
            mock.patch.object(MODULE, "require_live_comments"),
            mock.patch.object(
                MODULE,
                "fetch_copilot_threads",
                return_value=([], "BOT_1"),
            ),
            mock.patch.object(MODULE, "fetch_reviews", return_value=[]),
            mock.patch.object(
                MODULE,
                "remote_head",
                side_effect=["1" * 40, "2" * 40],
            ),
        ):
            fingerprint = MODULE.github_decision_fingerprint(
                target,
                {"pr": expected},
            )

        self.assertEqual("1" * 40, fingerprint["head_ref_sha"])
        self.assertEqual("2" * 40, fingerprint["base_ref_sha"])
        self.assertRegex(fingerprint["pr_sha256"], r"^[0-9a-f]{64}$")

    def test_base_sha_is_the_live_base_branch_tip_not_the_frozen_base_ref_oid(self):
        target = MODULE.parse_target("owner/repo#42")
        metadata = {
            "id": "PR_1",
            "number": 42,
            "title": "Fix the review loop",
            "url": target["pr_url"],
            "headRepositoryOwner": {"login": "owner"},
            "headRepository": {"name": "repo"},
            "headRefName": "branch",
            "headRefOid": "head",
            "baseRefName": "main",
            "baseRefOid": "frozen",
        }

        with (
            mock.patch.object(MODULE, "gh_json", return_value=metadata),
            mock.patch.object(MODULE, "base_ref_tip", return_value="live-tip") as tip,
        ):
            result = MODULE.metadata_for(target)

        self.assertEqual("live-tip", result["base_sha"])
        tip.assert_called_once_with("owner/repo", "main")

    def test_reports_a_deleted_head_repository(self):
        target = MODULE.parse_target("owner/repo#42")
        metadata = {
            "id": "PR_1",
            "number": 42,
            "url": target["pr_url"],
            "headRepositoryOwner": None,
            "headRepository": None,
            "headRefName": "branch",
            "headRefOid": "head",
            "baseRefName": "main",
            "baseRefOid": "frozen",
        }

        with (
            mock.patch.object(MODULE, "gh_json", return_value=metadata),
            self.assertRaisesRegex(
                MODULE.WorkflowError, "head repository is unavailable"
            ),
        ):
            MODULE.metadata_for(target)


class BaseRefTipTest(unittest.TestCase):
    def test_returns_the_live_tip_from_the_branch_ref(self):
        response = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"object": {"sha": "live-tip", "type": "commit"}}),
            stderr="",
        )
        with mock.patch.object(MODULE, "run", return_value=response) as run:
            self.assertEqual("live-tip", MODULE.base_ref_tip("owner/repo", "main"))
        self.assertEqual(
            ["gh", "api", "repos/owner/repo/git/ref/heads/main"],
            run.call_args.args[0],
        )

    def test_a_deleted_base_branch_raises_rather_than_falling_back(self):
        response = SimpleNamespace(
            returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)"
        )
        with mock.patch.object(MODULE, "run", return_value=response):
            with self.assertRaisesRegex(MODULE.WorkflowError, "may have been deleted"):
                MODULE.base_ref_tip("owner/repo", "gone")


class ProcessLivenessTest(unittest.TestCase):
    def test_windows_uses_a_non_signaling_query(self):
        with (
            mock.patch.object(MODULE, "IS_WINDOWS", True),
            mock.patch.object(
                MODULE, "windows_process_is_running", return_value=True
            ) as windows_query,
            mock.patch.object(MODULE.os, "kill") as kill,
        ):
            self.assertTrue(MODULE.process_is_running(123))

        windows_query.assert_called_once_with(123)
        kill.assert_not_called()

    def test_posix_uses_signal_zero(self):
        with (
            mock.patch.object(MODULE, "IS_WINDOWS", False),
            mock.patch.object(MODULE.os, "kill") as kill,
        ):
            self.assertTrue(MODULE.process_is_running(123))

        kill.assert_called_once_with(123, 0)


class CurrentPrStatusTest(unittest.TestCase):
    def test_resolves_current_pr_from_checked_out_repository(self):
        repo_root = Path("repo")
        upstream = {
            "remote": "origin",
            "repo": "open-telemetry/repo",
            "branch": "topic",
        }
        target = MODULE.parse_target("open-telemetry/repo#42")

        with (
            mock.patch.object(MODULE, "git", return_value="topic"),
            mock.patch.object(MODULE, "configured_upstream", return_value=upstream),
            mock.patch.object(
                MODULE, "simple_current_pr_target", return_value=target
            ) as simple,
            mock.patch.object(
                MODULE, "exact_upstream_pr_targets", return_value=[target]
            ) as exact,
        ):
            resolved = MODULE.current_pr_target(repo_root)

        self.assertEqual(resolved["number"], 42)
        simple.assert_called_once_with(repo_root, upstream)
        exact.assert_called_once_with(upstream)

    def test_simple_lookup_ignores_closed_pull_request(self):
        payload = {
            "url": "https://github.com/open-telemetry/repo/pull/42",
            "state": "CLOSED",
        }

        self.assertIsNone(MODULE.pr_target_from_payload(payload))

    def test_reads_configured_upstream_remote_and_merge_ref(self):
        outputs = {
            (
                "config",
                "--get",
                "branch.local-topic.remote",
            ): mock.Mock(returncode=0, stdout="fork\n"),
            (
                "config",
                "--get",
                "branch.local-topic.merge",
            ): mock.Mock(
                returncode=0, stdout="refs/heads/trask/grpc-metadata-selectors\n"
            ),
            (
                "remote",
                "get-url",
                "fork",
            ): mock.Mock(returncode=0, stdout="git@github.com:trask/repo.git\n"),
        }

        def fake_run(command, **_kwargs):
            return outputs[tuple(command[3:])]

        with mock.patch.object(MODULE, "run", side_effect=fake_run):
            upstream = MODULE.configured_upstream(Path("repo"), "local-topic")

        self.assertEqual(
            upstream,
            {
                "remote": "fork",
                "repo": "trask/repo",
                "branch": "trask/grpc-metadata-selectors",
            },
        )

    def test_uses_upstream_branch_when_local_branch_name_differs(self):
        repo_root = Path("repo")
        upstream = {
            "remote": "origin",
            "repo": "open-telemetry/repo",
            "branch": "trask/grpc-metadata-selectors",
        }
        target = MODULE.parse_target("open-telemetry/repo#19447")

        with (
            mock.patch.object(
                MODULE, "git", return_value="trask-grpc-metadata-selectors"
            ),
            mock.patch.object(MODULE, "configured_upstream", return_value=upstream),
            mock.patch.object(MODULE, "simple_current_pr_target") as simple,
            mock.patch.object(
                MODULE, "exact_upstream_pr_targets", return_value=[target]
            ) as exact,
        ):
            resolved = MODULE.current_pr_target(repo_root)

        self.assertEqual(resolved["number"], 19447)
        simple.assert_not_called()
        exact.assert_called_once_with(upstream)

    def test_rejects_multiple_exact_upstream_pull_requests(self):
        upstream = {
            "remote": "origin",
            "repo": "fork-owner/repo",
            "branch": "topic",
        }
        targets = [
            MODULE.parse_target("upstream/repo#1"),
            MODULE.parse_target("upstream/repo#2"),
        ]

        with (
            mock.patch.object(MODULE, "git", return_value="local-topic"),
            mock.patch.object(MODULE, "configured_upstream", return_value=upstream),
            mock.patch.object(
                MODULE, "exact_upstream_pr_targets", return_value=targets
            ),
            self.assertRaisesRegex(MODULE.WorkflowError, "multiple open pull requests"),
        ):
            MODULE.current_pr_target(Path("repo"))

    def test_reports_no_matching_upstream_pull_request(self):
        upstream = {
            "remote": "origin",
            "repo": "fork-owner/repo",
            "branch": "topic",
        }

        with (
            mock.patch.object(MODULE, "git", return_value="local-topic"),
            mock.patch.object(MODULE, "configured_upstream", return_value=upstream),
            mock.patch.object(MODULE, "exact_upstream_pr_targets", return_value=[]),
            self.assertRaisesRegex(MODULE.WorkflowError, "no open pull request"),
        ):
            MODULE.current_pr_target(Path("repo"))

    def test_reports_failed_lookup_without_an_upstream(self):
        with (
            mock.patch.object(MODULE, "git", return_value="topic"),
            mock.patch.object(MODULE, "configured_upstream", return_value=None),
            mock.patch.object(MODULE, "simple_current_pr_target", return_value=None),
            self.assertRaisesRegex(MODULE.WorkflowError, "no configured upstream"),
        ):
            MODULE.current_pr_target(Path("repo"))

    def test_exact_search_filters_to_remote_repository_and_branch(self):
        upstream = {
            "remote": "fork",
            "repo": "fork-owner/repo",
            "branch": "feature/topic",
        }
        payload = {
            "data": {
                "repository": {
                    "ref": {
                        "target": {
                            "associatedPullRequests": {
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                                "nodes": [
                                    {
                                        "url": "https://github.com/upstream/repo/pull/42",
                                        "state": "OPEN",
                                        "headRefName": "feature/topic",
                                        "headRepository": {
                                            "nameWithOwner": "fork-owner/repo"
                                        },
                                    },
                                    {
                                        "url": "https://github.com/other/repo/pull/99",
                                        "state": "OPEN",
                                        "headRefName": "feature/topic",
                                        "headRepository": {
                                            "nameWithOwner": "other/repo"
                                        },
                                    },
                                ],
                            }
                        }
                    }
                }
            }
        }

        with mock.patch.object(MODULE, "graphql", return_value=payload) as graphql:
            targets = MODULE.exact_upstream_pr_targets(upstream)

        self.assertEqual([target["number"] for target in targets], [42])
        self.assertEqual(
            graphql.call_args.args[1],
            {
                "owner": "fork-owner",
                "repo": "repo",
                "refName": "refs/heads/feature/topic",
                "after": None,
            },
        )

    def test_status_current_loads_only_current_pr_state(self):
        target = MODULE.parse_target("https://github.com/open-telemetry/repo/pull/42")
        state = {
            "version": MODULE.STATE_VERSION,
            "pr": {"number": 42, "url": target["pr_url"]},
            "queue": {"id": "pr-42"},
            "monitoring": {"status": "requested"},
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "current.json"
            MODULE.save_state(state_path, state)
            args = SimpleNamespace(current=True, state=None, repo_root="repo")

            with (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(
                    MODULE, "resolve_repo_root", return_value=Path(directory)
                ),
                mock.patch.object(MODULE, "current_pr_target", return_value=target),
                mock.patch.object(
                    MODULE, "default_state_path", return_value=state_path
                ) as default_state_path,
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_status(args)

        default_state_path.assert_called_once_with(target)
        payload = emit.call_args.args[0]
        self.assertEqual(payload["result"], "ready")
        self.assertEqual(payload["pr"]["number"], 42)
        self.assertEqual(payload["monitoring"]["status"], "requested")

    def test_status_current_reports_missing_current_pr_state(self):
        target = MODULE.parse_target("https://github.com/open-telemetry/repo/pull/42")

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "missing.json"
            args = SimpleNamespace(current=True, state=None, repo_root="repo")

            with (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(
                    MODULE, "resolve_repo_root", return_value=Path(directory)
                ),
                mock.patch.object(MODULE, "current_pr_target", return_value=target),
                mock.patch.object(
                    MODULE, "default_state_path", return_value=state_path
                ),
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_status(args)

        payload = emit.call_args.args[0]
        self.assertEqual(payload["result"], "no_state")
        self.assertEqual(payload["pr"]["url"], target["pr_url"])
        self.assertIsNone(payload["monitoring"])

    def test_fresh_invocations_never_select_the_legacy_pr_state(self):
        target = MODULE.parse_target("owner/repo#20173")
        args = SimpleNamespace(
            pipeline_run=None,
            invocation_run=None,
            new_invocation=False,
            state=None,
        )
        with mock.patch.object(
            MODULE.secrets, "token_hex", side_effect=["fresh-1", "fresh-2"]
        ):
            first, first_id = MODULE.invocation_state_path(target, args)
            second, second_id = MODULE.invocation_state_path(target, args)

        self.assertNotEqual(MODULE.default_state_path(target), first)
        self.assertNotEqual(first, second)
        self.assertEqual(("fresh-1", "fresh-2"), (first_id, second_id))


class QueueSelectionTest(unittest.TestCase):
    def setUp(self):
        self.copilot_thread = {
            "id": "thread-1",
            "isResolved": False,
            "comments": {
                "nodes": [
                    {
                        "databaseId": 10,
                        "url": "https://example.test/10",
                        "body": "root",
                        "path": "a.java",
                        "position": 1,
                        "originalPosition": 1,
                        "line": 2,
                        "originalLine": 2,
                        "author": {
                            "login": "copilot-pull-request-reviewer[bot]",
                            "id": "BOT_1",
                        },
                        "pullRequestReview": {"databaseId": 100},
                    },
                    {
                        "databaseId": 11,
                        "url": "https://example.test/11",
                        "body": "reply",
                        "path": "a.java",
                        "position": 1,
                        "originalPosition": 1,
                        "line": 2,
                        "originalLine": 2,
                        "author": {"login": "author"},
                        "pullRequestReview": {"databaseId": 101},
                    },
                ]
            },
        }
        self.human_thread = {
            "id": "thread-2",
            "isResolved": False,
            "comments": {
                "nodes": [
                    {
                        "databaseId": 20,
                        "url": "https://example.test/20",
                        "body": "human review",
                        "path": "b.java",
                        "position": 3,
                        "originalPosition": 3,
                        "line": 4,
                        "originalLine": 4,
                        "author": {"login": "reviewer"},
                        "pullRequestReview": {"databaseId": 102},
                    }
                ]
            },
        }
        self.resolved_copilot_thread = {
            "id": "thread-3",
            "isResolved": True,
            "comments": {
                "nodes": [
                    {
                        "databaseId": 30,
                        "url": "https://example.test/30",
                        "body": "already handled",
                        "author": {"login": "copilot-pull-request-reviewer"},
                        "pullRequestReview": {"databaseId": 103},
                    }
                ]
            },
        }
        self.threads = [
            self.copilot_thread,
            self.human_thread,
            self.resolved_copilot_thread,
        ]

    def test_selects_only_unresolved_copilot_thread_roots(self):
        threads, skipped = MODULE.partition_copilot_threads(self.threads)
        queue = MODULE.select_queue(threads)

        self.assertEqual([comment["id"] for comment in queue], [10])
        self.assertEqual(queue[0]["source"], "thread")
        self.assertEqual(queue[0]["author_bot_id"], "BOT_1")
        self.assertEqual(skipped, ["reviewer"])

    def test_selects_copilot_comments_across_every_review(self):
        second_review_thread = {
            "id": "thread-4",
            "isResolved": False,
            "comments": {
                "nodes": [
                    {
                        "databaseId": 40,
                        "url": "https://example.test/40",
                        "body": "newer review",
                        "author": {"login": "copilot-pull-request-reviewer"},
                        "pullRequestReview": {"databaseId": 200},
                    }
                ]
            },
        }

        threads, _ = MODULE.partition_copilot_threads(
            [*self.threads, second_review_thread]
        )
        queue = MODULE.select_queue(threads)

        self.assertEqual([comment["id"] for comment in queue], [10, 40])
        self.assertEqual([comment["review_id"] for comment in queue], [100, 200])

    def test_reports_skipped_authors_when_no_copilot_comments_remain(self):
        threads, skipped = MODULE.partition_copilot_threads([self.human_thread])

        self.assertEqual(threads, [])
        self.assertEqual(MODULE.select_queue(threads), [])
        self.assertEqual(skipped, ["reviewer"])

    def test_drops_every_human_thread_before_the_queue_is_built(self):
        threads, _ = MODULE.partition_copilot_threads(self.threads)

        self.assertEqual([thread["id"] for thread in threads], ["thread-1", "thread-3"])
        serialized = json.dumps(threads)
        self.assertNotIn("human review", serialized)
        self.assertNotIn("https://example.test/20", serialized)

    def test_keeps_a_human_reply_inside_a_copilot_thread(self):
        threads, skipped = MODULE.partition_copilot_threads([self.copilot_thread])

        self.assertEqual(threads, [self.copilot_thread])
        self.assertEqual(skipped, [])

    def test_reports_each_skipped_author_once_and_ignores_resolved_human_threads(self):
        resolved_human_thread = {
            "id": "thread-5",
            "isResolved": True,
            "comments": {
                "nodes": [{"databaseId": 50, "author": {"login": "settled-reviewer"}}]
            },
        }
        anonymous_thread = {
            "id": "thread-6",
            "isResolved": False,
            "comments": {"nodes": [{"databaseId": 60, "author": None}]},
        }

        threads, skipped = MODULE.partition_copilot_threads(
            [
                self.human_thread,
                dict(self.human_thread, id="thread-7"),
                resolved_human_thread,
                anonymous_thread,
            ]
        )

        self.assertEqual(threads, [])
        self.assertEqual(skipped, ["reviewer", "unknown"])

    def test_fetches_and_filters_threads_together(self):
        with mock.patch.object(
            MODULE, "fetch_threads", return_value=self.threads
        ) as fetch_threads:
            threads, skipped = MODULE.fetch_copilot_threads("owner", "repo", 7)

        fetch_threads.assert_called_once_with("owner", "repo", 7)
        self.assertEqual([thread["id"] for thread in threads], ["thread-1", "thread-3"])
        self.assertEqual(skipped, ["reviewer"])

    def test_fetches_only_selected_thread_ids(self):
        payload = {"data": {"t0": self.copilot_thread}}
        with mock.patch.object(MODULE, "graphql", return_value=payload) as graphql:
            threads = MODULE.fetch_threads_by_id(["thread-1", "thread-1"])

        self.assertEqual(threads, [self.copilot_thread])
        self.assertIn('node(id:"thread-1")', graphql.call_args.args[0])

    def test_resolved_thread_still_marks_its_review_as_having_findings(self):
        review = {"id": 103}

        self.assertTrue(
            MODULE.review_has_inline_findings(review, [self.resolved_copilot_thread])
        )


class CarryOverProgressTest(unittest.TestCase):
    def test_preserves_approved_but_unpublished_work(self):
        previous = [
            {
                "id": 10,
                "status": "handled",
                "batch": "batch-1",
                "commit": "abc123",
                "summary": "fixed it",
                "rationale": None,
                "reply_id": None,
            }
        ]
        refreshed = [
            {
                "id": 10,
                "status": "pending",
                "batch": None,
                "commit": None,
                "summary": None,
                "rationale": None,
                "reply_id": None,
            },
            {"id": 20, "status": "pending", "batch": None, "commit": None},
        ]

        MODULE.carry_over_progress(previous, refreshed)

        self.assertEqual(refreshed[0]["status"], "handled")
        self.assertEqual(refreshed[0]["commit"], "abc123")
        self.assertEqual(refreshed[0]["summary"], "fixed it")
        self.assertEqual(refreshed[1]["status"], "pending")


class SuppressedCommentTest(unittest.TestCase):
    def test_parses_exact_legacy_review_details_body(self):
        review = json.loads(LEGACY_REVIEW_DETAILS.read_text(encoding="utf-8"))
        self.assertEqual(
            hashlib.sha256(review["body"].encode("utf-8")).hexdigest(),
            "c57a8daa9046648b080ad31ff5e4f7c7606bbdc093b25035fd661b1c3647b7b8",
        )
        self.assertEqual(MODULE.latest_copilot_review([review], None), review)
        self.assertEqual(MODULE.latest_copilot_review_for_head(
            [review], None, review["commit_id"]
        ), review)
        entries = MODULE.parse_suppressed_comments(review["body"])
        self.assertEqual(entries, [{
            "path": "instrumentation/jedis/jedis-3.0/javaagent/src/main/java/io/"
            "opentelemetry/javaagent/instrumentation/jedis/v3_0/JedisRequest.java",
            "line": 36,
            "body": "[Performance] This target lookup now runs for every Redis "
            "command, including the default legacy-semconv mode, even though both "
            "server getters ignore `getServerTarget()` in that mode. That adds a "
            "`Context.current()` plus `VirtualField` lookup on the command hot "
            "path without affecting legacy telemetry; gate the lookup on stable "
            "database semconv.",
        }])
        queued = MODULE.suppressed_queue(review, entries)
        self.assertEqual(queued[0]["id"], -5203651790000)
        self.assertEqual(queued[0]["review_id"], review["id"])
        self.assertEqual(queued[0]["source"], "suppressed")
        self.assertIsNone(queued[0]["thread_id"])
        self.assertIsNone(queued[0]["reply_id"])

    def test_legacy_review_details_fail_on_unparsed_or_unknown_active_feedback(self):
        body = json.loads(LEGACY_REVIEW_DETAILS.read_text(encoding="utf-8"))["body"]
        cases = [
            body.replace("Suppressed comments (1)", "Suppressed comments (2)"),
            body.replace("Suppressed comments (1)", "Suppressed comments (unknown)"),
            body.replace("Previously missed (1)", "Previously missed (2)"),
            body.replace("Previously missed (1)", "Previously missed (unknown)"),
            body.replace("Previously missed (1)", "Unknown feedback (1)"),
            body.replace("### Suppressed comments (1)", "**Suppressed comments (1)**"),
            body.replace("### Suppressed comments (1)", ""),
            body.replace("JedisRequest.java:36**", "JedisRequest.java**"),
            body.replace(
                "- **Files reviewed:**", "#### Additional concern\n"
                "Unsupported nested feedback.\n- **Files reviewed:**"
            ),
            body[:body.index("* [Performance]")] + "</details>",
            body.replace("</details>", ""),
            body.replace("</summary>", ""),
            "<details><summary>Review details</summary>\n"
            "### Suppressed comments (0)\nUnexpected feedback.\n</details>",
        ]
        for malformed in cases:
            with self.subTest(body=malformed), self.assertRaisesRegex(
                MODULE.WorkflowError, "review body"
            ):
                MODULE.parse_suppressed_comments(malformed)

    def test_legacy_review_details_exclude_markdown_and_html_resolved_history(self):
        body = """
<details><summary>Review details</summary>
### Resolved since last review (1)
#### Suppressed comments (1)
**old.py:1**
* Resolved markdown finding.
<details><summary>Previously missed (1)</summary>
<details><summary>Resolved nested finding</summary>
`old.py:4`
Resolved nested concern.
</details>
</details>
### Suppressed comments (1)
**src/active.py:2**
* Active finding.
<details><summary>Resolved since last review (1)</summary>
### Suppressed comments (1)
**old.py:3**
* Resolved HTML finding.
</details>
- **Files reviewed:** 1/1 changed files
- **Comments generated:** 0 new
- **Review effort level:** Balanced
</details>
"""
        self.assertEqual(MODULE.parse_suppressed_comments(body), [
            {"path": "src/active.py", "line": 2, "body": "Active finding."},
        ])
        self.assertEqual(MODULE.parse_suppressed_comments(
            body.replace("### Suppressed comments (1)\n**src/active.py:2**\n"
                         "* Active finding.", "### Suppressed comments (0)")
        ), [])

    def test_legacy_review_details_keep_multiple_findings_and_nested_v2_feedback(self):
        body = """
<details><summary>Review details</summary>
### Suppressed comments (2)
**Previously missed (2)** - in unchanged code.
**src/first.py:1**
* First finding.
```python
### This heading is code, not a feedback boundary.
value = 1
```
**src/second.py:2**
* Second finding.
### Review summary
Not part of the second finding.
<details><summary>Previously missed (1)</summary>
<details><summary>Third finding</summary>
`src/third.py:3`
Third concern.
</details>
</details>
</details>
"""
        self.assertEqual(MODULE.parse_suppressed_comments(body), [
            {"path": "src/first.py", "line": 1,
             "body": "First finding.\n```python\n"
             "### This heading is code, not a feedback boundary.\nvalue = 1\n```"},
            {"path": "src/second.py", "line": 2, "body": "Second finding."},
            {"path": "src/third.py", "line": 3,
             "body": "Third finding\n\nThird concern."},
        ])

    def test_parses_exact_ccr_v2_previously_missed_body(self):
        review = json.loads(CCR_V2_REVIEW.read_text(encoding="utf-8"))
        self.assertEqual(
            hashlib.sha256(review["body"].encode("utf-8")).hexdigest(),
            "27359ec30de8bd1fd5adcebb1ca6d64fa219eb824c746773ad8e202c62f2b458",
        )
        entries = MODULE.parse_suppressed_comments(review["body"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(
            entries[0]["path"],
            "instrumentation/grpc-1.6/library/src/main/java/io/opentelemetry/"
            "instrumentation/grpc/v1_6/GrpcTelemetry.java",
        )
        self.assertEqual(entries[0]["line"], 93)
        self.assertEqual(
            entries[0]["body"],
            "Document capability-based fallback for gRPC 1.64+ builders\n\n"
            "[Documentation] This version-based guarantee is too broad: gRPC 1.64 "
            "adds the hook to `ManagedChannelBuilder`, but its default implementation "
            "throws `UnsupportedOperationException`. Custom builders that do not "
            "override/delegate the hook therefore take this method's fallback path "
            "even on 1.64+, so their target is not captured. Please document the "
            "capability-based fallback as well as the version boundary.",
        )
        queued = MODULE.suppressed_queue(review, entries)
        self.assertEqual(queued[0]["id"], -5259532804000)
        self.assertEqual(queued[0]["source"], "suppressed")
        self.assertIsNone(queued[0]["thread_id"])

    def test_parses_all_nested_findings_and_legacy_sections_in_order(self):
        body = """
<details><summary><strong>Previously missed (2)</strong></summary>
<details><summary>First &amp; second</summary>
`src/\u200bFirst.java:1`
First concern.
</details>
<details><summary>Third</summary>
`src/Second.java:2`
Second concern.
</details>
</details>
<details><summary>Suppressed comments (1)</summary>
**src/Third.java:3**
* Legacy concern.
</details>
"""
        self.assertEqual(MODULE.parse_suppressed_comments(body), [
            {"path": "src/First.java", "line": 1,
             "body": "First & second\n\nFirst concern."},
            {"path": "src/Second.java", "line": 2,
             "body": "Third\n\nSecond concern."},
            {"path": "src/Third.java", "line": 3, "body": "Legacy concern."},
        ])

    def test_rejects_recognized_but_unparsed_body_feedback(self):
        body = json.loads(CCR_V2_REVIEW.read_text(encoding="utf-8"))["body"]
        cases = [
            body.replace("Previously missed (1)", "Previously missed (2)"),
            body.replace("Previously missed (1)", "Previously missed (zero)"),
            body.replace("GrpcTelemetry.java:93`", "GrpcTelemetry.java`"),
            body.rsplit("</details>", 1)[0],
            body.replace("</summary>", "", 2),
            "<details><summary>Previously missed (1)</summary>Unreadable</details>",
            "<details><summary>Suppressed comments (1)</summary>Unreadable</details>",
            "<details><summary>Suppressed comments (2)</summary>"
            "**a.java:1**\n* Only one.\n</details>",
            "<details><summary>Suppressed comments (1)</summary>"
            "**a.java:1**\n* \n</details>",
        ]
        for malformed in cases:
            with self.subTest(body=malformed), self.assertRaisesRegex(
                MODULE.WorkflowError, "review body"
            ):
                MODULE.parse_suppressed_comments(malformed)

    def test_exact_ccr_v2_resolved_only_body_has_no_feedback(self):
        body = json.loads(CCR_V2_RESOLVED_REVIEW.read_text(encoding="utf-8"))["body"]
        self.assertEqual(
            hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "cc02b2d01ec84c0ef95d21308df46258f4ce7de471239de27350efcc86b5ad51",
        )
        self.assertEqual(MODULE.parse_suppressed_comments(body), [])

    def test_body_feedback_supports_html_tag_case_and_spacing(self):
        body = """
<DETAILS class="feedback"><SUMMARY ><strong>Previously missed (1)</strong></SUMMARY >
<DETAILS ><SUMMARY >A concern</SUMMARY >
`src/First.java:1`
Full text.
</DETAILS >
</DETAILS >
"""
        self.assertEqual(MODULE.parse_suppressed_comments(body), [
            {"path": "src/First.java", "line": 1, "body": "A concern\n\nFull text."},
        ])

    def test_summary_prose_and_resolved_sections_are_not_feedback(self):
        body = """
<!-- ccr-overview-v2 -->
### Needs a closer look
**Findings:** None
<details><summary>Resolved since last review (1)</summary>
<details><summary>Previously missed (1)</summary>
<details><summary>Old concern</summary>
`src/First.java:1`
Already resolved.
</details>
</details>
</details>
<details><summary>Previously missed (0)</summary></details>
"""
        self.assertEqual(MODULE.parse_suppressed_comments(body), [])

    def test_parses_multiple_suppressed_comments_with_fenced_context(self):
        body = """
<details>
<summary>Show a summary per file</summary>

Nothing to queue.
</details>
<details>
<summary>Suppressed comments (3)</summary>

**src/First.java:65**
* [Testing] Add coverage for this branch.
```java
return value;
```
**src/First.java:58**
* [Maintainability] Extract this expression.
**nested/path/Second.java:7**
* Avoid the redundant allocation.
</details>
"""

        self.assertEqual(
            MODULE.parse_suppressed_comments(body),
            [
                {
                    "path": "src/First.java",
                    "line": 65,
                    "body": "[Testing] Add coverage for this branch.\n"
                    "```java\nreturn value;\n```",
                },
                {
                    "path": "src/First.java",
                    "line": 58,
                    "body": "[Maintainability] Extract this expression.",
                },
                {
                    "path": "nested/path/Second.java",
                    "line": 7,
                    "body": "Avoid the redundant allocation.",
                },
            ],
        )

    def test_ignores_non_suppressed_details(self):
        body = """
<details>
<summary>Show a summary per file</summary>

**src/First.java:65**
* This is summary content, not a suppressed comment.
</details>
"""

        self.assertEqual(MODULE.parse_suppressed_comments(body), [])

    def test_synthetic_ids_are_stable_and_do_not_collide_across_reviews(self):
        body = """
<details><summary>Suppressed comments (2)</summary>
**a.java:1**
* First.
**b.java:2**
* Second.
</details>
"""
        first_review = {
            "id": 100,
            "html_url": "https://example.test/review/100",
            "body": body,
            "user": {
                "login": "copilot-pull-request-reviewer[bot]",
                "node_id": "BOT_1",
            },
        }
        second_review = {**first_review, "id": 101}

        first_parse = MODULE.suppressed_queue(
            first_review, MODULE.parse_suppressed_comments(body)
        )
        repeated_parse = MODULE.suppressed_queue(
            first_review, MODULE.parse_suppressed_comments(body)
        )
        second_parse = MODULE.suppressed_queue(
            second_review, MODULE.parse_suppressed_comments(body)
        )

        self.assertEqual(
            [comment["id"] for comment in first_parse],
            [comment["id"] for comment in repeated_parse],
        )
        self.assertTrue(
            {comment["id"] for comment in first_parse}.isdisjoint(
                comment["id"] for comment in second_parse
            )
        )
        self.assertTrue(
            all(comment["source"] == "suppressed" for comment in first_parse)
        )
        self.assertTrue(all(comment["thread_id"] is None for comment in first_parse))

    def test_latest_copilot_review_uses_highest_review_id(self):
        reviews = [
            {
                "id": 100,
                "user": {"login": "copilot-pull-request-reviewer[bot]"},
            },
            {"id": 999, "user": {"login": "human"}},
            {
                "id": 101,
                "user": {"login": "copilot-pull-request-reviewer[bot]"},
            },
        ]

        self.assertEqual(MODULE.latest_copilot_review(reviews, None)["id"], 101)

    def test_latest_head_review_requires_matching_commit_and_completed_state(self):
        reviews = [
            {
                "id": 100,
                "commit_id": "old-head",
                "submitted_at": "2026-08-09T12:00:00Z",
                "state": "COMMENTED",
                "user": {"login": "copilot-pull-request-reviewer[bot]"},
            },
            {
                "id": 101,
                "commit_id": "head",
                "submitted_at": None,
                "state": "PENDING",
                "user": {"login": "copilot-pull-request-reviewer[bot]"},
            },
            {
                "id": 102,
                "commit_id": "head",
                "submitted_at": "2026-08-09T12:02:00Z",
                "state": "DISMISSED",
                "user": {"login": "copilot-pull-request-reviewer[bot]"},
            },
            {
                "id": 103,
                "commit_id": "head",
                "submitted_at": "2026-08-09T12:03:00Z",
                "state": "COMMENTED",
                "user": {"login": "copilot-pull-request-reviewer[bot]"},
            },
        ]

        self.assertEqual(
            MODULE.latest_copilot_review_for_head(reviews, None, "head")["id"],
            103,
        )


class CheckoutHeadTest(unittest.TestCase):
    def test_accepts_exact_pr_head(self):
        with mock.patch.object(MODULE, "run") as run:
            MODULE.verify_checkout_head(Path("repo"), "abc123", "abc123")

        run.assert_not_called()

    def test_accepts_local_head_ahead_of_pr(self):
        completed = mock.Mock(returncode=0)
        with mock.patch.object(MODULE, "run", return_value=completed) as run:
            MODULE.verify_checkout_head(Path("repo"), "local123", "remote123")

        self.assertEqual(run.call_args.kwargs, {"check": False})
        self.assertEqual(
            run.call_args.args[0][-4:],
            ["merge-base", "--is-ancestor", "remote123", "local123"],
        )

    def test_rejects_local_head_not_descended_from_pr(self):
        completed = mock.Mock(returncode=1, stderr="", stdout="")
        with mock.patch.object(MODULE, "run", return_value=completed):
            with self.assertRaisesRegex(MODULE.WorkflowError, "HEAD mismatch"):
                MODULE.verify_checkout_head(Path("repo"), "local123", "remote123")

    def test_keeps_the_existing_pr_branch_checked_out(self):
        target = {"pr_url": "https://github.com/owner/repo/pull/7"}
        metadata = {"head_branch": "feature", "head_sha": "remote123"}

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

    def test_checks_out_the_remote_pr_head_when_on_another_branch(self):
        target = {"pr_url": "https://github.com/owner/repo/pull/7"}
        metadata = {"head_branch": "feature", "head_sha": "remote123"}

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
        metadata = {"head_branch": "feature", "head_sha": "remote123"}
        error = MODULE.WorkflowError("authentication failed")

        with (
            mock.patch.object(MODULE, "git", return_value="feature"),
            mock.patch.object(MODULE, "run", side_effect=error),
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "authentication failed"):
                MODULE.checkout_pr(Path("repo"), target, metadata)


class RemoteParsingTest(unittest.TestCase):
    def test_parses_https_and_ssh_remotes(self):
        self.assertEqual(
            MODULE.github_repo_from_remote("https://github.com/trask/repo.git"),
            "trask/repo",
        )
        self.assertEqual(
            MODULE.github_repo_from_remote("git@github.com:trask/repo.git"),
            "trask/repo",
        )
        self.assertEqual(
            MODULE.github_repo_from_remote(
                "ssh://git@github.com:22/fork-owner/repo.git"
            ),
            "fork-owner/repo",
        )
        self.assertEqual(
            MODULE.github_repo_from_remote("git://github.com/trask/repo"),
            "trask/repo",
        )

    def test_rejects_non_github_and_malformed_remotes(self):
        self.assertIsNone(
            MODULE.github_repo_from_remote("https://example.com/trask/repo.git")
        )
        self.assertIsNone(
            MODULE.github_repo_from_remote("https://notgithub.com/trask/repo.git")
        )

    def test_rejects_upstream_owned_pr_head(self):
        pr = {
            "upstream_owner": "open-telemetry",
            "upstream_repo": "repo",
            "head_owner": "open-telemetry",
            "head_repo": "repo",
        }

        with self.assertRaisesRegex(MODULE.WorkflowError, "refusing to push"):
            MODULE.require_fork_head(pr, "abc123")

    def test_allows_upstream_owned_pr_head_when_branch_exists(self):
        pr = {
            "upstream_owner": "open-telemetry",
            "upstream_repo": "repo",
            "head_owner": "open-telemetry",
            "head_repo": "repo",
            "head_branch": "topic",
        }

        MODULE.require_fork_head(pr, "abc123")

    def test_rejects_upstream_owned_pr_head_when_branch_missing(self):
        pr = {
            "upstream_owner": "open-telemetry",
            "upstream_repo": "repo",
            "head_owner": "open-telemetry",
            "head_repo": "repo",
            "head_branch": "topic",
        }

        with self.assertRaisesRegex(MODULE.WorkflowError, "refusing to push"):
            MODULE.require_fork_head(pr, None)

    def test_waits_for_the_pushed_ref_to_propagate(self):
        with (
            mock.patch.object(
                MODULE,
                "remote_head",
                side_effect=["old-head", "old-head", "new-head"],
            ) as remote_head,
            mock.patch.object(MODULE.time, "sleep") as sleep,
        ):
            result = MODULE.wait_for_remote_head("owner", "repo", "branch", "new-head")

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
            result = MODULE.wait_for_remote_head("owner", "repo", "branch", "new-head")

        self.assertEqual(result, "old-head")
        self.assertEqual(
            remote_head.call_count, len(MODULE.REMOTE_REF_LAG_RETRY_DELAYS) + 1
        )
        self.assertEqual(sleep.call_count, len(MODULE.REMOTE_REF_LAG_RETRY_DELAYS))


class RecordCommitTest(unittest.TestCase):
    def test_requires_the_recorded_sha_to_resolve_to_a_commit(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "repo_root": "repo",
            "queue": {
                "status": "active",
                "comments": [{"id": 10, "status": "pending"}],
                "batches": [{"id": "batch-1", "status": "planned"}],
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            reply_path = Path(directory) / "reply.txt"
            MODULE.save_state(state_path, state)
            reply_path.write_text("Applied the fix.", encoding="utf-8")
            args = SimpleNamespace(
                state=str(state_path),
                comments=[10],
                reply_file=str(reply_path),
                commit="f" * 40,
                batch="batch-1",
                rationale=None,
                summary="Fix the issue",
            )

            with (
                mock.patch.object(
                    MODULE,
                    "git",
                    side_effect=MODULE.WorkflowError("unknown revision"),
                ) as git,
                self.assertRaisesRegex(
                    MODULE.WorkflowError,
                    f"recorded commit does not exist or is not a commit: {'f' * 40}",
                ),
            ):
                MODULE.command_record(args)

            saved = MODULE.load_state(state_path)

        git.assert_called_once_with(
            Path("repo"),
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{'f' * 40}^{{commit}}",
        )
        self.assertEqual(saved["queue"]["comments"][0]["status"], "pending")
        self.assertEqual(saved["queue"]["batches"][0]["status"], "planned")

    def test_records_the_canonical_verified_commit_sha(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "repo_root": "repo",
            "queue": {
                "status": "active",
                "comments": [{"id": 10, "status": "pending"}],
                "batches": [{"id": "batch-1", "status": "planned"}],
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            reply_path = Path(directory) / "reply.txt"
            MODULE.save_state(state_path, state)
            reply_path.write_text("Applied the fix.", encoding="utf-8")
            args = SimpleNamespace(
                state=str(state_path),
                comments=[10],
                reply_file=str(reply_path),
                commit="HEAD",
                batch="batch-1",
                rationale=None,
                summary="Fix the issue",
            )

            with (
                mock.patch.object(MODULE, "git", return_value="a" * 40),
                mock.patch.object(MODULE, "emit"),
            ):
                MODULE.command_record(args)

            saved = MODULE.load_state(state_path)

        self.assertEqual(saved["queue"]["comments"][0]["commit"], "a" * 40)


class ReplyPublishingTest(unittest.TestCase):
    def test_reply_body_uses_model_authored_text(self):
        reply = "Analysis: The guard is needed.\n\nUpsides: Safer.\n\nDownsides: None."

        self.assertEqual(
            MODULE.reply_body({"commit": "abc123", "reply": reply}),
            f"Addressed in abc123.\n\n{reply}",
        )
        self.assertEqual(
            MODULE.reply_body({"commit": None, "reply": reply}),
            f"No code change.\n\n{reply}",
        )

    def test_posts_each_reply_as_its_own_published_comment(self):
        state = {
            "pr": {
                "upstream_owner": "open-telemetry",
                "upstream_repo": "repo",
                "number": 42,
            }
        }
        comments = [
            {
                "id": 10,
                "thread_id": "THREAD_1",
                "commit": "abc123",
                "reply": "Analysis: Applied the requested change.",
            },
            {
                "id": 20,
                "thread_id": "THREAD_2",
                "commit": None,
                "reply": "Analysis: The existing behavior is intentional.",
            },
        ]

        def fake_gh_json(arguments, input_payload=None):
            if arguments == ["api", "user"]:
                return {"login": "author"}
            self.assertIsNotNone(input_payload)
            return {"id": 11 if "/10/replies" in arguments[-1] else 21}

        with (
            mock.patch.object(MODULE, "fetch_review_comments", return_value=[]),
            mock.patch.object(MODULE, "gh_json", side_effect=fake_gh_json) as gh_json,
            mock.patch.object(MODULE, "graphql") as graphql,
        ):
            reply_ids = MODULE.post_missing_replies(state, comments)

        self.assertEqual(reply_ids, {10: 11, 20: 21})
        self.assertEqual(comments[0]["reply_id"], 11)
        self.assertEqual(comments[1]["reply_id"], 21)
        # A single bundled review is never created for the replies.
        graphql.assert_not_called()
        posts = [
            call for call in gh_json.call_args_list if call.args[0] != ["api", "user"]
        ]
        self.assertEqual(
            [call.args[0] for call in posts],
            [
                [
                    "api",
                    "--method",
                    "POST",
                    "--input",
                    "-",
                    "repos/open-telemetry/repo/pulls/42/comments/10/replies",
                ],
                [
                    "api",
                    "--method",
                    "POST",
                    "--input",
                    "-",
                    "repos/open-telemetry/repo/pulls/42/comments/20/replies",
                ],
            ],
        )
        self.assertEqual(
            [call.kwargs["input_payload"] for call in posts],
            [
                {
                    "body": "Addressed in abc123.\n\n"
                    "Analysis: Applied the requested change."
                },
                {
                    "body": "No code change.\n\n"
                    "Analysis: The existing behavior is intentional."
                },
            ],
        )

    def test_reuses_an_existing_identical_reply(self):
        state = {
            "pr": {
                "upstream_owner": "open-telemetry",
                "upstream_repo": "repo",
                "number": 42,
            }
        }
        comment = {
            "id": 10,
            "thread_id": "THREAD_1",
            "commit": "abc123",
            "reply": "Analysis: Applied the requested change.",
        }
        existing = [
            {
                "id": 11,
                "in_reply_to_id": 10,
                "user": {"login": "author"},
                "body": "Addressed in abc123.\n\nAnalysis: Applied the requested change.",
            }
        ]

        with (
            mock.patch.object(MODULE, "fetch_review_comments", return_value=existing),
            mock.patch.object(
                MODULE, "gh_json", return_value={"login": "author"}
            ) as gh_json,
        ):
            reply_ids = MODULE.post_missing_replies(state, [comment])

        self.assertEqual(reply_ids, {10: 11})
        gh_json.assert_called_once_with(["api", "user"])

    def test_reconciles_partial_outdated_threads_reply_first_and_checkpoints(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "pr": {
                "upstream_owner": "open-telemetry",
                "upstream_repo": "shared-workflows",
                "number": 377,
            }
        }
        comments = [
            {
                "id": 4021507173,
                "thread_id": "PRRT_kwDOTENyc86iv3hz",
                "source": "thread",
                "resolved": False,
                "commit": "571bade3904ff473283e6b9da95853e712fe6c8a",
                "reply": "Applied the pending-run checkpoint fix.",
            },
            {
                "id": 4021507189,
                "thread_id": "PRRT_kwDOTENyc86iv3iA",
                "source": "thread",
                "resolved": True,
                "commit": "c546c4902433040a05262cb22fa5587ae829de62",
                "reply": "Removed the redundant orphan-worktree command.",
            },
        ]
        published: list[dict[str, Any]] = []
        events: list[str] = []

        def fake_gh_json(arguments, input_payload=None):
            if arguments == ["api", "user"]:
                return {"login": "trask"}
            comment_id = int(arguments[-1].split("/")[-2])
            if comment_id == 4021507189:
                self.assertTrue(
                    state["thread_mutations"][str(comment_id)]["resolved"]
                )
            reply = {
                "id": 9000 + comment_id,
                "in_reply_to_id": comment_id,
                "user": {"login": "trask"},
                "body": input_payload["body"],
            }
            published.append(reply)
            events.append(f"reply:{comment_id}")
            return reply

        def fake_graphql(_query, variables):
            events.append(f"resolve:{variables['thread']}")
            return {
                "data": {
                    "resolveReviewThread": {
                        "thread": {
                            "id": variables["thread"],
                            "isResolved": True,
                        }
                    }
                }
            }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            MODULE.save_state(state_path, state)
            with (
                mock.patch.object(
                    MODULE,
                    "fetch_review_comments",
                    side_effect=lambda *_args: list(published),
                ),
                mock.patch.object(MODULE, "gh_json", side_effect=fake_gh_json),
                mock.patch.object(MODULE, "graphql", side_effect=fake_graphql),
            ):
                reply_ids = MODULE.post_missing_replies(
                    state, comments, state_path=state_path
                )
                MODULE.resolve_threads(
                    comments, state=state, state_path=state_path
                )

                recovered = MODULE.load_state(state_path)
                repeated = [dict(comment) for comment in comments]
                MODULE.post_missing_replies(
                    recovered, repeated, state_path=state_path
                )
                MODULE.resolve_threads(
                    repeated, state=recovered, state_path=state_path
                )

        self.assertEqual(
            reply_ids,
            {4021507173: 4021516173, 4021507189: 4021516189},
        )
        self.assertEqual(
            events,
            [
                "reply:4021507173",
                "reply:4021507189",
                "resolve:PRRT_kwDOTENyc86iv3hz",
            ],
        )
        self.assertTrue(
            state["thread_mutations"]["4021507173"]["resolved"]
        )
        self.assertTrue(
            state["thread_mutations"]["4021507189"]["resolved"]
        )

    def test_reply_interruption_retains_complete_plan_and_finished_side_effect(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "pr": {
                "upstream_owner": "open-telemetry",
                "upstream_repo": "shared-workflows",
                "number": 377,
            },
        }
        comments = [
            {
                "id": 4021507173,
                "thread_id": "PRRT_kwDOTENyc86iv3hz",
                "source": "thread",
                "commit": "571bade3904ff473283e6b9da95853e712fe6c8a",
                "reply": "Applied the pending-run checkpoint fix.",
            },
            {
                "id": 4021507189,
                "thread_id": "PRRT_kwDOTENyc86iv3iA",
                "source": "thread",
                "commit": "c546c4902433040a05262cb22fa5587ae829de62",
                "reply": "Removed the redundant orphan-worktree command.",
            },
        ]
        posts = 0

        def fake_gh_json(arguments, input_payload=None):
            nonlocal posts
            if arguments == ["api", "user"]:
                return {"login": "trask"}
            posts += 1
            if posts == 2:
                raise MODULE.WorkflowError("reply transport interrupted")
            return {
                "id": 4021516173,
                "in_reply_to_id": 4021507173,
                "user": {"login": "trask"},
                "body": input_payload["body"],
            }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            MODULE.save_state(state_path, state)
            with (
                mock.patch.object(MODULE, "fetch_review_comments", return_value=[]),
                mock.patch.object(MODULE, "gh_json", side_effect=fake_gh_json),
                self.assertRaisesRegex(
                    MODULE.WorkflowError, "reply transport interrupted"
                ),
            ):
                MODULE.post_missing_replies(
                    state, comments, state_path=state_path
                )
            recovered = MODULE.load_state(state_path)

        self.assertEqual(
            set(recovered["thread_mutations"]),
            {"4021507173", "4021507189"},
        )
        self.assertEqual(
            recovered["thread_mutations"]["4021507173"]["reply_id"],
            4021516173,
        )
        self.assertIsNone(
            recovered["thread_mutations"]["4021507189"]["reply_id"]
        )

    def test_rejects_a_reply_without_a_numeric_comment_id(self):
        state = {
            "pr": {
                "upstream_owner": "open-telemetry",
                "upstream_repo": "repo",
                "number": 42,
            }
        }
        comment = {
            "id": 10,
            "thread_id": "THREAD_1",
            "commit": "abc123",
            "reply": "Analysis: Applied the requested change.",
        }

        def fake_gh_json(arguments, input_payload=None):
            del input_payload
            if arguments == ["api", "user"]:
                return {"login": "author"}
            return {}

        with (
            mock.patch.object(MODULE, "fetch_review_comments", return_value=[]),
            mock.patch.object(MODULE, "gh_json", side_effect=fake_gh_json),
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "returned no numeric comment ID"
            ):
                MODULE.post_missing_replies(state, [comment])

    def test_suppressed_comments_get_no_reply_or_resolution(self):
        comment = {
            "id": -100001,
            "source": "suppressed",
            "thread_id": None,
            "commit": "abc123",
            "reply": "Analysis: Applied the requested change.",
        }
        state = {"pr": {}}

        with (
            mock.patch.object(MODULE, "fetch_review_comments") as fetch_comments,
            mock.patch.object(MODULE, "graphql") as graphql,
        ):
            self.assertEqual(MODULE.post_missing_replies(state, [comment]), {})
            MODULE.resolve_threads([comment])

        fetch_comments.assert_not_called()
        graphql.assert_not_called()

    def test_publishes_empty_follow_up_without_reply_operations(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "repo_root": "repo",
            "pr": {
                "head_owner": "author",
                "head_repo": "repo",
                "head_branch": "branch",
                "head_sha": "old-head",
            },
            "queue": {
                "id": "pr-42",
                "comments": [],
                "status": "active",
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            MODULE.save_state(state_path, state)
            args = SimpleNamespace(
                state=str(state_path),
                no_comments=True,
                validated=None,
                not_validated=None,
                rewrote=None,
            )

            def fake_git(repo_root, *arguments):
                del repo_root
                return {
                    ("status", "--porcelain=v1"): "",
                    ("rev-parse", "HEAD"): "new-head",
                }[arguments]

            with (
                mock.patch.object(MODULE, "git", side_effect=fake_git),
                mock.patch.object(MODULE, "require_fork_head"),
                mock.patch.object(MODULE, "find_push_remote", return_value="origin"),
                mock.patch.object(
                    MODULE,
                    "remote_head",
                    side_effect=["old-head", "old-head", "new-head"],
                ),
                mock.patch.object(MODULE.time, "sleep") as sleep,
                mock.patch.object(MODULE, "run") as run,
                mock.patch.object(MODULE, "post_missing_replies") as post_replies,
                mock.patch.object(MODULE, "resolve_threads") as resolve_threads,
                mock.patch.object(
                    MODULE,
                    "request_copilot",
                    return_value={"status": "requested"},
                ),
                mock.patch.object(MODULE, "verify_publish", return_value={}),
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_publish(args)

        run.assert_called_once_with(
            ["git", "-C", "repo", "push", "origin", "HEAD:branch"]
        )
        sleep.assert_called_once_with(MODULE.REMOTE_REF_LAG_RETRY_DELAYS[0])
        post_replies.assert_not_called()
        resolve_threads.assert_not_called()
        self.assertEqual(emit.call_args.args[0]["reply_ids"], {})

    def test_reports_remote_head_divergence_without_pushing(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "repo_root": "repo",
            "pr": {
                "head_owner": "author",
                "head_repo": "repo",
                "head_branch": "branch",
                "head_sha": "old-head",
            },
            "queue": {
                "id": "pr-42",
                "comments": [],
                "status": "active",
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            MODULE.save_state(state_path, state)
            args = SimpleNamespace(
                state=str(state_path),
                no_comments=True,
                validated=None,
                not_validated=None,
                rewrote=None,
            )

            def fake_git(repo_root, *arguments):
                del repo_root
                return {
                    ("status", "--porcelain=v1"): "",
                    ("rev-parse", "HEAD"): "local-head",
                }[arguments]

            with (
                mock.patch.object(MODULE, "git", side_effect=fake_git),
                mock.patch.object(MODULE, "require_fork_head"),
                mock.patch.object(
                    MODULE, "find_push_remote", return_value="origin"
                ) as find_remote,
                mock.patch.object(
                    MODULE, "remote_head", return_value="force-updated-head"
                ),
                mock.patch.object(MODULE, "run") as run,
                mock.patch.object(MODULE, "post_missing_replies") as post_replies,
                mock.patch.object(MODULE, "request_copilot") as request_copilot,
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_publish(args)

        run.assert_not_called()
        find_remote.assert_not_called()
        post_replies.assert_not_called()
        request_copilot.assert_not_called()
        emit.assert_called_once_with(
            {
                "result": "head_changed",
                "state": str(state_path.resolve()),
                "expected_head": "old-head",
                "actual_head": "force-updated-head",
                "local_head": "local-head",
            }
        )

    def test_reports_divergence_when_remote_moves_during_push(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "repo_root": "repo",
            "pr": {
                "head_owner": "author",
                "head_repo": "repo",
                "head_branch": "branch",
                "head_sha": "old-head",
            },
            "queue": {
                "id": "pr-42",
                "comments": [],
                "status": "active",
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            MODULE.save_state(state_path, state)
            args = SimpleNamespace(
                state=str(state_path),
                no_comments=True,
                validated=None,
                not_validated=None,
                rewrote=None,
            )

            def fake_git(repo_root, *arguments):
                del repo_root
                return {
                    ("status", "--porcelain=v1"): "",
                    ("rev-parse", "HEAD"): "local-head",
                }[arguments]

            with (
                mock.patch.object(MODULE, "git", side_effect=fake_git),
                mock.patch.object(MODULE, "require_fork_head"),
                mock.patch.object(MODULE, "find_push_remote", return_value="origin"),
                mock.patch.object(
                    MODULE,
                    "remote_head",
                    side_effect=["old-head", "force-updated-head"],
                ),
                mock.patch.object(
                    MODULE,
                    "run",
                    side_effect=MODULE.WorkflowError("fetch first"),
                ),
                mock.patch.object(MODULE, "post_missing_replies") as post_replies,
                mock.patch.object(MODULE, "request_copilot") as request_copilot,
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_publish(args)

        post_replies.assert_not_called()
        request_copilot.assert_not_called()
        emit.assert_called_once_with(
            {
                "result": "head_changed",
                "state": str(state_path.resolve()),
                "expected_head": "old-head",
                "actual_head": "force-updated-head",
                "local_head": "local-head",
            }
        )

    def test_records_the_local_validation_behind_the_push(self):
        """The state has to say what ran, or a live run proves nothing.

        Every publication here spends a Copilot review and a cycle of checks
        at once, so what ran before it is worth reading afterwards.
        """
        state = {
            "version": MODULE.STATE_VERSION,
            "iterations": 1,
            "repo_root": "repo",
            "pr": {
                "head_owner": "author",
                "head_repo": "repo",
                "head_branch": "branch",
                "head_sha": "old-head",
            },
            "queue": {"id": "pr-42", "comments": [], "status": "active"},
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            MODULE.save_state(state_path, state)
            args = SimpleNamespace(
                state=str(state_path),
                no_comments=True,
                validated=["check one"],
                not_validated=None,
                rewrote=["check one"],
            )

            def fake_git(repo_root, *arguments):
                del repo_root
                return {
                    ("status", "--porcelain=v1"): "",
                    ("rev-parse", "HEAD"): "new-head",
                }[arguments]

            with (
                mock.patch.object(MODULE, "git", side_effect=fake_git),
                mock.patch.object(MODULE, "require_fork_head"),
                mock.patch.object(MODULE, "find_push_remote", return_value="origin"),
                mock.patch.object(
                    MODULE, "remote_head", side_effect=["old-head", "new-head"]
                ),
                mock.patch.object(MODULE, "run"),
                mock.patch.object(
                    MODULE, "request_copilot", return_value={"status": "requested"}
                ),
                mock.patch.object(MODULE, "verify_publish", return_value={}),
                mock.patch.object(MODULE, "emit"),
            ):
                MODULE.command_publish(args)

            saved = MODULE.load_state(state_path)

        self.assertEqual(
            [
                {
                    "head_sha": "new-head",
                    "status": "passed",
                    "commands": ["check one"],
                    "rewrote": ["check one"],
                }
            ],
            saved["local_validation"],
        )

    def test_records_nothing_for_a_publication_that_pushes_no_commit(self):
        """A publication that only re-requests a review changes no code.

        There is nothing to validate, so an `unreported` entry there would be
        noise that hides the publications the record is actually watching.
        """
        state = {
            "version": MODULE.STATE_VERSION,
            "iterations": 1,
            "repo_root": "repo",
            "pr": {
                "head_owner": "author",
                "head_repo": "repo",
                "head_branch": "branch",
                "head_sha": "same-head",
            },
            "queue": {"id": "pr-42", "comments": [], "status": "active"},
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            MODULE.save_state(state_path, state)
            args = SimpleNamespace(
                state=str(state_path),
                no_comments=True,
                validated=None,
                not_validated=None,
                rewrote=None,
            )

            def fake_git(repo_root, *arguments):
                del repo_root
                return {
                    ("status", "--porcelain=v1"): "",
                    ("rev-parse", "HEAD"): "same-head",
                }[arguments]

            with (
                mock.patch.object(MODULE, "git", side_effect=fake_git),
                mock.patch.object(MODULE, "require_fork_head"),
                mock.patch.object(MODULE, "find_push_remote", return_value="origin"),
                mock.patch.object(MODULE, "remote_head", return_value="same-head"),
                mock.patch.object(MODULE, "run") as run,
                mock.patch.object(
                    MODULE, "request_copilot", return_value={"status": "requested"}
                ),
                mock.patch.object(MODULE, "verify_publish", return_value={}),
                mock.patch.object(MODULE, "emit"),
            ):
                MODULE.command_publish(args)

            saved = MODULE.load_state(state_path)

        run.assert_not_called()
        self.assertNotIn("local_validation", saved)

    def test_publishes_a_suppressed_only_queue(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "iterations": 2,
            "repo_root": "repo",
            "pr": {
                "head_owner": "author",
                "head_repo": "repo",
                "head_branch": "branch",
                "head_sha": "same-head",
            },
            "queue": {
                "id": "pr-42",
                "comments": [
                    {
                        "id": -100001,
                        "source": "suppressed",
                        "thread_id": None,
                        "status": "handled",
                        "commit": None,
                        "rationale": "No change is appropriate.",
                        "summary": "Kept the existing behavior.",
                        "reply": "Analysis: The existing behavior is intentional.",
                    }
                ],
                "status": "active",
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            MODULE.save_state(state_path, state)
            args = SimpleNamespace(
                state=str(state_path),
                no_comments=False,
                validated=None,
                not_validated=None,
                rewrote=None,
            )

            def fake_git(repo_root, *arguments):
                del repo_root
                return {
                    ("status", "--porcelain=v1"): "",
                    ("rev-parse", "HEAD"): "same-head",
                }[arguments]

            with (
                mock.patch.object(MODULE, "git", side_effect=fake_git),
                mock.patch.object(MODULE, "require_fork_head"),
                mock.patch.object(MODULE, "find_push_remote", return_value="origin"),
                mock.patch.object(MODULE, "remote_head", return_value="same-head"),
                mock.patch.object(MODULE, "run") as run,
                mock.patch.object(MODULE, "fetch_review_comments") as fetch_comments,
                mock.patch.object(MODULE, "graphql") as graphql,
                mock.patch.object(
                    MODULE,
                    "request_copilot",
                    return_value={"status": "requested"},
                ),
                mock.patch.object(MODULE, "verify_publish", return_value={}),
                mock.patch.object(MODULE, "emit"),
            ):
                MODULE.command_publish(args)

            saved = MODULE.load_state(state_path)

        run.assert_not_called()
        fetch_comments.assert_not_called()
        graphql.assert_not_called()
        self.assertEqual(saved["iterations"], 3)
        self.assertEqual(saved["queue"]["status"], "published")


class VerifyPublishTest(unittest.TestCase):
    STATE = {
        "repo_root": "repo",
        "pr": {
            "upstream_owner": "open-telemetry",
            "upstream_repo": "repo",
            "number": 42,
        },
        "monitoring": {"copilot_bot_id": "BOT_1"},
    }

    def run_verify(self, published_reply_ids):
        comment = {
            "id": 10,
            "source": "thread",
            "thread_id": "THREAD_1",
            "reply_id": 11,
        }
        threads = [
            {
                "id": "THREAD_1",
                "isResolved": True,
                "comments": {"nodes": [{"databaseId": 10}, {"databaseId": 11}]},
            }
        ]
        review_requests = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewRequests": {
                            "nodes": [{"requestedReviewer": {"id": "BOT_1"}}]
                        }
                    }
                }
            }
        }

        with (
            mock.patch.object(MODULE, "git", return_value="abc123"),
            mock.patch.object(
                MODULE, "gh_json", return_value={"head": {"sha": "abc123"}}
            ),
            mock.patch.object(MODULE, "fetch_threads", return_value=threads),
            mock.patch.object(
                MODULE,
                "fetch_review_comments",
                return_value=[{"id": item} for item in published_reply_ids],
            ),
            mock.patch.object(MODULE, "graphql", return_value=review_requests),
            mock.patch.object(MODULE, "fetch_reviews", return_value=[]),
        ):
            return MODULE.verify_publish(dict(self.STATE), [comment])

    def test_accepts_a_published_reply(self):
        result = self.run_verify([10, 11])

        self.assertEqual(
            result["threads"],
            [{"thread_id": "THREAD_1", "resolved": True, "reply_present": True}],
        )

    def test_rejects_a_reply_left_in_an_unsubmitted_review(self):
        # A pending reply is absent from the REST review comments listing.
        with self.assertRaisesRegex(
            MODULE.WorkflowError, "publishing verification failed"
        ):
            self.run_verify([10])

    def test_retries_pr_head_verification_after_publication(self):
        with (
            mock.patch.object(
                MODULE,
                "gh_json",
                side_effect=[
                    {"head": {"sha": "old-head"}},
                    {"head": {"sha": "abc123"}},
                ],
            ) as gh_json,
            mock.patch.object(MODULE.time, "sleep") as sleep,
        ):
            payload = MODULE.wait_for_pr_head(dict(self.STATE), "abc123")

        self.assertEqual(payload["head"]["sha"], "abc123")
        self.assertEqual(gh_json.call_count, 2)
        sleep.assert_called_once_with(MODULE.PR_HEAD_LAG_RETRY_DELAYS[0])

    def test_stops_retrying_pr_head_after_the_propagation_budget(self):
        with (
            mock.patch.object(
                MODULE, "gh_json", return_value={"head": {"sha": "old-head"}}
            ) as gh_json,
            mock.patch.object(MODULE.time, "sleep") as sleep,
        ):
            payload = MODULE.wait_for_pr_head(dict(self.STATE), "abc123")

        self.assertEqual(payload["head"]["sha"], "old-head")
        self.assertEqual(gh_json.call_count, len(MODULE.PR_HEAD_LAG_RETRY_DELAYS) + 1)
        self.assertEqual(sleep.call_count, len(MODULE.PR_HEAD_LAG_RETRY_DELAYS))


class RequestCopilotTest(unittest.TestCase):
    def test_retries_pr_head_mismatch_after_remote_head_is_confirmed(self):
        state = {
            "repo_root": "repo",
            "pr": {
                "upstream_owner": "owner",
                "upstream_repo": "repo",
                "number": 7,
                "pr_node_id": "PR_1",
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with (
                mock.patch.object(MODULE, "git", return_value="new-head"),
                mock.patch.object(MODULE, "resolve_copilot_bot", return_value="BOT_1"),
                mock.patch.object(MODULE, "fetch_reviews", return_value=[]),
                mock.patch.object(
                    MODULE,
                    "graphql",
                    side_effect=[
                        MODULE.WorkflowError("GraphQL failed: PR head mismatch"),
                        {"data": {}},
                    ],
                ) as graphql,
                mock.patch.object(MODULE.time, "sleep") as sleep,
            ):
                result = MODULE.request_copilot(state, path, "new-head")

        self.assertEqual(result["status"], "requested")
        self.assertEqual(graphql.call_count, 2)
        sleep.assert_called_once_with(MODULE.PR_HEAD_LAG_RETRY_DELAYS[0])

    def test_does_not_retry_pr_head_mismatch_without_confirmed_remote_head(self):
        state = {
            "repo_root": "repo",
            "pr": {
                "upstream_owner": "owner",
                "upstream_repo": "repo",
                "number": 7,
                "pr_node_id": "PR_1",
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with (
                mock.patch.object(MODULE, "git", return_value="new-head"),
                mock.patch.object(MODULE, "resolve_copilot_bot", return_value="BOT_1"),
                mock.patch.object(MODULE, "fetch_reviews", return_value=[]),
                mock.patch.object(
                    MODULE,
                    "graphql",
                    side_effect=MODULE.WorkflowError(
                        "GraphQL failed: PR head mismatch"
                    ),
                ) as graphql,
                mock.patch.object(MODULE.time, "sleep") as sleep,
                self.assertRaisesRegex(MODULE.WorkflowError, "PR head mismatch"),
            ):
                MODULE.request_copilot(state, path, "old-head")

        graphql.assert_called_once()
        sleep.assert_not_called()


class FirstCopilotReviewTest(unittest.TestCase):
    PR = {"upstream_owner": "owner", "upstream_repo": "repo", "number": 7}

    def test_uses_the_reviewer_alias_on_a_supported_cli(self):
        command = MODULE.copilot_request_command(self.PR, alias_supported=True)

        self.assertEqual(
            command,
            [
                "gh",
                "pr",
                "edit",
                "7",
                "--repo",
                "owner/repo",
                "--add-reviewer",
                "@copilot",
            ],
        )

    def test_falls_back_to_the_rest_endpoint_on_an_older_cli(self):
        command = MODULE.copilot_request_command(self.PR, alias_supported=False)

        self.assertEqual(
            command,
            [
                "gh",
                "api",
                "--method",
                "POST",
                "repos/owner/repo/pulls/7/requested_reviewers",
                "-f",
                "reviewers[]=copilot-pull-request-reviewer[bot]",
            ],
        )

    def test_reads_the_cli_version(self):
        with mock.patch.object(
            MODULE,
            "run",
            return_value=SimpleNamespace(stdout="gh version 2.88.0 (2026-01-01)\n"),
        ):
            self.assertEqual(MODULE.gh_version(), (2, 88, 0))

    def test_rejects_an_unreadable_cli_version(self):
        with (
            mock.patch.object(MODULE, "run", return_value=SimpleNamespace(stdout="")),
            self.assertRaisesRegex(
                MODULE.WorkflowError, "could not read the GitHub CLI version"
            ),
        ):
            MODULE.gh_version()

    def test_the_alias_boundary_is_the_supported_cli_version(self):
        self.assertLess((2, 87, 9), MODULE.GH_REVIEWER_ALIAS_VERSION)
        self.assertGreaterEqual((2, 88, 0), MODULE.GH_REVIEWER_ALIAS_VERSION)
        self.assertGreaterEqual((3, 0, 0), MODULE.GH_REVIEWER_ALIAS_VERSION)

    def test_requests_the_first_review_when_the_pr_has_none(self):
        state = {"pr": dict(self.PR)}

        with (
            mock.patch.object(
                MODULE, "lookup_copilot_bot", side_effect=[None, "BOT_1"]
            ),
            mock.patch.object(MODULE, "gh_version", return_value=(2, 88, 0)),
            mock.patch.object(
                MODULE, "run", return_value=SimpleNamespace(returncode=0)
            ) as run,
            mock.patch.object(MODULE.time, "sleep") as sleep,
        ):
            bot_id = MODULE.resolve_copilot_bot(state)

        self.assertEqual(bot_id, "BOT_1")
        self.assertEqual(state["copilot_bot_id"], "BOT_1")
        self.assertIn("@copilot", run.call_args.args[0])
        sleep.assert_not_called()

    def test_never_requests_a_review_when_the_bot_is_already_known(self):
        state = {"pr": dict(self.PR)}

        with (
            mock.patch.object(MODULE, "lookup_copilot_bot", return_value="BOT_1"),
            mock.patch.object(MODULE, "run") as run,
        ):
            bot_id = MODULE.resolve_copilot_bot(state)

        self.assertEqual(bot_id, "BOT_1")
        run.assert_not_called()

    def test_waits_for_the_request_to_appear_on_the_pull_request(self):
        with (
            mock.patch.object(
                MODULE, "lookup_copilot_bot", side_effect=[None, None, "BOT_1"]
            ),
            mock.patch.object(MODULE, "gh_version", return_value=(2, 88, 0)),
            mock.patch.object(
                MODULE, "run", return_value=SimpleNamespace(returncode=0)
            ),
            mock.patch.object(MODULE.time, "sleep") as sleep,
        ):
            bot_id = MODULE.request_first_copilot_review(dict(self.PR))

        self.assertEqual(bot_id, "BOT_1")
        self.assertEqual(
            sleep.call_args_list,
            [
                mock.call(MODULE.COPILOT_REQUEST_RETRY_DELAYS[0]),
                mock.call(MODULE.COPILOT_REQUEST_RETRY_DELAYS[1]),
            ],
        )

    def test_rejects_a_clean_exit_that_changed_nothing(self):
        with (
            mock.patch.object(MODULE, "lookup_copilot_bot", return_value=None),
            mock.patch.object(MODULE, "gh_version", return_value=(2, 88, 0)),
            mock.patch.object(
                MODULE, "run", return_value=SimpleNamespace(returncode=0)
            ),
            mock.patch.object(MODULE.time, "sleep") as sleep,
            self.assertRaisesRegex(
                MODULE.WorkflowError, "still lists no Copilot reviewer"
            ),
        ):
            MODULE.request_first_copilot_review(dict(self.PR))

        self.assertEqual(sleep.call_count, len(MODULE.COPILOT_REQUEST_RETRY_DELAYS))

    def test_reports_the_failure_detail_when_the_request_is_rejected(self):
        with (
            mock.patch.object(MODULE, "lookup_copilot_bot", return_value=None),
            mock.patch.object(MODULE, "gh_version", return_value=(2, 87, 0)),
            mock.patch.object(
                MODULE,
                "run",
                return_value=SimpleNamespace(
                    returncode=1,
                    stderr="HTTP 422: Reviews may only be requested\n",
                    stdout="",
                ),
            ),
            self.assertRaisesRegex(
                MODULE.WorkflowError,
                "requesting the first Copilot review failed: HTTP 422",
            ),
        ):
            MODULE.request_first_copilot_review(dict(self.PR))

    def test_starts_watching_before_the_first_review_is_requested(self):
        """The baseline and timestamp must precede the bootstrap that triggers a review."""
        state = {
            "repo_root": "repo",
            "pr": {
                "upstream_owner": "owner",
                "upstream_repo": "repo",
                "number": 7,
                "pr_node_id": "PR_1",
            },
        }
        order: list[str] = []
        stamps = iter([f"2026-05-01T12:00:{second:02d}Z" for second in range(30)])

        def stamp():
            order.append("utc_now")
            return next(stamps)

        def fetch(*arguments):
            del arguments
            order.append("fetch_reviews")
            return []

        def resolve(bot_state):
            order.append("resolve_copilot_bot")
            bot_state["copilot_bot_id"] = "BOT_1"
            return "BOT_1"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with (
                mock.patch.object(MODULE, "git", return_value="head"),
                mock.patch.object(MODULE, "utc_now", side_effect=stamp),
                mock.patch.object(MODULE, "resolve_copilot_bot", side_effect=resolve),
                mock.patch.object(MODULE, "fetch_reviews", side_effect=fetch),
                mock.patch.object(MODULE, "graphql", return_value={"data": {}}),
            ):
                monitoring = MODULE.request_copilot(state, path, "head")

        self.assertEqual(monitoring["request_start"], "2026-05-01T12:00:00Z")
        self.assertEqual(monitoring["baseline_review_id"], 0)
        self.assertEqual(monitoring["copilot_bot_id"], "BOT_1")
        self.assertEqual(order[0], "utc_now")
        self.assertLess(
            order.index("fetch_reviews"), order.index("resolve_copilot_bot")
        )

    def test_the_baseline_covers_a_review_from_before_the_bootstrap(self):
        state = {
            "repo_root": "repo",
            "pr": {
                "upstream_owner": "owner",
                "upstream_repo": "repo",
                "number": 7,
                "pr_node_id": "PR_1",
            },
        }
        reviews = [
            {"id": 101, "user": {"login": "copilot-pull-request-reviewer[bot]"}},
            {"id": 102, "user": {"login": "reviewer"}},
        ]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with (
                mock.patch.object(MODULE, "git", return_value="head"),
                mock.patch.object(MODULE, "resolve_copilot_bot", return_value="BOT_1"),
                mock.patch.object(MODULE, "fetch_reviews", return_value=reviews),
                mock.patch.object(MODULE, "graphql", return_value={"data": {}}),
            ):
                monitoring = MODULE.request_copilot(state, path, "head")

        self.assertEqual(monitoring["baseline_review_id"], 101)


class CleanAtHeadShaTest(unittest.TestCase):
    """The marker an external orchestrator reads to see whether this stage is green."""

    def test_preflight_records_a_clean_head_with_no_unresolved_comments(self):
        review = {
            "id": 10,
            "commit_id": "head",
            "submitted_at": "2026-08-09T12:00:00Z",
            "state": "COMMENTED",
            "body": "No comments.",
            "user": {"login": "copilot-pull-request-reviewer[bot]"},
        }

        payload, saved = self.run_preflight(reviews=[review])

        self.assertEqual(payload["result"], "no_unresolved_comments")
        self.assertEqual(payload["clean_at_head_sha"], "head")
        self.assertEqual(saved["clean_at_head_sha"], "head")
        self.assertEqual(MODULE.stage_outcome(saved), "cleared")

    def test_preflight_records_a_clean_head_with_only_human_threads(self):
        review = {
            "id": 10,
            "commit_id": "head",
            "submitted_at": "2026-08-09T12:00:00Z",
            "state": "COMMENTED",
            "body": "No comments.",
            "user": {"login": "copilot-pull-request-reviewer[bot]"},
        }
        thread = {
            "id": "thread-1",
            "isResolved": False,
            "comments": {
                "nodes": [
                    {
                        "databaseId": 1,
                        "author": {"login": "reviewer"},
                        "pullRequestReview": {"databaseId": 5},
                    }
                ]
            },
        }

        payload, saved = self.run_preflight(threads=[thread], reviews=[review])

        self.assertEqual(payload["result"], "no_copilot_comments")
        self.assertEqual(payload["clean_at_head_sha"], "head")
        self.assertEqual(saved["clean_at_head_sha"], "head")

    def test_preflight_leaves_no_marker_when_the_head_needs_a_review(self):
        payload, saved = self.run_preflight()

        self.assertEqual(payload["result"], "review_required")
        self.assertIsNone(payload["clean_at_head_sha"])
        self.assertIsNone(saved["clean_at_head_sha"])
        self.assertEqual(saved["last_result"], "review_required")
        # `preflight` writes this before any work, so it is not an ending. The
        # run is owed one from the agent, and reading it as `escalated` is the
        # #19517 false ending that discarded an unpushed fix.
        self.assertIsNone(MODULE.stage_outcome(saved))

    def test_preflight_clears_a_stale_marker_from_an_earlier_clean_head(self):
        thread = {
            "id": "thread-1",
            "isResolved": False,
            "comments": {
                "nodes": [
                    {
                        "databaseId": 1,
                        "url": "https://example.test/1",
                        "body": "Fix this.",
                        "author": {
                            "login": "copilot-pull-request-reviewer[bot]",
                            "id": "BOT_1",
                        },
                        "pullRequestReview": {"databaseId": 5},
                    }
                ]
            },
        }

        payload, saved = self.run_preflight(
            threads=[thread], prior_clean_at_head_sha="older-head"
        )

        self.assertEqual(payload["result"], "ready")
        self.assertIsNone(payload["clean_at_head_sha"])
        self.assertIsNone(saved["clean_at_head_sha"])
        # The #19517 scenario built through real preflight: Copilot left comments,
        # so the run is owed an ending from the agent. `stage_outcome` must defer
        # rather than manufacture `escalated`, which is the false ending that
        # overrode the live agent and discarded its unpushed fix commit.
        self.assertEqual(saved["last_result"], "ready")
        self.assertIsNone(MODULE.stage_outcome(saved))

    def run_preflight(
        self, *, threads=None, reviews=None, prior_clean_at_head_sha=None
    ):
        metadata = {"head_branch": "branch", "head_sha": "head", "base_sha": "base"}

        def fake_git(repo_root, *arguments):
            del repo_root
            return {
                ("status", "--porcelain=v1"): "",
                ("branch", "--show-current"): "branch",
                ("rev-parse", "HEAD"): "head",
            }[arguments]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            if prior_clean_at_head_sha:
                MODULE.save_state(
                    path,
                    {
                        "version": MODULE.STATE_VERSION,
                        "clean_at_head_sha": prior_clean_at_head_sha,
                        "queue": {"comments": [], "batches": []},
                    },
                )
            args = SimpleNamespace(
                target="owner/repo#7",
                repo_root=directory,
                state=str(path),
                max_iterations=5,
                completed_run_iterations=0,
            )

            with (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(
                    MODULE, "resolve_repo_root", return_value=Path(directory)
                ),
                mock.patch.object(MODULE, "git", side_effect=fake_git),
                mock.patch.object(MODULE, "metadata_for", return_value=metadata),
                mock.patch.object(MODULE, "checkout_pr", return_value=True),
                mock.patch.object(MODULE, "run"),
                mock.patch.object(MODULE, "fetch_threads", return_value=threads or []),
                mock.patch.object(MODULE, "fetch_reviews", return_value=reviews or []),
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_preflight(args)

            return emit.call_args.args[0], MODULE.load_state(path)

    def test_watch_records_a_clean_head_when_the_review_asks_for_nothing(self):
        payload, saved = self.run_watch(review_comments=[], body="No comments.")

        self.assertEqual(payload["result"], "review_no_comments")
        self.assertEqual(payload["clean_at_head_sha"], "head")
        self.assertEqual(saved["clean_at_head_sha"], "head")

    def test_watch_leaves_no_marker_when_the_review_asks_for_something(self):
        payload, saved = self.run_watch(
            review_comments=[{"id": 5}], body="No comments."
        )

        self.assertEqual(payload["result"], "review_comments")
        self.assertIsNone(payload["clean_at_head_sha"])
        self.assertIsNone(saved.get("clean_at_head_sha"))

    def test_watch_leaves_no_marker_for_a_suppressed_only_review(self):
        body = """
<details><summary>Suppressed comments (1)</summary>
**a.java:1**
* Fix this.
</details>
"""

        payload, saved = self.run_watch(review_comments=[], body=body)

        self.assertEqual(payload["result"], "review_comments")
        self.assertIsNone(payload["clean_at_head_sha"])
        self.assertIsNone(saved.get("clean_at_head_sha"))

    def test_watch_routes_ccr_v2_body_only_feedback_without_clean_marker(self):
        review = json.loads(CCR_V2_REVIEW.read_text(encoding="utf-8"))
        payload, saved = self.run_watch(review_comments=[], body=review["body"])
        self.assertEqual(payload["result"], "review_comments")
        self.assertEqual(payload["comment_ids"], [])
        self.assertEqual(payload["suppressed_comment_count"], 1)
        self.assertIsNone(payload["clean_at_head_sha"])
        self.assertIsNone(saved.get("clean_at_head_sha"))

    def test_watch_routes_legacy_review_details_without_clean_marker(self):
        review = json.loads(LEGACY_REVIEW_DETAILS.read_text(encoding="utf-8"))
        payload, saved = self.run_watch(review_comments=[], body=review["body"])
        self.assertEqual(payload["result"], "review_comments")
        self.assertEqual(payload["comment_ids"], [])
        self.assertEqual(payload["suppressed_comment_count"], 1)
        self.assertIsNone(payload["clean_at_head_sha"])
        self.assertIsNone(saved.get("clean_at_head_sha"))

    def test_watch_fails_on_unparsed_body_feedback(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "review body"):
            self.run_watch(
                review_comments=[],
                body="<details><summary>Previously missed (1)</summary>"
                "Unparsed feedback</details>",
            )

    def test_watch_keeps_ccr_v2_resolved_only_review_clean(self):
        body = json.loads(CCR_V2_RESOLVED_REVIEW.read_text(encoding="utf-8"))["body"]
        payload, saved = self.run_watch(review_comments=[], body=body)
        self.assertEqual(payload["result"], "review_no_comments")
        self.assertEqual(payload["suppressed_comment_count"], 0)
        self.assertEqual(saved["clean_at_head_sha"], "head")

    def run_watch(self, *, review_comments, body):
        state = {
            "version": MODULE.STATE_VERSION,
            "pr": {"upstream_owner": "owner", "upstream_repo": "repo", "number": 42, "base_sha": "base"},
            "monitoring": {
                "status": "requested",
                "head_sha": "head",
                "baseline_review_id": 100,
                "copilot_bot_id": "BOT_1",
                "request_start": "2026-05-01T12:00:00Z",
                "cancel_requested": False,
            },
        }
        review = {
            "id": 101,
            "html_url": "https://example.test/review/101",
            "body": body,
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)
            args = SimpleNamespace(state=str(path), interval=0, cancellation_grace=0)

            with (
                mock.patch.object(
                    MODULE, "gh_json", return_value={"head": {"sha": "head"}}
                ),
                mock.patch.object(MODULE, "fetch_reviews", return_value=[review]),
                mock.patch.object(MODULE, "matching_review", return_value=review),
                mock.patch.object(MODULE, "gh_paginated", return_value=review_comments),
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_watch(args)

            return emit.call_args_list[-1].args[0], MODULE.load_state(path)

    def test_publish_clears_the_marker_because_the_new_head_has_no_review(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "iterations": 2,
            "clean_at_head_sha": "same-head",
            "repo_root": "repo",
            "pr": {
                "head_owner": "author",
                "head_repo": "repo",
                "head_branch": "branch",
                "head_sha": "same-head",
            },
            "queue": {"id": "pr-42", "comments": [], "status": "active"},
        }

        def fake_git(repo_root, *arguments):
            del repo_root
            return {
                ("status", "--porcelain=v1"): "",
                ("rev-parse", "HEAD"): "same-head",
            }[arguments]

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            MODULE.save_state(state_path, state)
            args = SimpleNamespace(
                state=str(state_path),
                no_comments=True,
                validated=None,
                not_validated=None,
                rewrote=None,
            )

            with (
                mock.patch.object(MODULE, "git", side_effect=fake_git),
                mock.patch.object(MODULE, "require_fork_head"),
                mock.patch.object(MODULE, "remote_head", return_value="same-head"),
                mock.patch.object(MODULE, "run"),
                mock.patch.object(
                    MODULE, "request_copilot", return_value={"status": "requested"}
                ),
                mock.patch.object(MODULE, "verify_publish", return_value={}),
                mock.patch.object(MODULE, "emit"),
            ):
                MODULE.command_publish(args)

            saved = MODULE.load_state(state_path)

        self.assertIsNone(saved["clean_at_head_sha"])

    def test_status_reports_the_marker_for_an_external_orchestrator(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "pr": {"number": 42, "url": "https://github.com/owner/repo/pull/42"},
            "queue": {"id": "pr-42"},
            "monitoring": {"status": "completed"},
            "clean_at_head_sha": "head",
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)
            args = SimpleNamespace(current=False, state=str(path), repo_root=None)

            with mock.patch.object(MODULE, "emit") as emit:
                MODULE.command_status(args)

        payload = emit.call_args.args[0]
        self.assertEqual(payload["result"], "ready")
        self.assertEqual(payload["clean_at_head_sha"], "head")

    def test_status_reports_when_the_helper_last_wrote_its_state(self):
        """The only signal a reader has for telling working from wedged.

        Every write stamps it, so a stamp minutes old and a stamp an hour old
        are different answers to the question a person actually asks.
        """
        state = {
            "version": MODULE.STATE_VERSION,
            "pr": {"number": 42, "url": "https://github.com/owner/repo/pull/42"},
            "queue": {"id": "pr-42"},
            "monitoring": {"status": "completed"},
            "clean_at_head_sha": "head",
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)
            stamp = MODULE.load_state(path)["updated_at"]
            args = SimpleNamespace(current=False, state=str(path), repo_root=None)

            with mock.patch.object(MODULE, "emit") as emit:
                MODULE.command_status(args)

        payload = emit.call_args.args[0]
        self.assertEqual(stamp, payload["last_helper_activity"])

    def test_status_reports_no_marker_before_the_stage_has_run(self):
        target = MODULE.parse_target("https://github.com/owner/repo/pull/42")

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "missing.json"
            args = SimpleNamespace(current=True, state=None, repo_root="repo")

            with (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(
                    MODULE, "resolve_repo_root", return_value=Path(directory)
                ),
                mock.patch.object(MODULE, "current_pr_target", return_value=target),
                mock.patch.object(
                    MODULE, "default_state_path", return_value=state_path
                ),
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_status(args)

        payload = emit.call_args.args[0]
        self.assertEqual(payload["result"], "no_state")
        self.assertIsNone(payload["clean_at_head_sha"])
        self.assertNotIn("stage_outcome", payload)


class StageProgressTest(unittest.TestCase):
    def test_progress_command_records_each_supported_live_substate(self):
        for phase in sorted(MODULE.STAGE_PROGRESS_PHASES):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.json"
                MODULE.save_state(
                    path,
                    {"version": MODULE.STATE_VERSION, "pr": {"number": 42}},
                )
                args = SimpleNamespace(
                    state=str(path), phase=phase, detail=f"detail for {phase}"
                )
                with mock.patch.object(MODULE, "emit") as emit:
                    MODULE.command_progress(args)

                saved = MODULE.load_state(path)
                self.assertEqual(phase, saved["stage_progress"]["phase"])
                self.assertEqual(
                    f"detail for {phase}", saved["stage_progress"]["detail"]
                )
                self.assertEqual(
                    phase, emit.call_args.args[0]["stage_progress"]["phase"]
                )

    def test_status_exposes_structured_stage_progress(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "pr": {"number": 42},
            "stage_progress": {
                "phase": "validating",
                "observed_at": "2026-08-31T12:00:00Z",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)
            with mock.patch.object(MODULE, "emit") as emit:
                MODULE.command_status(
                    SimpleNamespace(current=False, state=str(path), repo_root=None)
                )

        self.assertEqual(
            "validating", emit.call_args.args[0]["stage_progress"]["phase"]
        )

    def test_agent_marks_validation_with_structured_progress(self):
        instructions = AGENT.read_text(encoding="utf-8")
        self.assertIn("The helper owns review requests", instructions)
        self.assertNotIn("progress --state", instructions)


class StageOutcomeTest(unittest.TestCase):
    """The vocabulary an external orchestrator reads instead of the prose report."""

    PIPELINE_VOCABULARY = ("cleared", "skipped", "no_progress", "escalated", "carried")

    def test_a_clearance_is_read_off_the_marker_and_never_decided_again(self):
        self.assertEqual(
            MODULE.stage_outcome({"clean_at_head_sha": "abc123"}), "cleared"
        )

    def test_no_result_can_clear_a_run_the_marker_did_not_clear(self):
        """`stage_outcome` must never become a second, softer route to green.

        Every result a run can record is checked, including the ones that mean
        Copilot asked for nothing. Without the marker, none of them clear.
        """
        results = sorted(recorded_results() | {"published"})
        for result in results:
            with self.subTest(result=result):
                outcome = MODULE.stage_outcome({"last_result": result})
                self.assertNotEqual(outcome, "cleared")
                self.assertIn(outcome, (None, *self.PIPELINE_VOCABULARY))

    def test_a_spent_iteration_cap_is_carried(self):
        self.assertEqual(
            MODULE.stage_outcome({"last_result": "max_iterations_reached"}), "carried"
        )

    def test_an_absent_review_asks_for_a_person(self):
        for result in ("request_cancelled", "review_dismissed"):
            with self.subTest(result=result):
                self.assertEqual(
                    MODULE.stage_outcome({"last_result": result}), "escalated"
                )

    def test_a_re_runnable_stop_reports_no_progress(self):
        for result in ("head_changed", "cancelled_locally", "stopped"):
            with self.subTest(result=result):
                self.assertEqual(
                    MODULE.stage_outcome({"last_result": result}), "no_progress"
                )

    def test_an_unrecognized_ending_still_escalates(self):
        """A run did end here. Nobody can describe it, which is worth a person."""
        self.assertEqual(MODULE.stage_outcome({"last_result": "surprise"}), "escalated")

    def test_a_state_that_recorded_no_ending_answers_nothing(self):
        """Absence of evidence is not evidence of absence.

        A state file written before this field existed, or one from a run that
        never recorded an ending, supports no claim about how a run went. It must
        not be dressed up as one, not even a conservative one.
        """
        self.assertIsNone(MODULE.stage_outcome({}))
        self.assertIsNone(MODULE.stage_outcome({"last_result": None}))
        self.assertIsNone(MODULE.stage_outcome({"last_result": ""}))
        self.assertIsNone(
            MODULE.stage_outcome({"clean_at_head_sha": None, "queue": {"id": "pr-42"}})
        )

    def test_every_mapped_outcome_uses_the_exact_pipeline_spelling(self):
        """A near miss like `green` or `clean` is silently ignored by the reader."""
        for result, outcome in MODULE.STAGE_OUTCOME_BY_RESULT.items():
            with self.subTest(result=result):
                self.assertIn(outcome, self.PIPELINE_VOCABULARY)

    def test_no_mapped_result_is_unreachable(self):
        """A map entry for a result nothing records describes a run nobody has.

        It reads as a promise the helper keeps, so it hides the case it claims to
        cover: the run ends some other way and is described by whatever an
        earlier command happened to leave behind.
        """

        self.assertEqual(
            sorted(set(MODULE.STAGE_OUTCOME_BY_RESULT) - recorded_results()), []
        )

    def test_every_result_the_writer_can_record_is_classified(self):
        """Growing the writer must fail here rather than misreport in the field.

        Every ``last_result`` the code can write falls into exactly one class the
        source declares: a preflight-pending value the run is still owed an ending
        for, a clean review that clears through its marker, or a recorded ending
        the map names. Each class's declaration lives in the source, and this test
        asserts what ``stage_outcome`` actually returns for it rather than trusting
        a set the test builds. A new recorded ending nobody maps would escalate
        every run silently; a new preflight or clean value nobody declares would be
        read as an unrecognized ending and escalate a run that never ended; and a
        clean value read without its marker must never be a markerless clearance.
        All are invisible at runtime, so all are pinned here from the source sets.
        """

        pending = set(MODULE.PREFLIGHT_PENDING_RESULTS)
        # Clears only through the marker, so without one it must defer, not clear.
        marker_clears = set(MODULE.CLEAN_PREFLIGHT_RESULTS) | set(
            MODULE.WATCHER_CLEAN_RESULTS
        )
        mapped = set(MODULE.STAGE_OUTCOME_BY_RESULT)
        classified = pending | marker_clears | mapped

        # Every value the writer can record is classified somewhere. `pending` and
        # `marker_clears` overlap by design -- the clean preflight pair is both
        # written up front and a clearance through its marker -- so they are not
        # required to be disjoint. What must never overlap is a value the map gives
        # a word and a value that defers or clears through the marker: a mapped
        # ending returns its word unconditionally, which would override the other
        # two behaviors. That disjointness is the one that guards the contract.
        self.assertEqual(sorted(recorded_results() - classified), [])
        self.assertEqual(set(), mapped & (pending | marker_clears))

        # A run still owed an ending defers rather than being read as one;
        # `ready`/`review_required` are the values the #19517 loss made a false
        # `escalated`. A preflight-pending value never carries a marker of its own.
        for result in sorted(pending):
            with self.subTest(pending=result):
                self.assertIsNone(MODULE.stage_outcome({"last_result": result}))

        # A clean review is a clearance, but only with its marker. With one it
        # clears; without one it defers rather than reporting a markerless
        # clearance or a false `escalated` on the clean path.
        for result in sorted(marker_clears):
            with self.subTest(clean=result):
                self.assertEqual(
                    "cleared",
                    MODULE.stage_outcome(
                        {"last_result": result, "clean_at_head_sha": "head"}
                    ),
                )
                self.assertIsNone(MODULE.stage_outcome({"last_result": result}))

        # A recorded ending the map names returns exactly its word.
        for result in sorted(mapped):
            with self.subTest(mapped=result):
                self.assertEqual(
                    MODULE.stage_outcome({"last_result": result}),
                    MODULE.STAGE_OUTCOME_BY_RESULT[result],
                )

    def test_a_cleared_run_always_carries_the_marker_it_rests_on(self):
        """`pr-pipeline` refuses a clearance whose marker names another head.

        That guard only works when the marker travels with the word, so no path
        may report `cleared` and leave the reader nothing to check it against.
        """

        for result in sorted(recorded_results() | {"", "surprise"}):
            for marker in (None, "", "abc123"):
                state = {"last_result": result, "clean_at_head_sha": marker}
                with self.subTest(result=result, marker=marker):
                    if MODULE.stage_outcome(state) == "cleared":
                        self.assertTrue(state["clean_at_head_sha"])

    def test_the_watcher_records_the_result_the_outcome_is_read_from(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, {"version": MODULE.STATE_VERSION, "monitoring": {}})
            state = MODULE.load_state(path)
            MODULE.watcher_result(state, {"result": "request_cancelled"})
            MODULE.save_state(path, state)
            recorded = MODULE.load_state(path)

        self.assertEqual(recorded["last_result"], "request_cancelled")
        self.assertEqual(MODULE.stage_outcome(recorded), "escalated")

    def test_a_stopped_watch_records_the_ending_it_actually_had(self):
        """Interrupting the watcher must not report the result preflight left.

        The user stopped this run themselves, so it did not clear and nobody
        needs fetching. Recording the stop anywhere but `last_result` leaves the
        run described by an earlier command, and preflight's own results
        escalate.
        """

        state = {
            "version": MODULE.STATE_VERSION,
            "pr": {
                "number": 7,
                "upstream_owner": "trask",
                "upstream_repo": "copilot-plugins",
            },
            "queue": {},
            "last_result": "review_required",
            "monitoring": {
                "status": "running",
                "head_sha": "abc123",
                "baseline_review_id": 0,
                "copilot_bot_id": "BOT_1",
                "request_start": "2026-05-01T12:00:00Z",
                "cancel_requested": False,
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)
            args = SimpleNamespace(state=str(path), interval=0, cancellation_grace=0)

            with (
                mock.patch.object(MODULE, "gh_json", side_effect=KeyboardInterrupt),
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_watch(args)

            recorded = MODULE.load_state(path)

        self.assertEqual(emit.call_args.args[0], {"result": "stopped"})
        self.assertEqual(recorded["monitoring"]["status"], "stopped")
        self.assertEqual(recorded["last_result"], "stopped")
        self.assertEqual(MODULE.stage_outcome(recorded), "no_progress")
        self.assertIsNone(recorded.get("clean_at_head_sha"))

    def test_status_reports_the_outcome_for_an_external_orchestrator(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "pr": {"number": 42},
            "queue": {"id": "pr-42"},
            "monitoring": {"status": "completed"},
            "last_result": "head_changed",
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)
            args = SimpleNamespace(current=False, state=str(path), repo_root=None)

            with mock.patch.object(MODULE, "emit") as emit:
                MODULE.command_status(args)

        payload = emit.call_args.args[0]
        self.assertEqual(payload["result"], "ready")
        self.assertEqual(payload["stage_outcome"], "no_progress")
        self.assertIsNone(payload["clean_at_head_sha"])

    def test_a_no_state_payload_never_carries_an_outcome_word(self):
        """A stage that was never launched has not made no progress. It has not run.

        Pinned because a later edit will be tempted to make the key unconditional
        for tidiness, which would assert a run that never happened.
        """
        target = MODULE.parse_target("https://github.com/owner/repo/pull/42")

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "missing.json"
            args = SimpleNamespace(current=True, state=None, repo_root="repo")

            with (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(
                    MODULE, "resolve_repo_root", return_value=Path(directory)
                ),
                mock.patch.object(MODULE, "current_pr_target", return_value=target),
                mock.patch.object(
                    MODULE, "default_state_path", return_value=state_path
                ),
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_status(args)

        payload = emit.call_args.args[0]
        self.assertEqual(payload["result"], "no_state")
        self.assertNotIn("stage_outcome", payload)
        for value in MODULE.STAGE_OUTCOME_BY_RESULT.values():
            self.assertNotIn(value, json.dumps(payload))

    def test_status_omits_the_outcome_for_a_state_that_recorded_no_ending(self):
        """A state file from before this field existed must not gain an ending."""
        state = {
            "version": MODULE.STATE_VERSION,
            "pr": {"number": 42},
            "queue": {"id": "pr-42"},
            "monitoring": {"status": "completed"},
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)
            args = SimpleNamespace(current=False, state=str(path), repo_root=None)

            with mock.patch.object(MODULE, "emit") as emit:
                MODULE.command_status(args)

        payload = emit.call_args.args[0]
        self.assertEqual(payload["result"], "ready")
        self.assertNotIn("stage_outcome", payload)


class CopilotReviewTest(unittest.TestCase):
    def test_matches_review_that_completed_immediately(self):
        monitoring = {
            "baseline_review_id": 100,
            "head_sha": "abc123",
            "copilot_bot_id": "BOT_1",
            "request_start": "2026-05-01T12:00:00Z",
        }
        reviews = [
            {
                "id": 101,
                "commit_id": "abc123",
                "submitted_at": "2026-05-01T12:00:01Z",
                "user": {
                    "login": "copilot-pull-request-reviewer[bot]",
                    "node_id": "BOT_1",
                },
            }
        ]

        self.assertEqual(MODULE.matching_review(reviews, monitoring)["id"], 101)

    def test_watch_records_a_bounded_timeout(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "pr": {"upstream_owner": "owner", "upstream_repo": "repo", "number": 7},
            "monitoring": {
                "status": "requested",
                "head_sha": "abc123",
                "baseline_review_id": 100,
                "copilot_bot_id": "BOT_1",
                "request_start": "2026-05-01T12:00:00Z",
                "cancel_requested": False,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)
            args = SimpleNamespace(
                state=str(path),
                interval=0,
                max_interval=0,
                timeout=1,
                poll_jitter=0,
                cancellation_grace=0,
            )
            with (
                mock.patch.object(MODULE.time, "monotonic", side_effect=[0, 2]),
                mock.patch.object(MODULE, "gh_json") as gh_json,
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_watch(args)

            saved = MODULE.load_state(path)

        gh_json.assert_not_called()
        self.assertEqual(saved["monitoring"]["result"], {"result": "timeout"})
        self.assertEqual(emit.call_args_list[-1].args[0], {"result": "timeout"})

    def test_request_only_timeout_keeps_review_request_resumable(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "pr": {"upstream_owner": "owner", "upstream_repo": "repo", "number": 7},
            "monitoring": {
                "status": "requested",
                "head_sha": "abc123",
                "baseline_review_id": 100,
                "copilot_bot_id": "BOT_1",
                "request_start": "2026-05-01T12:00:00Z",
                "cancel_requested": False,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)
            args = SimpleNamespace(
                state=str(path),
                interval=0,
                max_interval=0,
                timeout=1,
                poll_jitter=0,
                cancellation_grace=0,
                resume_on_timeout=True,
            )
            with (
                mock.patch.object(MODULE.time, "monotonic", side_effect=[0, 2]),
                mock.patch.object(MODULE, "gh_json") as gh_json,
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_watch(args)

            saved = MODULE.load_state(path)

        gh_json.assert_not_called()
        self.assertEqual("requested", saved["monitoring"]["status"])
        self.assertEqual({"result": "timeout"}, saved["monitoring"]["result"])
        self.assertEqual({"result": "timeout"}, emit.call_args_list[-1].args[0])

    def test_watch_retries_rate_limited_review_comments_with_local_backoff(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "pr": {"upstream_owner": "owner", "upstream_repo": "repo", "number": 7, "base_sha": "base"},
            "monitoring": {
                "status": "requested",
                "head_sha": "abc123",
                "baseline_review_id": 100,
                "copilot_bot_id": "BOT_1",
                "request_start": "2026-05-01T12:00:00Z",
                "cancel_requested": False,
            },
        }
        review = {
            "id": 101,
            "html_url": "https://example.test/review/101",
            "body": "",
            "state": "COMMENTED",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)
            args = SimpleNamespace(
                state=str(path),
                interval=1,
                max_interval=10,
                timeout=60,
                poll_jitter=0,
                cancellation_grace=0,
            )
            with (
                mock.patch.object(MODULE.time, "monotonic", return_value=0),
                mock.patch.object(MODULE.time, "sleep") as sleep,
                mock.patch.object(
                    MODULE, "gh_json", return_value={"head": {"sha": "abc123"}}
                ),
                mock.patch.object(MODULE, "fetch_reviews", return_value=[review]),
                mock.patch.object(MODULE, "matching_review", return_value=review),
                mock.patch.object(
                    MODULE,
                    "gh_paginated",
                    side_effect=[
                        MODULE.WorkflowError("API rate limit exceeded"),
                        [],
                    ],
                ) as comments,
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_watch(args)

            saved = MODULE.load_state(path)

        self.assertEqual(comments.call_count, 2)
        sleep.assert_called_once_with(1)
        self.assertIn("rate limit", saved["monitoring"]["last_rate_limit"]["detail"])
        self.assertEqual(
            emit.call_args_list[-1].args[0]["result"],
            MODULE.WATCHER_REVIEW_CLEAN,
        )

    def test_tolerates_github_timestamp_precision(self):
        monitoring = {
            "baseline_review_id": 100,
            "head_sha": "abc123",
            "copilot_bot_id": "BOT_1",
            "request_start": "2026-05-01T12:00:00.750000Z",
        }
        reviews = [
            {
                "id": 101,
                "commit_id": "abc123",
                "submitted_at": "2026-05-01T12:00:00Z",
                "user": {
                    "login": "copilot-pull-request-reviewer[bot]",
                    "node_id": "BOT_1",
                },
            }
        ]

        self.assertEqual(MODULE.matching_review(reviews, monitoring)["id"], 101)

    def test_ignores_an_in_flight_review_of_an_earlier_commit(self):
        """A review Copilot began before the head moved is not evidence about the head.

        The marker is proof for exactly one SHA, so a review that landed during the
        watch but describes an older commit must never satisfy the wait.
        """
        monitoring = {
            "baseline_review_id": 100,
            "head_sha": "abc123",
            "copilot_bot_id": "BOT_1",
            "request_start": "2026-05-01T12:00:00Z",
        }
        reviews = [
            {
                "id": 101,
                "commit_id": "0ldc0de",
                "submitted_at": "2026-05-01T12:00:05Z",
                "user": {
                    "login": "copilot-pull-request-reviewer[bot]",
                    "node_id": "BOT_1",
                },
            }
        ]

        self.assertIsNone(MODULE.matching_review(reviews, monitoring))

    def test_watch_records_no_marker_while_only_an_older_commit_was_reviewed(self):
        """The end-to-end shape of the same hazard, through the watcher itself.

        The only Copilot review present describes an earlier commit, so the watcher
        must keep waiting rather than conclude the head is clean. Here it leaves the
        loop because the review request was withdrawn, which proves it never treated
        the stale review as an answer.
        """
        state = {
            "version": MODULE.STATE_VERSION,
            "pr": {"upstream_owner": "owner", "upstream_repo": "repo", "number": 7},
            "monitoring": {
                "status": "requested",
                "head_sha": "abc123",
                "baseline_review_id": 100,
                "copilot_bot_id": "BOT_1",
                "request_start": "2026-05-01T12:00:00Z",
                "cancel_requested": False,
            },
        }
        reviews = [
            {
                "id": 101,
                "commit_id": "0ldc0de",
                "submitted_at": "2026-05-01T12:00:05Z",
                "html_url": "https://example.test/review/101",
                "state": "COMMENTED",
                "user": {
                    "login": "copilot-pull-request-reviewer[bot]",
                    "node_id": "BOT_1",
                },
            }
        ]
        timeline = [
            {
                "event": "review_request_removed",
                "created_at": "2026-05-01T12:00:06Z",
                "requested_reviewer": {
                    "login": "copilot-pull-request-reviewer[bot]",
                    "node_id": "BOT_1",
                },
            }
        ]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)
            args = SimpleNamespace(state=str(path), interval=0, cancellation_grace=0)
            with (
                mock.patch.object(
                    MODULE, "gh_json", return_value={"head": {"sha": "abc123"}}
                ),
                mock.patch.object(MODULE, "fetch_reviews", return_value=reviews),
                mock.patch.object(MODULE, "fetch_timeline", return_value=timeline),
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_watch(args)

            recorded = MODULE.load_state(path).get("clean_at_head_sha")

        payload = emit.call_args_list[-1].args[0]
        self.assertEqual(payload["result"], "request_cancelled")
        self.assertIsNone(payload.get("clean_at_head_sha"))
        self.assertIsNone(recorded)

    def test_watch_treats_suppressed_only_review_as_comments(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "pr": {
                "upstream_owner": "owner",
                "upstream_repo": "repo",
                "number": 42,
            },
            "monitoring": {
                "status": "requested",
                "head_sha": "head",
                "baseline_review_id": 100,
                "copilot_bot_id": "BOT_1",
                "request_start": "2026-05-01T12:00:00Z",
                "cancel_requested": False,
            },
        }
        review = {
            "id": 101,
            "html_url": "https://example.test/review/101",
            "body": """
<details><summary>Suppressed comments (1)</summary>
**a.java:1**
* Fix this.
</details>
""",
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)
            args = SimpleNamespace(state=str(path), interval=0, cancellation_grace=0)

            with (
                mock.patch.object(
                    MODULE, "gh_json", return_value={"head": {"sha": "head"}}
                ),
                mock.patch.object(MODULE, "fetch_reviews", return_value=[review]),
                mock.patch.object(MODULE, "matching_review", return_value=review),
                mock.patch.object(MODULE, "gh_paginated", return_value=[]),
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_watch(args)

        result = emit.call_args_list[-1].args[0]
        self.assertEqual(result["result"], "review_comments")
        self.assertEqual(result["comment_ids"], [])
        self.assertEqual(result["suppressed_comment_count"], 1)

    def test_requested_watcher_cancellation_completes_locally(self):
        state = {
            "monitoring": {
                "status": "requested",
                "cancel_requested": False,
            }
        }

        result = MODULE.request_watch_cancellation(state)

        self.assertEqual(result, "cancelled_locally")
        self.assertEqual(state["monitoring"]["status"], "completed")
        self.assertEqual(state["monitoring"]["result"], {"result": "cancelled_locally"})

    def test_stale_watcher_cancellation_completes_locally(self):
        state = {
            "monitoring": {
                "status": "running",
                "pid": 123,
                "cancel_requested": False,
            }
        }

        with mock.patch.object(MODULE, "process_is_running", return_value=False):
            result = MODULE.request_watch_cancellation(state)

        self.assertEqual(result, "cancelled_locally")
        self.assertEqual(state["monitoring"]["status"], "completed")
        self.assertEqual(state["monitoring"]["result"], {"result": "cancelled_locally"})

    def test_live_watcher_cancellation_waits_for_watcher(self):
        state = {
            "monitoring": {
                "status": "running",
                "pid": 123,
                "cancel_requested": False,
            }
        }

        with mock.patch.object(MODULE, "process_is_running", return_value=True):
            result = MODULE.request_watch_cancellation(state)

        self.assertEqual(result, "cancel_requested")
        self.assertEqual(state["monitoring"]["status"], "running")
        self.assertTrue(state["monitoring"]["cancel_requested"])

    def test_preflight_reports_the_active_watcher_state_and_actions(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "monitoring": {
                "status": "running",
                "pid": 123,
                "cancel_requested": False,
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)
            args = SimpleNamespace(
                target="owner/repo#1",
                repo_root=directory,
                state=str(path),
            )

            with (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(MODULE, "process_is_running", return_value=True),
                mock.patch.object(MODULE, "resolve_repo_root") as resolve_repo_root,
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_preflight(args)

            saved = MODULE.load_state(path)

        resolve_repo_root.assert_called_once_with(directory)
        self.assertTrue(saved["monitoring"]["cancel_requested"])
        emit.assert_called_once_with(
            {
                "result": "watcher_cancellation_pending",
                "state": str(path.resolve()),
                "watcher_pid": 123,
                "wait_action": {
                    "command": "await-watch",
                    "state": str(path.resolve()),
                },
                "cancel_action": {
                    "command": "cancel-watch",
                    "state": str(path.resolve()),
                },
            }
        )

    def test_await_watch_returns_the_persisted_terminal_result(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "monitoring": {
                "status": "running",
                "pid": 123,
                "cancel_requested": True,
            },
        }
        completed = {
            **state,
            "monitoring": {
                "status": "completed",
                "result": {"result": "cancelled_locally"},
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)
            args = SimpleNamespace(state=str(path), interval=0.25)

            with (
                mock.patch.object(MODULE, "load_state", side_effect=[state, completed]),
                mock.patch.object(MODULE, "process_is_running", return_value=True),
                mock.patch.object(MODULE.time, "sleep") as sleep,
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_await_watch(args)

        sleep.assert_called_once_with(0.25)
        emit.assert_called_once_with(
            {
                "result": "watcher_completed",
                "state": str(path.resolve()),
                "watcher_result": {"result": "cancelled_locally"},
            }
        )

    def test_await_watch_completes_a_stale_running_watcher(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "monitoring": {
                "status": "running",
                "pid": 123,
                "cancel_requested": True,
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)
            args = SimpleNamespace(state=str(path), interval=0.25)

            with (
                mock.patch.object(MODULE, "process_is_running", return_value=False),
                mock.patch.object(MODULE.time, "sleep") as sleep,
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_await_watch(args)

            saved = MODULE.load_state(path)

        sleep.assert_not_called()
        self.assertEqual(saved["monitoring"]["status"], "completed")
        self.assertEqual(saved["monitoring"]["result"], {"result": "cancelled_locally"})
        emit.assert_called_once_with(
            {
                "result": "watcher_completed",
                "state": str(path.resolve()),
                "watcher_result": {"result": "cancelled_locally"},
            }
        )

    def test_watch_rejects_duplicate_live_process(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "monitoring": {
                "status": "running",
                "pid": 123,
                "cancel_requested": False,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)

            with (
                mock.patch.object(MODULE, "process_is_running", return_value=True),
                self.assertRaisesRegex(MODULE.WorkflowError, "already running"),
            ):
                MODULE.command_watch(SimpleNamespace(state=str(path)))

    def test_preflight_recovers_stale_watcher(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "monitoring": {
                "status": "running",
                "pid": 123,
                "cancel_requested": False,
            },
        }
        metadata = {"head_branch": "branch", "head_sha": "head", "base_sha": "base"}

        def fake_git(repo_root, *arguments):
            del repo_root
            return {
                ("status", "--porcelain=v1"): "",
                ("branch", "--show-current"): "branch",
                ("rev-parse", "HEAD"): "head",
            }[arguments]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(path, state)
            args = SimpleNamespace(
                target="https://github.com/owner/repo/pull/1#pullrequestreview-2",
                repo_root=directory,
                state=str(path),
            )

            with (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(MODULE, "process_is_running", return_value=False),
                mock.patch.object(
                    MODULE, "resolve_repo_root", return_value=Path(directory)
                ),
                mock.patch.object(MODULE, "git", side_effect=fake_git),
                mock.patch.object(MODULE, "metadata_for", return_value=metadata),
                mock.patch.object(MODULE, "run"),
                mock.patch.object(MODULE, "fetch_threads", return_value=[]),
                mock.patch.object(MODULE, "fetch_reviews", return_value=[]),
                mock.patch.object(MODULE, "emit"),
            ):
                MODULE.command_preflight(args)

            saved = MODULE.load_state(path)

        self.assertEqual(saved["monitoring"]["status"], "completed")
        self.assertEqual(saved["monitoring"]["result"], {"result": "cancelled_locally"})
        self.assertEqual(saved["queue"]["id"], "pr-1")


class PreflightTargetTest(unittest.TestCase):
    def run_preflight(
        self,
        *,
        threads=None,
        reviews=None,
        iterations=0,
        completed_run_iterations=0,
        max_iterations=5,
        local_branch="branch",
        checked_out_branch=True,
        pipeline=None,
        state_path=None,
    ):
        metadata = {"head_branch": "branch", "head_sha": "head", "base_sha": "base"}

        def fake_git(repo_root, *arguments):
            del repo_root
            return {
                ("status", "--porcelain=v1"): "",
                ("branch", "--show-current"): local_branch,
                ("rev-parse", "HEAD"): "head",
            }[arguments]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(state_path) if state_path else Path(directory) / "state.json"
            if iterations and not path.exists():
                MODULE.save_state(
                    path,
                    {
                        "version": MODULE.STATE_VERSION,
                        "iterations": iterations,
                        "queue": {"comments": [], "batches": []},
                    },
                )
            args = SimpleNamespace(
                target="owner/repo#7",
                repo_root=directory,
                state=str(path),
                max_iterations=max_iterations,
                completed_run_iterations=completed_run_iterations,
                **(pipeline or {}),
            )

            with (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(
                    MODULE, "resolve_repo_root", return_value=Path(directory)
                ),
                mock.patch.object(MODULE, "git", side_effect=fake_git),
                mock.patch.object(MODULE, "metadata_for", return_value=metadata),
                mock.patch.object(
                    MODULE, "checkout_pr", return_value=checked_out_branch
                ),
                mock.patch.object(MODULE, "run"),
                mock.patch.object(MODULE, "fetch_threads", return_value=threads or []),
                mock.patch.object(MODULE, "fetch_reviews", return_value=reviews or []),
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_preflight(args)

        return emit.call_args.args[0]

    def test_preflight_accepts_detached_checkout_from_another_branch(self):
        payload = self.run_preflight(
            local_branch="session-branch", checked_out_branch=False
        )

        self.assertEqual(payload["pr"]["head_branch"], "branch")
        self.assertEqual(payload["pr"]["head_sha"], "head")

    def test_targetless_preflight_uses_the_current_branch_pr(self):
        metadata = {"head_branch": "branch", "head_sha": "head", "base_sha": "base"}
        target = MODULE.parse_target("https://github.com/owner/repo/pull/7")

        def fake_git(repo_root, *arguments):
            del repo_root
            return {
                ("status", "--porcelain=v1"): "",
                ("branch", "--show-current"): "branch",
                ("rev-parse", "HEAD"): "head",
            }[arguments]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            args = SimpleNamespace(target=None, repo_root=directory, state=str(path))

            with (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(
                    MODULE, "resolve_repo_root", return_value=Path(directory)
                ),
                mock.patch.object(
                    MODULE, "current_pr_target", return_value=target
                ) as current_pr_target,
                mock.patch.object(MODULE, "git", side_effect=fake_git),
                mock.patch.object(MODULE, "metadata_for", return_value=metadata),
                mock.patch.object(MODULE, "run"),
                mock.patch.object(MODULE, "fetch_threads", return_value=[]),
                mock.patch.object(MODULE, "fetch_reviews", return_value=[]),
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_preflight(args)

            saved = MODULE.load_state(path)

        current_pr_target.assert_called_once_with(Path(directory))
        self.assertEqual(saved["queue"]["id"], "pr-7")
        self.assertEqual(emit.call_args.args[0]["result"], "review_required")
        self.assertFalse(emit.call_args.args[0]["head_review_clean"])

    def test_preflight_accepts_clean_review_on_exact_head(self):
        review = {
            "id": 10,
            "commit_id": "head",
            "submitted_at": "2026-08-09T12:00:00Z",
            "state": "APPROVED",
            "html_url": "https://example.test/review/10",
            "body": "No comments.",
            "user": {"login": "copilot-pull-request-reviewer[bot]"},
        }

        payload = self.run_preflight(reviews=[review])

        self.assertEqual(payload["result"], "no_unresolved_comments")
        self.assertEqual(payload["head_review_id"], 10)
        self.assertEqual(
            payload["head_review_url"],
            "https://example.test/review/10",
        )
        self.assertTrue(payload["head_review_clean"])

    def test_preflight_requests_review_when_only_review_is_for_older_head(self):
        review = {
            "id": 10,
            "commit_id": "old-head",
            "submitted_at": "2026-08-09T12:00:00Z",
            "state": "COMMENTED",
            "body": "No comments.",
            "user": {"login": "copilot-pull-request-reviewer[bot]"},
        }

        payload = self.run_preflight(reviews=[review])

        self.assertEqual(payload["result"], "review_required")
        self.assertIsNone(payload["head_review_id"])
        self.assertFalse(payload["head_review_clean"])

    def test_preflight_requests_review_when_exact_head_review_was_dismissed(self):
        review = {
            "id": 10,
            "commit_id": "head",
            "submitted_at": "2026-08-09T12:00:00Z",
            "state": "DISMISSED",
            "body": "No comments.",
            "user": {"login": "copilot-pull-request-reviewer[bot]"},
        }

        payload = self.run_preflight(reviews=[review])

        self.assertEqual(payload["result"], "review_required")
        self.assertIsNone(payload["head_review_id"])
        self.assertFalse(payload["head_review_clean"])

    def test_preflight_requests_review_after_resolved_exact_head_finding(self):
        review = {
            "id": 10,
            "commit_id": "head",
            "submitted_at": "2026-08-09T12:00:00Z",
            "state": "COMMENTED",
            "body": "",
            "user": {"login": "copilot-pull-request-reviewer[bot]"},
        }
        thread = {
            "id": "thread-1",
            "isResolved": True,
            "comments": {
                "nodes": [
                    {
                        "databaseId": 1,
                        "author": {
                            "login": "copilot-pull-request-reviewer[bot]",
                            "id": "BOT_1",
                        },
                        "pullRequestReview": {"databaseId": 10},
                    }
                ]
            },
        }

        payload = self.run_preflight(threads=[thread], reviews=[review])

        self.assertEqual(payload["result"], "review_required")
        self.assertEqual(payload["head_review_id"], 10)
        self.assertFalse(payload["head_review_clean"])

    def test_preflight_queues_suppressed_exact_head_finding(self):
        review = {
            "id": 10,
            "commit_id": "head",
            "submitted_at": "2026-08-09T12:00:00Z",
            "state": "COMMENTED",
            "html_url": "https://example.test/review/10",
            "body": """
<details><summary>Suppressed comments (1)</summary>
**src/example.py:4**
* Fix this.
</details>
""",
            "user": {
                "login": "copilot-pull-request-reviewer[bot]",
                "node_id": "BOT_1",
            },
        }

        payload = self.run_preflight(reviews=[review])

        self.assertEqual(payload["result"], "ready")
        self.assertEqual(payload["queue"]["comments"][0]["source"], "suppressed")
        self.assertFalse(payload["head_review_clean"])

    def test_preflight_queues_ccr_v2_body_only_feedback(self):
        review = json.loads(CCR_V2_REVIEW.read_text(encoding="utf-8"))
        review["commit_id"] = "head"
        payload = self.run_preflight(reviews=[review])
        self.assertEqual(payload["result"], "ready")
        self.assertFalse(payload["head_review_clean"])
        self.assertEqual(len(payload["queue"]["comments"]), 1)
        self.assertEqual(payload["queue"]["comments"][0]["source"], "suppressed")
        self.assertIsNone(payload["queue"]["comments"][0]["thread_id"])

    def test_preflight_queues_legacy_review_details_without_clearance(self):
        review = json.loads(LEGACY_REVIEW_DETAILS.read_text(encoding="utf-8"))
        review["commit_id"] = "head"
        payload = self.run_preflight(reviews=[review])
        self.assertEqual(payload["result"], "ready")
        self.assertFalse(payload["head_review_clean"])
        self.assertIsNone(payload["clean_at_head_sha"])
        self.assertEqual(payload["suppressed_review_id"], review["id"])
        self.assertEqual(payload["head_review_id"], review["id"])
        self.assertEqual(len(payload["queue"]["comments"]), 1)
        self.assertEqual(payload["queue"]["comments"][0]["id"], -5203651790000)
        self.assertEqual(payload["queue"]["comments"][0]["source"], "suppressed")
        self.assertIsNone(payload["queue"]["comments"][0]["thread_id"])

    def test_preflight_rejects_unparsed_legacy_review_details(self):
        review = json.loads(LEGACY_REVIEW_DETAILS.read_text(encoding="utf-8"))
        review["commit_id"] = "head"
        review["body"] = review["body"].replace(
            "Suppressed comments (1)", "Suppressed comments (unknown)"
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "review body"):
            self.run_preflight(reviews=[review])

    def test_preflight_reports_when_only_human_comments_remain(self):
        metadata = {"head_branch": "branch", "head_sha": "head", "base_sha": "base"}
        threads = [
            {
                "id": "thread-1",
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 1,
                            "url": "https://example.test/1",
                            "body": "human",
                            "author": {"login": "reviewer"},
                            "pullRequestReview": {"databaseId": 5},
                        }
                    ]
                },
            }
        ]

        def fake_git(repo_root, *arguments):
            del repo_root
            return {
                ("status", "--porcelain=v1"): "",
                ("branch", "--show-current"): "branch",
                ("rev-parse", "HEAD"): "head",
            }[arguments]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            args = SimpleNamespace(
                target="owner/repo#7", repo_root=directory, state=str(path)
            )

            with (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(
                    MODULE, "resolve_repo_root", return_value=Path(directory)
                ),
                mock.patch.object(MODULE, "git", side_effect=fake_git),
                mock.patch.object(MODULE, "metadata_for", return_value=metadata),
                mock.patch.object(MODULE, "run"),
                mock.patch.object(MODULE, "fetch_threads", return_value=threads),
                mock.patch.object(
                    MODULE,
                    "fetch_reviews",
                    return_value=[
                        {
                            "id": 6,
                            "commit_id": "head",
                            "submitted_at": "2026-08-09T12:00:00Z",
                            "state": "COMMENTED",
                            "body": "No comments.",
                            "user": {"login": "copilot-pull-request-reviewer[bot]"},
                        }
                    ],
                ),
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_preflight(args)

        payload = emit.call_args.args[0]
        self.assertEqual(payload["result"], "no_copilot_comments")
        self.assertEqual(payload["skipped_authors"], ["reviewer"])

    def test_preflight_requests_review_with_only_human_threads_and_no_clean_review(
        self,
    ):
        thread = {
            "id": "thread-1",
            "isResolved": False,
            "comments": {
                "nodes": [
                    {
                        "databaseId": 1,
                        "author": {"login": "reviewer"},
                        "pullRequestReview": {"databaseId": 5},
                    }
                ]
            },
        }

        payload = self.run_preflight(threads=[thread])

        self.assertEqual(payload["result"], "review_required")
        self.assertEqual(payload["skipped_authors"], ["reviewer"])
        self.assertEqual(payload["queue"]["comments"], [])

    def test_preflight_never_reports_what_a_human_reviewer_wrote(self):
        threads = [
            {
                "id": "thread-1",
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 1,
                            "url": "https://example.test/1",
                            "body": "This whole approach is wrong.",
                            "author": {"login": "reviewer"},
                            "pullRequestReview": {"databaseId": 5},
                        }
                    ]
                },
            },
            {
                "id": "thread-2",
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 2,
                            "url": "https://example.test/2",
                            "body": "Rename this variable.",
                            "author": {
                                "login": "copilot-pull-request-reviewer[bot]",
                                "id": "BOT_1",
                            },
                            "pullRequestReview": {"databaseId": 6},
                        }
                    ]
                },
            },
        ]

        payload = self.run_preflight(threads=threads)

        self.assertEqual(payload["result"], "ready")
        self.assertEqual([item["id"] for item in payload["queue"]["comments"]], [2])
        self.assertNotIn("This whole approach is wrong.", json.dumps(payload))

    def test_preflight_ignores_persisted_iterations_for_run_cap(self):
        payload = self.run_preflight(iterations=5, max_iterations=5)

        self.assertEqual(payload["result"], "review_required")
        self.assertEqual(payload["iteration"], 1)
        self.assertEqual(payload["completed_run_iterations"], 0)
        self.assertEqual(payload["max_iterations"], 5)
        self.assertEqual(payload["published_iterations"], 5)

    def test_preflight_caps_empty_review_required_iteration_for_current_run(self):
        payload = self.run_preflight(
            iterations=12, completed_run_iterations=5, max_iterations=5
        )

        self.assertEqual(payload["result"], "max_iterations_reached")
        self.assertEqual(payload["iteration"], 6)
        self.assertEqual(payload["completed_run_iterations"], 5)
        self.assertEqual(payload["max_iterations"], 5)
        self.assertEqual(payload["published_iterations"], 12)

    def test_preflight_stops_at_the_iteration_cap(self):
        metadata = {"head_branch": "branch", "head_sha": "head", "base_sha": "base"}
        threads = [
            {
                "id": "thread-1",
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 1,
                            "url": "https://example.test/1",
                            "body": "Copilot comment",
                            "author": {
                                "login": "copilot-pull-request-reviewer[bot]",
                                "id": "BOT_1",
                            },
                            "pullRequestReview": {"databaseId": 5},
                        }
                    ]
                },
            }
        ]

        def fake_git(repo_root, *arguments):
            del repo_root
            return {
                ("status", "--porcelain=v1"): "",
                ("branch", "--show-current"): "branch",
                ("rev-parse", "HEAD"): "head",
            }[arguments]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(
                path,
                {
                    "version": MODULE.STATE_VERSION,
                    "iterations": 5,
                    "queue": {"comments": [], "batches": []},
                },
            )
            args = SimpleNamespace(
                target="owner/repo#7",
                repo_root=directory,
                state=str(path),
                max_iterations=5,
                completed_run_iterations=5,
            )

            with (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(
                    MODULE, "resolve_repo_root", return_value=Path(directory)
                ),
                mock.patch.object(MODULE, "git", side_effect=fake_git),
                mock.patch.object(MODULE, "metadata_for", return_value=metadata),
                mock.patch.object(MODULE, "run"),
                mock.patch.object(MODULE, "fetch_threads", return_value=threads),
                mock.patch.object(MODULE, "fetch_reviews", return_value=[]),
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_preflight(args)

        payload = emit.call_args.args[0]
        self.assertEqual(payload["result"], "max_iterations_reached")
        self.assertEqual(payload["iteration"], 6)
        self.assertEqual(payload["completed_run_iterations"], 5)
        self.assertEqual(payload["max_iterations"], 5)


class PipelineBudgetTest(unittest.TestCase):
    """The stage allowance belongs to the entire Pipeline run."""

    def scope(self, state, **pipeline):
        return MODULE.pipeline_scope(state, SimpleNamespace(**pipeline))

    def test_later_sweeps_can_spend_only_the_remaining_five_iteration_allowance(self):
        state = {"iterations": 0, "budget_scope": "pipeline"}
        for iteration in range(5):
            scope = self.scope(
                state, pipeline_run="run-a",
                pipeline_iteration=1 if iteration < 3 else 2,
            )
            state["pipeline_budget"] = scope
            scoped = MODULE.scoped_pipeline_budget(state, scope)
            spent = MODULE.budget_spent(state, scoped, 0)
            self.assertIsNone(MODULE.exhausted_budget(*spent, 5, 5))
            MODULE.charge_iteration(state)
        scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=3)
        scoped = MODULE.scoped_pipeline_budget(state, scope)
        self.assertEqual(0, scope["baseline"])
        self.assertEqual(5, MODULE.budget_spent(state, scoped, 0)[1])
        self.assertEqual(
            "absolute",
            MODULE.exhausted_budget(
                *MODULE.budget_spent(state, scoped, 0), 5,
                MODULE.absolute_iteration_cap(scope, 5, 100),
            ),
        )

    def test_a_standalone_invocation_is_left_exactly_as_it_was(self):
        """Absent arguments must never read as a new run."""
        self.assertIsNone(self.scope({"iterations": 3}))
        self.assertIsNone(self.scope({"iterations": 3}, pipeline_run=None))
        self.assertIsNone(self.scope({"iterations": 3}, pipeline_run=""))
        self.assertIsNone(
            self.scope(
                {"iterations": 3}, pipeline_iteration=2, pipeline_max_iterations=4
            )
        )

    def test_a_run_this_stage_has_not_seen_starts_a_fresh_budget(self):
        scope = self.scope(
            {"iterations": 7}, pipeline_run="run-a", pipeline_iteration=1
        )

        self.assertEqual(scope["baseline"], 7)

    def test_a_relaunch_within_one_iteration_does_not_buy_a_fresh_budget(self):
        """The launch is the one event the reset must ignore."""
        state = {
            "iterations": 9,
            "pipeline_budget": {"run": "run-a", "iteration": 2, "baseline": 7},
        }

        scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=2)

        self.assertEqual(scope["baseline"], 7)

    def test_a_stale_or_replayed_iteration_is_inert(self):
        """Strictly greater, so a repeat and a replay both change nothing."""
        state = {
            "iterations": 9,
            "pipeline_budget": {"run": "run-a", "iteration": 4, "baseline": 7},
        }

        for iteration in (1, 3, 4):
            with self.subTest(iteration=iteration):
                scope = self.scope(
                    state, pipeline_run="run-a", pipeline_iteration=iteration
                )
                self.assertEqual(scope["baseline"], 7)
                self.assertEqual(scope["iteration"], 4)

    def test_a_genuine_advance_within_one_run_preserves_the_budget(self):
        state = {
            "iterations": 9,
            "pipeline_budget": {"run": "run-a", "iteration": 2, "baseline": 7},
        }

        scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=3)

        self.assertEqual(scope["baseline"], 7)
        self.assertEqual(scope["iteration"], 3)

    def test_a_new_run_resets_even_when_its_iteration_went_backwards(self):
        """An outer iteration restarts at 1 while this state is durable per PR.

        Comparing order alone would see the count go backwards on every later
        run and never reset again, holding the pull request permanently.
        """
        state = {
            "iterations": 9,
            "pipeline_budget": {"run": "run-a", "iteration": 6, "baseline": 7},
        }

        scope = self.scope(state, pipeline_run="run-b", pipeline_iteration=1)

        self.assertEqual(scope["baseline"], 9)
        self.assertEqual(scope["iteration"], 1)

    def test_the_run_is_opaque_and_only_ever_compared_for_equality(self):
        state = {
            "iterations": 4,
            "pipeline_budget": {"run": "2026-05-01/7", "iteration": 1, "baseline": 2},
        }

        same = self.scope(state, pipeline_run="2026-05-01/7", pipeline_iteration=1)
        other = self.scope(state, pipeline_run="2026-05-01/8", pipeline_iteration=1)

        self.assertEqual(same["baseline"], 2)
        self.assertEqual(other["baseline"], 4)

    def test_a_run_without_an_iteration_still_resets_on_the_run(self):
        state = {
            "iterations": 9,
            "pipeline_budget": {"run": "run-a", "iteration": 2, "baseline": 7},
        }

        self.assertEqual(self.scope(state, pipeline_run="run-a")["baseline"], 7)
        self.assertEqual(self.scope(state, pipeline_run="run-b")["baseline"], 9)

    def test_a_standalone_publication_does_not_spend_a_pipeline_budget(self):
        state = {"iterations": 5}
        scope = MODULE.scoped_pipeline_budget(
            state,
            {
                "run": "pipeline-run",
                "iteration": 1,
                "baseline": 5,
                "run_baseline": 5,
            },
        )
        state["pipeline_budget"] = {
            key: value for key, value in scope.items() if not key.startswith("_")
        }
        state["budget_scope"] = "standalone"

        MODULE.charge_iteration(state)

        self.assertEqual((0, 0), MODULE.budget_spent(state, scope, 0))
        state["budget_scope"] = "pipeline"
        MODULE.charge_iteration(state)
        self.assertEqual((1, 1), MODULE.budget_spent(state, scope, 0))

    def test_migration_seals_a_paused_pipeline_budget_before_standalone_work(self):
        state = {
            "iterations": 7,
            "pipeline_budget": {
                "run": "pipeline-run",
                "iteration": 1,
                "baseline": 5,
                "run_baseline": 5,
            },
        }
        state["budget_scope"] = "standalone"
        MODULE.charge_iteration(state)
        scope = MODULE.scoped_pipeline_budget(
            state,
            MODULE.pipeline_scope(
                state,
                SimpleNamespace(pipeline_run="pipeline-run", pipeline_iteration=1),
            ),
        )

        self.assertEqual((2, 2), MODULE.budget_spent(state, scope, 0))

    def test_direct_legacy_pipeline_publish_is_charged_after_migration(self):
        state = {
            "iterations": 7,
            "pipeline_budget": {
                "run": "pipeline-run",
                "iteration": 1,
                "baseline": 5,
                "run_baseline": 5,
            },
        }

        MODULE.charge_iteration(state)
        scope = MODULE.scoped_pipeline_budget(
            state,
            MODULE.pipeline_scope(
                state,
                SimpleNamespace(pipeline_run="pipeline-run", pipeline_iteration=1),
            ),
        )

        self.assertEqual((3, 3), MODULE.budget_spent(state, scope, 0))


class DerivedCeilingTest(unittest.TestCase):
    """The outer cap bounds the run; it does not replace the stage's own budget."""

    SCOPE = {"run": "run-a", "iteration": 2, "baseline": 9, "run_baseline": 2}

    def scope(self, state, **pipeline):
        return MODULE.pipeline_scope(state, SimpleNamespace(**pipeline))

    def run_preflight(self, stored, threads=None, **pipeline):
        metadata = {"head_branch": "branch", "head_sha": "head", "base_sha": "base"}

        def fake_git(repo_root, *arguments):
            del repo_root
            return {
                ("status", "--porcelain=v1"): "",
                ("branch", "--show-current"): "branch",
                ("rev-parse", "HEAD"): "head",
            }[arguments]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            MODULE.save_state(
                path,
                {
                    "version": MODULE.STATE_VERSION,
                    "queue": {"comments": [], "batches": []},
                    **stored,
                },
            )
            args = SimpleNamespace(
                target="owner/repo#7",
                repo_root=directory,
                state=str(path),
                max_iterations=5,
                completed_run_iterations=0,
                **pipeline,
            )

            with (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(
                    MODULE, "resolve_repo_root", return_value=Path(directory)
                ),
                mock.patch.object(MODULE, "git", side_effect=fake_git),
                mock.patch.object(MODULE, "metadata_for", return_value=metadata),
                mock.patch.object(MODULE, "checkout_pr", return_value=True),
                mock.patch.object(MODULE, "run"),
                mock.patch.object(MODULE, "fetch_threads", return_value=threads or []),
                mock.patch.object(MODULE, "fetch_reviews", return_value=[]),
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_preflight(args)

        return emit.call_args.args[0]

    def test_the_ceiling_is_the_stages_own_cap(self):
        self.assertEqual(5, MODULE.absolute_iteration_cap(self.SCOPE, 5, 3))
        self.assertEqual(10, MODULE.absolute_iteration_cap(self.SCOPE, 10, 2))
        self.assertIsNone(MODULE.absolute_iteration_cap(None, 5, 3))

    def test_an_omitted_outer_cap_does_not_change_the_stage_allowance(self):
        for value in (None, 0, -1, True, "3"):
            with self.subTest(value=value):
                self.assertEqual(
                    5,
                    MODULE.absolute_iteration_cap(self.SCOPE, 5, value),
                )

    def test_the_outer_cap_never_becomes_the_stage_budget(self):
        """Two different quantities: the caller's loop-backs and this stage's iterations.

        Reading one as the other hands the stage as few iterations as its caller
        has loop-backs, and review comments arrive in waves that two passes
        routinely fail to clear.
        """
        payload = self.run_preflight(
            {
                "iterations": 12,
                "pipeline_budget": {
                    "run": "run-a",
                    "iteration": 2,
                    "baseline": 9,
                    "run_baseline": 9,
                },
            },
            pipeline_run="run-a",
            pipeline_iteration=2,
            pipeline_max_iterations=2,
        )

        self.assertEqual(5, payload["max_iterations"])
        self.assertEqual(3, payload["completed_run_iterations"])
        self.assertEqual("review_required", payload["result"])
        self.assertIsNone(payload["budget_exhausted"])

    def test_the_whole_run_ceiling_still_stops_a_caller_that_keeps_advancing(self):
        """The stage budget being untouched must not leave the run unbounded."""
        payload = self.run_preflight(
            {
                "iterations": 12,
                "pipeline_budget": {
                    "run": "run-a",
                    "iteration": 2,
                    "baseline": 12,
                    "run_baseline": 2,
                },
            },
            pipeline_run="run-a",
            pipeline_iteration=2,
            pipeline_max_iterations=2,
        )

        self.assertEqual(0, payload["completed_run_iterations"])
        self.assertEqual(5, payload["absolute_cap"])
        self.assertEqual("absolute", payload["budget_exhausted"])
        self.assertEqual("max_iterations_reached", payload["result"])

    def test_a_genuine_advance_preserves_both_baselines(self):
        state = {"iterations": 11, "pipeline_budget": dict(self.SCOPE)}

        scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=3)

        self.assertEqual(9, scope["baseline"])
        self.assertEqual(2, scope["run_baseline"])

    def test_a_new_run_resets_both_budgets(self):
        """Starting the outer loop again is an authority outside any budget kept here."""
        state = {"iterations": 9, "pipeline_budget": dict(self.SCOPE)}

        scope = self.scope(state, pipeline_run="run-b", pipeline_iteration=1)

        self.assertEqual(9, scope["baseline"])
        self.assertEqual(9, scope["run_baseline"])

    def test_a_relaunch_leaves_both_baselines_where_they_were(self):
        state = {"iterations": 40, "pipeline_budget": dict(self.SCOPE)}

        scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=2)

        self.assertEqual(self.SCOPE, scope)

    def test_a_stored_budget_that_lost_a_number_does_not_crash_the_run(self):
        """State files are durable, so a value from any earlier version reaches this.

        Coercing a stored baseline directly raises on ``null`` and on anything
        else that is not a number, and it raises on the ordinary relaunch path
        rather than on some rare branch.
        """
        for stored in (
            {"baseline": None, "run_baseline": None},
            {"baseline": "7", "run_baseline": "2"},
            {"baseline": -1, "run_baseline": -1},
            {"baseline": True, "run_baseline": False},
            {},
        ):
            with self.subTest(stored=stored):
                state = {
                    "iterations": 9,
                    "pipeline_budget": {"run": "run-a", "iteration": 2, **stored},
                }

                scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=2)

                self.assertEqual(9, scope["baseline"])
                self.assertEqual(9, scope["run_baseline"])
                self.assertIsNone(
                    MODULE.exhausted_budget(
                        *MODULE.budget_spent(state, scope, 0), 5, 10
                    )
                )

    def test_a_standalone_invocation_still_counts_what_the_agent_counts(self):
        """The standalone budget stays the agent's per-invocation count, not the durable one.

        This loop is alone among the pipeline stages in taking that count from
        its caller, and nothing here changes it.
        """
        self.assertEqual((3, 3), MODULE.budget_spent({"iterations": 40}, None, 3))
        self.assertEqual("iteration", MODULE.exhausted_budget(5, 5, 5, None))
        self.assertIsNone(MODULE.exhausted_budget(4, 4, 5, None))

    def test_a_spent_stage_budget_is_never_a_permanent_refusal(self):
        """Forty iterations over the pull request's life say nothing about this run."""
        scope = {"run": "run-a", "iteration": 1, "baseline": 40, "run_baseline": 40}
        state = {"iterations": 40}

        self.assertIsNone(
            MODULE.exhausted_budget(*MODULE.budget_spent(state, scope, 0), 5, 10)
        )

    def test_the_agent_file_states_the_outer_cap_as_a_bound_on_the_run(self):
        """Left as a replacement in prose, the next reader reinstates it in code."""
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("iteration budgets", instructions)

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


class SourceDriftClassificationTest(unittest.TestCase):
    def setUp(self):
        self.expected = {
            "number": 7,
            "repo_name": "owner/repo",
            "title": "Title",
            "body": "Body",
            "head_owner": "owner",
            "head_repo": "repo",
            "head_branch": "feature",
            "head_sha": "1" * 40,
            "base_branch": "main",
            "base_sha": "2" * 40,
            "state": "OPEN",
        }

    def test_accepts_only_verified_same_ref_forward_movement(self):
        actual = {**self.expected, "head_sha": "3" * 40}
        with mock.patch.object(
            MODULE, "gh_json", return_value={"status": "ahead"}
        ) as compare:
            self.assertTrue(MODULE.same_ref_forward_head_drift(self.expected, actual))
        self.assertIn(
            f"{self.expected['head_sha']}...{actual['head_sha']}",
            compare.call_args.args[0][1],
        )

        with mock.patch.object(MODULE, "gh_json") as compare:
            self.assertFalse(
                MODULE.same_ref_forward_head_drift(
                    self.expected, {**actual, "base_branch": "release"}
                )
            )
        compare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
