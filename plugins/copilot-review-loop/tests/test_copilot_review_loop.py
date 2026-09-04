import argparse
import importlib.util
import ast
import json
from pathlib import Path
import re
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "copilot_review_loop.py"
AGENT = (
    Path(__file__).parents[1]
    / "agents"
    / "copilot-review-loop.agent.md"
)
SPEC = importlib.util.spec_from_file_location("copilot_review_loop", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


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
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "watcher_result":
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


class AgentInstructionsTest(unittest.TestCase):
    def setUp(self):
        self.instructions = AGENT.read_text(encoding="utf-8")

    def test_documents_the_helper_activity_stamp_without_overselling_it(self):
        """A reader who thinks the stamp proves liveness stops checking further.

        The helper writes only when a subcommand runs, so an hour of silence is
        as consistent with hard thinking as with a hang.
        """
        self.assertIn("`last_helper_activity`", AGENT.read_text(encoding="utf-8"))
        self.assertIn(
            "the moment this helper last wrote its state", AGENT.read_text(encoding="utf-8")
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
        self.assertIn(
            "never let it read like an ordinary uneventful run", instructions
        )

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

        self.assertIn(
            "`status` also reports `stage_outcome`", instructions
        )
        self.assertIn(
            "`cleared`, `skipped`, `no_progress`, `escalated`, or `carried`",
            instructions,
        )
        self.assertIn(
            "It never says whether this stage is green, because `clean_at_head_sha` "
            "alone says that",
            instructions,
        )
        self.assertIn(
            "do not set, quote, or work around either one", instructions
        )
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

        self.assertIn(
            "${COPILOT_HOME:-${USERPROFILE//\\\\//}/.copilot}", instructions
        )
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
        self.assertIn("either `--commit <sha>` or the no-code `--rationale <text>`", instructions)
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
        self.assertIn(
            "`preflight --completed-run-iterations <n>`", instructions
        )
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
        self.assertIn("read the message back with `git log -1 --pretty=%B`", instructions)

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

        self.assertIn(
            "## Copilot Review Loop Agent Retrospective", instructions
        )
        self.assertIn(
            "**Copilot Review Loop Agent Retrospective**", instructions
        )
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
        self.assertIn(
            "Report only friction you actually hit in this run", instructions
        )
        self.assertIn(
            "The **Copilot Review Loop Agent Retrospective** is the only content "
            "allowed after the `**Outcome:**` line",
            instructions,
        )
        self.assertIn("The retrospective is advice, and it belongs in chat only", instructions)
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

        self.assertIn(
            "The terminal response is the run's last message", instructions
        )
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
        self.assertIn(
            "Finish every tool call the run needs", instructions
        )
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
        target = MODULE.parse_target("open-telemetry/opentelemetry-java-instrumentation#19233")

        self.assertEqual(target["owner"], "open-telemetry")
        self.assertEqual(target["number"], 19233)

    def test_rejects_a_non_pull_request_target(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.parse_target("https://github.com/open-telemetry/repo/issues/7")

    def test_resolve_target_falls_back_to_the_current_pr(self):
        target = MODULE.parse_target("https://github.com/open-telemetry/repo/pull/42")

        with mock.patch.object(MODULE, "current_pr_target", return_value=target) as current:
            self.assertEqual(MODULE.resolve_target(None, Path("repo")), target)
            self.assertEqual(
                MODULE.resolve_target("open-telemetry/repo#43", Path("repo"))["number"], 43
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
            mock.patch.object(
                MODULE, "cli_path", return_value=Path(r"C:\src\repo")
            ),
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

        with mock.patch.object(
            MODULE, "gh_json", return_value=metadata
        ) as gh_json, mock.patch.object(
            MODULE, "base_ref_tip", return_value="live-tip"
        ):
            result = MODULE.metadata_for(target)

        self.assertEqual(result["title"], "Fix the review loop")
        self.assertIn("title", gh_json.call_args.args[0][-1].split(","))

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

        with mock.patch.object(
            MODULE, "gh_json", return_value=metadata
        ), mock.patch.object(
            MODULE, "base_ref_tip", return_value="live-tip"
        ) as tip:
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
            self.assertRaisesRegex(MODULE.WorkflowError, "head repository is unavailable"),
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
            mock.patch.object(
                MODULE, "configured_upstream", return_value=upstream
            ),
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
            ): mock.Mock(
                returncode=0, stdout="git@github.com:trask/repo.git\n"
            ),
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
            mock.patch.object(
                MODULE, "configured_upstream", return_value=upstream
            ),
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
            mock.patch.object(
                MODULE, "configured_upstream", return_value=upstream
            ),
            mock.patch.object(
                MODULE, "exact_upstream_pr_targets", return_value=targets
            ),
            self.assertRaisesRegex(
                MODULE.WorkflowError, "multiple open pull requests"
            ),
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
            mock.patch.object(
                MODULE, "configured_upstream", return_value=upstream
            ),
            mock.patch.object(MODULE, "exact_upstream_pr_targets", return_value=[]),
            self.assertRaisesRegex(MODULE.WorkflowError, "no open pull request"),
        ):
            MODULE.current_pr_target(Path("repo"))

    def test_reports_failed_lookup_without_an_upstream(self):
        with (
            mock.patch.object(MODULE, "git", return_value="topic"),
            mock.patch.object(MODULE, "configured_upstream", return_value=None),
            mock.patch.object(
                MODULE, "simple_current_pr_target", return_value=None
            ),
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
                "nodes": [
                    {"databaseId": 50, "author": {"login": "settled-reviewer"}}
                ]
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
            MODULE.review_has_inline_findings(
                review, [self.resolved_copilot_thread]
            )
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
        self.assertTrue(all(comment["source"] == "suppressed" for comment in first_parse))
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
            mock.patch.object(MODULE, "remote_head", return_value="old-head") as remote_head,
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


class ParserTest(unittest.TestCase):
    def test_plan_accumulates_repeated_path_flags(self):
        args = MODULE.build_parser().parse_args(
            [
                "plan",
                "--state",
                "state.json",
                "--batch",
                "batch-1",
                "--comments",
                "1",
                "--label",
                "Fix paths",
                "--paths",
                "one.java",
                "two.java",
                "--paths",
                "three.java",
            ]
        )

        self.assertEqual(args.paths, ["one.java", "two.java", "three.java"])


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
            mock.patch.object(
                MODULE, "gh_json", side_effect=fake_gh_json
            ) as gh_json,
            mock.patch.object(MODULE, "graphql") as graphql,
        ):
            reply_ids = MODULE.post_missing_replies(state, comments)

        self.assertEqual(reply_ids, {10: 11, 20: 21})
        self.assertEqual(comments[0]["reply_id"], 11)
        self.assertEqual(comments[1]["reply_id"], 21)
        # A single bundled review is never created for the replies.
        graphql.assert_not_called()
        posts = [call for call in gh_json.call_args_list if call.args[0] != ["api", "user"]]
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
        self.assertEqual(
            gh_json.call_count, len(MODULE.PR_HEAD_LAG_RETRY_DELAYS) + 1
        )
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
            mock.patch.object(MODULE, "lookup_copilot_bot", side_effect=[None, "BOT_1"]),
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
            mock.patch.object(MODULE, "run", return_value=SimpleNamespace(returncode=0)),
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
            mock.patch.object(MODULE, "run", return_value=SimpleNamespace(returncode=0)),
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
                    returncode=1, stderr="HTTP 422: Reviews may only be requested\n", stdout=""
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
        stamps = iter(
            [f"2026-05-01T12:00:{second:02d}Z" for second in range(30)]
        )

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
    def run_preflight(self, *, threads=None, reviews=None, prior_clean_at_head_sha=None):
        metadata = {"head_branch": "branch", "head_sha": "head"}

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

    def run_watch(self, *, review_comments, body):
        state = {
            "version": MODULE.STATE_VERSION,
            "pr": {"upstream_owner": "owner", "upstream_repo": "repo", "number": 42},
            "monitoring": {
                "status": "requested",
                "head_sha": "head",
                "baseline_review_id": 100,
                "copilot_bot_id": "BOT_1",
                "request_start": "2026-05-01T12:00:00Z",
                "cancel_requested": False,
            },
        }
        review = {"id": 101, "html_url": "https://example.test/review/101", "body": body}

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
                mock.patch.object(
                    MODULE, "gh_paginated", return_value=review_comments
                ),
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
        self.assertIn("progress --state <path> --phase validating", instructions)
        self.assertIn(
            "progress --state <path> --phase addressing_comments", instructions
        )


class StageOutcomeTest(unittest.TestCase):
    """The vocabulary an external orchestrator reads instead of the prose report."""

    PIPELINE_VOCABULARY = ("cleared", "skipped", "no_progress", "escalated", "carried")

    def test_a_clearance_is_read_off_the_marker_and_never_decided_again(self):
        self.assertEqual(MODULE.stage_outcome({"clean_at_head_sha": "abc123"}), "cleared")

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
            MODULE.save_state(
                path, {"version": MODULE.STATE_VERSION, "monitoring": {}}
            )
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
            args = SimpleNamespace(
                state=str(path), interval=0, cancellation_grace=0
            )
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
            args = SimpleNamespace(
                state=str(path), interval=0, cancellation_grace=0
            )

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
        self.assertEqual(
            state["monitoring"]["result"], {"result": "cancelled_locally"}
        )

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
        self.assertEqual(
            state["monitoring"]["result"], {"result": "cancelled_locally"}
        )

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
                mock.patch.object(
                    MODULE, "load_state", side_effect=[state, completed]
                ),
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
        self.assertEqual(
            saved["monitoring"]["result"], {"result": "cancelled_locally"}
        )
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
        metadata = {"head_branch": "branch", "head_sha": "head"}

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
                mock.patch.object(MODULE, "resolve_repo_root", return_value=Path(directory)),
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
        self.assertEqual(
            saved["monitoring"]["result"], {"result": "cancelled_locally"}
        )
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
        metadata = {"head_branch": "branch", "head_sha": "head"}

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
                mock.patch.object(MODULE, "resolve_repo_root", return_value=Path(directory)),
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
        metadata = {"head_branch": "branch", "head_sha": "head"}
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
                mock.patch.object(MODULE, "resolve_repo_root", return_value=Path(directory)),
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

    def test_preflight_reports_when_only_human_comments_remain(self):
        metadata = {"head_branch": "branch", "head_sha": "head"}
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
                mock.patch.object(MODULE, "resolve_repo_root", return_value=Path(directory)),
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
                            "user": {
                                "login": "copilot-pull-request-reviewer[bot]"
                            },
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
        metadata = {"head_branch": "branch", "head_sha": "head"}
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
                mock.patch.object(MODULE, "resolve_repo_root", return_value=Path(directory)),
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
    """A stage budget belongs to an outer loop's iteration, not to a launch."""

    def scope(self, state, **pipeline):
        return MODULE.pipeline_scope(state, SimpleNamespace(**pipeline))

    def test_a_standalone_invocation_is_left_exactly_as_it_was(self):
        """Absent arguments must never read as a new run."""
        self.assertIsNone(self.scope({"iterations": 3}))
        self.assertIsNone(self.scope({"iterations": 3}, pipeline_run=None))
        self.assertIsNone(self.scope({"iterations": 3}, pipeline_run=""))
        self.assertIsNone(
            self.scope({"iterations": 3}, pipeline_iteration=2, pipeline_max_iterations=4)
        )

    def test_a_run_this_stage_has_not_seen_starts_a_fresh_budget(self):
        scope = self.scope({"iterations": 7}, pipeline_run="run-a", pipeline_iteration=1)

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

    def test_a_genuine_advance_within_one_run_resets_the_budget(self):
        state = {
            "iterations": 9,
            "pipeline_budget": {"run": "run-a", "iteration": 2, "baseline": 7},
        }

        scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=3)

        self.assertEqual(scope["baseline"], 9)
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
                SimpleNamespace(
                    pipeline_run="pipeline-run", pipeline_iteration=1
                ),
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
                SimpleNamespace(
                    pipeline_run="pipeline-run", pipeline_iteration=1
                ),
            ),
        )

        self.assertEqual((3, 3), MODULE.budget_spent(state, scope, 0))


class DerivedCeilingTest(unittest.TestCase):
    """The outer cap bounds the run; it does not replace the stage's own budget."""

    SCOPE = {"run": "run-a", "iteration": 2, "baseline": 9, "run_baseline": 2}

    def scope(self, state, **pipeline):
        return MODULE.pipeline_scope(state, SimpleNamespace(**pipeline))

    def run_preflight(self, stored, threads=None, **pipeline):
        metadata = {"head_branch": "branch", "head_sha": "head"}

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

    def test_the_ceiling_is_derived_from_the_callers_own_cap(self):
        self.assertEqual(15, MODULE.absolute_iteration_cap(self.SCOPE, 5, 3))
        self.assertEqual(20, MODULE.absolute_iteration_cap(self.SCOPE, 10, 2))
        self.assertIsNone(MODULE.absolute_iteration_cap(None, 5, 3))

    def test_an_omitted_outer_cap_falls_back_rather_than_disabling_the_ceiling(self):
        """Only the outer cap is optional, and omitting it must not remove the bound."""
        for value in (None, 0, -1, True, "3"):
            with self.subTest(value=value):
                self.assertEqual(
                    5 * MODULE.DEFAULT_PIPELINE_MAX_ITERATIONS,
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
        self.assertEqual(10, payload["absolute_cap"])
        self.assertEqual("absolute", payload["budget_exhausted"])
        self.assertEqual("max_iterations_reached", payload["result"])

    def test_a_genuine_advance_refreshes_only_the_per_iteration_budget(self):
        """The whole-run ceiling must survive an advance, or it bounds nothing."""
        state = {"iterations": 9, "pipeline_budget": dict(self.SCOPE)}

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
                    MODULE.exhausted_budget(*MODULE.budget_spent(state, scope, 0), 5, 10)
                )

    def test_a_standalone_invocation_still_counts_what_the_agent_counts(self):
        """The standalone budget stays the agent's per-invocation count, not the durable one.

        This loop is alone among the pipeline stages in taking that count from
        its caller, and nothing here changes it.
        """
        self.assertEqual((3, 3), MODULE.budget_spent({"iterations": 40}, None, 3))
        self.assertEqual(
            "iteration", MODULE.exhausted_budget(5, 5, 5, None)
        )
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

        self.assertIn(
            "An outer loop does not raise or lower that; it bounds what the whole "
            "run may spend instead.",
            instructions,
        )

    def test_preflight_documents_the_ceiling_rather_than_a_replacement(self):
        """The flag reads as a replacement unless its help says otherwise."""
        parser = MODULE.build_parser()
        bare = parser.parse_args(["preflight"])
        self.assertIsNone(bare.pipeline_max_iterations)

        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("used to derive the ceiling ", source)
        self.assertIn("rather than to replace the per-iteration budget", source)


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