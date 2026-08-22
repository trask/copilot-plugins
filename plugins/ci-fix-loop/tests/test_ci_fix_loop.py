import argparse
import contextlib
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "ci_fix_loop.py"
AGENT = Path(__file__).parents[1] / "agents" / "ci-fix-loop.agent.md"
SPEC = importlib.util.spec_from_file_location("ci_fix_loop", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


NOW = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 import os
-value = 1
+value = 2
 print(value)
"""


def check(key, name=None, klass="failed", url=None, completed_at=None):
    return {
        "kind": "check_run",
        "key": key,
        "name": name or key.split(":", 1)[-1],
        "workflow": None,
        "status": None,
        "conclusion": None,
        "state": None,
        "class": klass,
        "url": url,
        "started_at": None,
        "completed_at": completed_at,
        "description": None,
    }


def stamp(minutes_ago=0):
    moment = NOW - dt.timedelta(minutes=minutes_ago)
    return moment.isoformat().replace("+00:00", "Z")


def run_arguments(*arguments):
    parser = MODULE.build_parser()
    return parser.parse_args(list(arguments))


def call(*arguments):
    args = run_arguments(*arguments)
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        args.function(args)
    return json.loads(stream.getvalue())


def write_state(directory: Path, **overrides) -> Path:
    state = {
        "version": MODULE.STATE_VERSION,
        "created_at": "2026-01-01T00:00:00Z",
        "iterations": 1,
        "history": [],
        "reruns": {},
        "escalation": None,
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
            "is_fork": True,
            "is_draft": True,
            "commits": [],
        },
        "run": {
            "id": "pr-7-iteration-1",
            "status": "active",
            "iteration": 1,
            "head_sha": "head1",
            "base_sha": "base1",
            "diff_path": str(directory / "state.json.diff"),
            "changed_files": ["app.py"],
            "pr_commits": [],
            "checks": [],
            "attributions": {},
            "batches": [],
            "tracking": {},
            "decision": None,
        },
    }
    for key, value in overrides.items():
        if key in {"run", "pr"} and isinstance(value, dict):
            state[key] = {**state[key], **value}
        else:
            state[key] = value
    path = directory / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    (directory / "state.json.diff").write_text(DIFF, encoding="utf-8")
    return path


def attribution(key, verdict, *, source="baseline", baseline=None, conclusion=None):
    return {
        "key": key,
        "name": key.split(":", 1)[-1],
        "verdict": verdict,
        "source": source,
        "baseline_conclusion": conclusion,
        "baseline_verdict": baseline if baseline is not None else verdict,
        "rationale": None,
    }


LOCAL_VALIDATION_HEADING = "## Local Validation Before A Push"
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

    def test_declares_the_frontmatter_the_siblings_use(self):
        self.assertIn("name: CI Fix Loop", self.instructions)
        self.assertIn(
            'argument-hint: "PR URL, PR number, or owner/repo#number; omit only '
            "from a worktree attached to the PR's branch\"",
            self.instructions,
        )
        self.assertIn(
            "tools: [read, edit, search, execute, agent, todo, rename_session]",
            self.instructions,
        )
        self.assertIn("user-invocable: true", self.instructions)
        self.assertIn("disable-model-invocation: true", self.instructions)

    def test_declares_no_model_frontmatter_key(self):
        frontmatter = self.instructions.split("---")[1]
        self.assertNotIn("\nmodel:", frontmatter)

    def test_tells_the_agent_that_no_progress_is_its_claim_to_make(self):
        self.assertIn(
            "It reports `cleared`, `skipped`, `escalated`, and `carried`, and it "
            "leaves the field out entirely when the state names no ending.",
            self.instructions,
        )
        self.assertIn(
            "No progress is the one ending only you can report.", self.instructions
        )
        self.assertIn(
            "a run killed part way through leaves state that looks exactly like a "
            "run still going",
            self.instructions,
        )

    def test_states_the_suppression_refusal_and_what_to_do_about_it(self):
        self.assertIn(
            "`record` and `publish` both read the commit and stop the run when it "
            "deletes a test file, or adds a skip, disable, or ignore annotation to "
            "one.",
            self.instructions,
        )
        self.assertIn(
            "That refusal has no override and no rationale gets past it",
            self.instructions,
        )

    def test_passes_a_launchers_loop_position_through_without_reading_it(self):
        """The budget only bounds anything if the agent cannot supply the reset."""
        self.assertIn("### A Launcher's Loop Position", self.instructions)
        self.assertIn(
            "`pipeline-run: <token> pipeline-iteration: <number> "
            "pipeline-max-iterations: <number>`",
            self.instructions,
        )
        self.assertIn(
            "--pipeline-run <token> --pipeline-iteration <number> "
            "--pipeline-max-iterations <number>",
            self.instructions,
        )
        self.assertIn(
            "A value you produced would be this loop refreshing its own cap",
            self.instructions,
        )
        self.assertIn(
            "never invent one to keep working after `max_iterations_reached`",
            self.instructions,
        )

    def test_keys_the_position_on_the_values_rather_than_one_spelling(self):
        """A launcher that words it differently still gets its budget scoped.

        Making one phrasing the trigger drops a position supplied any other way,
        and it drops it silently: the run reports cleanly and the budget was
        simply never scoped. The rule is about where a value came from.
        """
        self.assertIn("Read the values, not the spelling.", self.instructions)
        self.assertIn(
            "a spelling you do not recognize is still the caller's instruction",
            self.instructions,
        )
        self.assertIn(
            "Omit all three only when the request names no position at all",
            self.instructions,
        )
        self.assertIn(
            "Send `--pipeline-run` and `--pipeline-iteration` together",
            self.instructions,
        )
        self.assertNotIn("if the line is absent, omit all three", self.instructions)

    def test_runs_the_whole_loop_from_a_bare_reference(self):
        self.assertIn("## Activation: Bare PR References Run The Full Loop", self.instructions)
        self.assertIn(
            "Start the helper's `preflight` workflow at once", self.instructions
        )
        self.assertIn("Do not ask what action the user wants", self.instructions)

    def test_never_posts_anything_to_github(self):
        self.assertIn(
            "This agent never posts anything to GitHub.", self.instructions
        )
        self.assertIn(
            "It writes no comment, no review, no reply, and no label.",
            self.instructions,
        )
        self.assertIn(
            "say what you would have posted in your final response instead",
            self.instructions,
        )
        self.assertIn("Do not post any of this to GitHub.", self.instructions)

    def test_names_the_session_from_preflight_metadata_idempotently(self):
        self.assertIn("## Session Naming", self.instructions)
        self.assertIn(
            "ensure the session name is `CI Fix Loop: <PR number> - <PR title>`",
            self.instructions,
        )
        self.assertIn(
            "If the harness has already supplied a name beginning "
            "`CI Fix Loop: <PR number> - `",
            self.instructions,
        )
        self.assertIn("do not call `rename_session`", self.instructions)
        self.assertIn("Otherwise call `rename_session` once", self.instructions)

    def test_fixes_only_failures_this_pull_request_caused(self):
        self.assertIn(
            "Fix only a failure this pull request plausibly caused.", self.instructions
        )
        self.assertIn(
            "editing this pull request to hide it is worse than leaving it alone",
            self.instructions,
        )

    def test_reruns_a_suspected_flake_exactly_once(self):
        self.assertIn("Re-run a suspected flake exactly once.", self.instructions)
        self.assertIn(
            "If it fails again, it is not a flake, so escalate", self.instructions
        )

    def test_escalates_checks_that_cannot_resolve_on_their_own(self):
        self.assertIn(
            "A check that never starts, and a check that waits for a maintainer to "
            "approve a fork's workflow run, escalates straight away.",
            self.instructions,
        )
        self.assertIn("Never wait for one of those indefinitely", self.instructions)

    def test_treats_a_repository_without_checks_as_a_visible_skip(self):
        self.assertIn(
            "A pull request whose head reports no applicable checks is a skip, never "
            "a pass.",
            self.instructions,
        )
        self.assertIn(
            "the helper already recorded the terminal skip in the same atomic state "
            "write that observed it",
            self.instructions,
        )
        self.assertIn(
            "A broken continuous integration configuration must never look like a "
            "green pipeline.",
            self.instructions,
        )

    def test_caps_the_loop_at_five_iterations(self):
        self.assertIn("The maximum is 5 iterations", self.instructions)
        self.assertIn("max_iterations_reached", self.instructions)

    def test_says_an_iteration_is_charged_per_head_rather_than_per_launch(self):
        """The prose is what the next reader believes, so it has to say which it is.

        An agent that thinks a relaunch costs an iteration rations its own reads
        of the checks, and one that starts over at an unchanged head after a
        re-run would otherwise burn a fifth of the budget on the same analysis.
        """
        self.assertIn(
            "an iteration is charged per head rather than per launch",
            self.instructions,
        )
        self.assertIn(
            "only moving the head to a new commit spends the next one",
            self.instructions,
        )

    def test_never_weakens_a_check_to_make_it_pass(self):
        self.assertIn(
            "Never disable, delete, skip, or weaken a check to make it pass.",
            self.instructions,
        )
        self.assertIn(
            "Never touch a test's expectations to match broken behavior.",
            self.instructions,
        )

    def test_documents_the_helper_invocation_for_each_shell(self):
        self.assertIn("## Mechanical Helper", self.instructions)
        for shell in ("Git Bash on Windows", "PowerShell on Windows", "POSIX shells"):
            self.assertIn(shell, self.instructions)
        self.assertIn(
            "installed-plugins/trask-plugins/ci-fix-loop/scripts/ci_fix_loop.py",
            self.instructions,
        )
        self.assertIn(
            "Never pass a `~`-prefixed helper path to native Windows Python from "
            "Git Bash.",
            self.instructions,
        )

    def test_documents_every_helper_command(self):
        for command in (
            "`preflight ",
            "`checks --state",
            "`attribute --state",
            "`rerun --state",
            "`plan --state",
            "`record` and `skip`",
            "`escalate --state",
            "`resolve --state",
            "`publish --state",
            "`status [--state",
            "`cleanup --state",
        ):
            self.assertIn(command, self.instructions)

    def test_names_the_status_command_as_the_machine_readable_outcome(self):
        self.assertIn(
            "This is the machine-readable outcome an orchestrator reads.",
            self.instructions,
        )

    def test_carries_the_expected_workflow_sections(self):
        for heading in (
            "## Non-Negotiable Rules",
            "## Plain Language",
            "## Target And Preflight",
            "## What Green Means Here",
            "## Reading The Checks",
            "## Attributing A Failure",
            "## Fixing A Failure",
            "## Commit Content",
            "## Publishing And The Next Iteration",
            "## Final Report",
        ):
            self.assertIn(heading, self.instructions)

    def test_reads_greenness_from_github_rather_than_from_its_own_state(self):
        self.assertIn(
            "GitHub states whether the checks pass, and this loop's own state never "
            "does.",
            self.instructions,
        )
        self.assertIn(
            "checks that passed and then failed again at the same head must show "
            "through",
            self.instructions,
        )

    def test_treats_a_relaunch_at_a_cleared_head_as_ordinary(self):
        self.assertIn(
            "Being asked to run again at a head you already cleared is normal, not a "
            "fault.",
            self.instructions,
        )
        self.assertIn(
            "A run that finds nothing to fix spends no iteration", self.instructions
        )

    def test_states_a_skip_an_orchestrator_cannot_miss(self):
        self.assertIn(
            "`Outcome: skipped, because this repository runs no applicable checks on "
            "this pull request.`",
            self.instructions,
        )
        self.assertIn("the helper's `skip_note` verbatim", self.instructions)
        self.assertIn(
            "never let a run end without saying it when `checks` reported "
            "`no_checks`",
            self.instructions,
        )

    def test_never_ends_a_run_silently(self):
        self.assertIn("`Outcome: no progress.`", self.instructions)
        self.assertIn(
            "a run that says nothing reads as a stall and, twice in a row, stops a "
            "whole pipeline",
            self.instructions,
        )
        self.assertIn("Report it as no progress", self.instructions)

    def test_does_not_credit_the_failure_a_rerun_replaces(self):
        self.assertIn(
            "It records the moment it asked before it asks", self.instructions
        )
        self.assertIn(
            "Never read the failure still showing just after the request as the "
            "re-run's answer.",
            self.instructions,
        )
        self.assertIn(
            "A failure that was already on record when the re-run was requested is "
            "the old one, not a second failure.",
            self.instructions,
        )

    def test_ties_the_evidence_it_credits_to_the_pinned_head(self):
        self.assertIn(
            "Every check the loop credits belongs to the head it pinned.",
            self.instructions,
        )
        self.assertIn(
            "a check that ran on an earlier commit can never clear this one",
            self.instructions,
        )

    def test_ends_the_run_with_a_single_terminal_response(self):
        self.assertIn(
            "The terminal response is the run's last message.", self.instructions
        )
        self.assertIn("Send one message that calls no tool.", self.instructions)

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
            "GitHub says whether they pass and this loop never does",
            self.instructions,
        )

    def test_requires_reproducing_the_failing_check_before_fixing_it(self):
        """This stage knows which check failed, so it must not guess.

        A guess costs a whole CI cycle, which is the most expensive mistake
        available to a loop whose entire cost model is round trips.
        """
        self.assertIn("Reproduce the failure locally.", self.instructions)
        self.assertIn(
            "confirm it fails the same way it failed in CI", self.instructions
        )
        self.assertIn(
            "Run that same command again, and confirm it now passes",
            self.instructions,
        )

    def test_a_failure_that_cannot_be_reproduced_here_still_publishes(self):
        """Checks needing containers or credentials only run in CI.

        Refusing to push those would turn an ordinary repository into an
        escalation, which is worse than the failure this section prevents.
        """
        section = _agent_section(self.instructions, LOCAL_VALIDATION_HEADING)
        self.assertIn("must never stop this loop", section)

    def test_routes_a_no_target_request_around_a_detached_worktree(self):
        """A stage the pipeline launches has no branch checked out.

        A reader who copies the bare no-target form there reaches a resolver
        that refuses on purpose, so both steps have to name what to pass
        instead of leaving the refusal as the answer.
        """
        self.assertIn(
            "`--current` resolves through the branch that is checked out, which "
            "a detached worktree does not have, so pass `--state <path>` when "
            "the worktree is detached.",
            self.instructions,
        )
        self.assertIn(
            "a stage the pipeline launches works in a worktree detached at the "
            "pull request head, so name the pull request as a URL or "
            "`owner/repo#number` instead",
            self.instructions,
        )
        self.assertIn(
            "The bare form is for a checkout still sitting on a branch, and "
            "this loop's ordinary case under a pipeline is not one.",
            self.instructions,
        )

    def test_the_current_rule_admits_a_detached_worktree_has_no_pull_request(self):
        """The rule still rightly forbids picking a state file by hand.

        It was only wrong to imply a checked-out branch is always there to ask.
        """
        self.assertIn(
            "`current` always means the pull request attached to the branch "
            "that is checked out, and a detached worktree has no such pull "
            "request",
            self.instructions,
        )

    def test_the_argument_hint_stops_selling_the_bare_form_as_the_default(self):
        """The hint is the shape a caller copies before reaching any step list.

        It used to promise the current branch's PR, which a detached worktree
        cannot supply, so the omission read as the ordinary way to call this.
        The sibling frontmatter test pins the whole line; this one says why the
        clause is worded the way it is.
        """
        self.assertIn(
            "omit only from a worktree attached to the PR's branch", self.instructions
        )
        self.assertNotIn("omit to use the current branch's PR", self.instructions)


class EscalationCatalogTest(unittest.TestCase):
    def test_every_reason_carries_a_concrete_next_action(self):
        for reason in MODULE.ESCALATION_REASONS:
            self.assertIn(reason, MODULE.ESCALATION_ACTIONS)
            self.assertTrue(MODULE.ESCALATION_ACTIONS[reason].strip())

    def test_the_iteration_cap_and_rerun_cap_match_the_design(self):
        self.assertEqual(5, MODULE.DEFAULT_MAX_ITERATIONS)
        self.assertEqual(1, MODULE.MAX_RERUNS_PER_CHECK)

    def test_verdicts_are_exactly_the_three_the_loop_understands(self):
        self.assertEqual(("pr_caused", "pre_existing", "flake"), MODULE.VERDICTS)


class ParseTargetTest(unittest.TestCase):
    def test_accepts_a_pull_request_url(self):
        target = MODULE.parse_target("https://github.com/owner/repo/pull/7")
        self.assertEqual("owner", target["owner"])
        self.assertEqual("repo", target["repo"])
        self.assertEqual(7, target["number"])
        self.assertEqual("owner/repo", target["repo_name"])

    def test_accepts_a_url_with_a_fragment(self):
        target = MODULE.parse_target(
            "https://github.com/owner/repo/pull/7#issuecomment-1"
        )
        self.assertEqual(7, target["number"])

    def test_accepts_owner_repo_number(self):
        target = MODULE.parse_target("owner/repo#42")
        self.assertEqual("https://github.com/owner/repo/pull/42", target["pr_url"])

    def test_rejects_a_bare_number(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.parse_target("42")

    def test_rejects_an_issue_url(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.parse_target("https://github.com/owner/repo/issues/7")


def ci_gh_metadata(**overrides):
    payload = {
        "number": 7,
        "title": "Add a thing",
        "url": "https://github.com/owner/repo/pull/7",
        "state": "OPEN",
        "isDraft": False,
        "headRefName": "feature",
        "headRefOid": "head1",
        "headRepositoryOwner": {"login": "fork"},
        "headRepository": {"name": "repo"},
        "baseRefName": "main",
        "baseRefOid": "frozen",
        "commits": [{"oid": "head1", "messageHeadline": "Add a thing"}],
    }
    payload.update(overrides)
    return payload


class PullRequestMetadataTest(unittest.TestCase):
    def test_base_sha_is_the_live_base_branch_tip_not_the_frozen_base_ref_oid(self):
        target = MODULE.parse_target("owner/repo#7")
        with mock.patch.object(
            MODULE, "gh_json", return_value=ci_gh_metadata()
        ), mock.patch.object(
            MODULE, "base_ref_tip", return_value="live-tip"
        ) as tip:
            metadata = MODULE.metadata_for(target)
        # The base commit is what baseline_conclusions attributes against, so it
        # must be the branch's live tip, never GitHub's frozen baseRefOid.
        self.assertEqual("live-tip", metadata["base_sha"])
        self.assertEqual("main", metadata["base_branch"])
        tip.assert_called_once_with("owner/repo", "main")

    def test_a_missing_base_branch_is_rejected(self):
        target = MODULE.parse_target("owner/repo#7")
        with mock.patch.object(
            MODULE, "gh_json", return_value=ci_gh_metadata(baseRefName=None)
        ), mock.patch.object(MODULE, "base_ref_tip", return_value="live-tip"):
            with self.assertRaisesRegex(MODULE.WorkflowError, "no base branch"):
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


class PathHelperTest(unittest.TestCase):
    def test_state_path_uses_the_orchestrator_naming(self):
        target = MODULE.parse_target("owner/repo#7")
        path = MODULE.default_state_path(target)
        self.assertEqual("owner--repo--7.json", path.name)
        self.assertEqual("ci-fix-loop", path.parent.name)
        self.assertEqual("run", path.parent.parent.name)

    def test_side_files_hang_off_the_state_path(self):
        path = Path("/tmp/state.json")
        self.assertEqual("state.json.diff", MODULE.diff_path_for(path).name)
        self.assertEqual(
            "state.json.preflight.json", MODULE.preflight_path_for(path).name
        )
        self.assertEqual("state.json.checks.json", MODULE.checks_path_for(path).name)
        self.assertEqual("state.json.status.json", MODULE.status_path_for(path).name)

    def test_normalizes_git_bash_paths_only_on_windows(self):
        self.assertEqual(
            "C:/Users/x/.copilot",
            MODULE.normalize_cli_path("/c/Users/x/.copilot", windows=True),
        )
        self.assertEqual(
            "/c/Users/x/.copilot",
            MODULE.normalize_cli_path("/c/Users/x/.copilot", windows=False),
        )

    def test_reads_a_github_repository_from_any_remote_form(self):
        self.assertEqual(
            "owner/repo",
            MODULE.github_repo_from_remote("https://github.com/owner/repo.git"),
        )
        self.assertEqual(
            "owner/repo", MODULE.github_repo_from_remote("git@github.com:owner/repo")
        )
        self.assertIsNone(MODULE.github_repo_from_remote("https://example.com/o/r"))


class ClassificationTest(unittest.TestCase):
    def test_maps_completed_check_run_conclusions(self):
        self.assertEqual("passed", MODULE.classify_check_run("COMPLETED", "SUCCESS"))
        self.assertEqual("neutral", MODULE.classify_check_run("COMPLETED", "SKIPPED"))
        self.assertEqual("failed", MODULE.classify_check_run("COMPLETED", "FAILURE"))
        self.assertEqual("failed", MODULE.classify_check_run("COMPLETED", "TIMED_OUT"))
        self.assertEqual("failed", MODULE.classify_check_run("COMPLETED", "CANCELLED"))
        self.assertEqual("stale", MODULE.classify_check_run("COMPLETED", "STALE"))

    def test_treats_action_required_as_blocked_on_an_approval(self):
        self.assertEqual(
            "approval_blocked", MODULE.classify_check_run("COMPLETED", "ACTION_REQUIRED")
        )
        self.assertEqual("approval_blocked", MODULE.classify_check_run("WAITING", ""))

    def test_maps_incomplete_check_run_statuses(self):
        self.assertEqual("not_started", MODULE.classify_check_run("QUEUED", ""))
        self.assertEqual("running", MODULE.classify_check_run("IN_PROGRESS", ""))

    def test_maps_status_contexts(self):
        self.assertEqual("passed", MODULE.classify_status_context("SUCCESS"))
        self.assertEqual("running", MODULE.classify_status_context("PENDING"))
        self.assertEqual("not_started", MODULE.classify_status_context("EXPECTED"))
        self.assertEqual("failed", MODULE.classify_status_context("ERROR"))

    def test_an_unrecognized_state_is_unknown_rather_than_passing(self):
        self.assertEqual("unknown", MODULE.classify_check_run("COMPLETED", "WAT"))
        self.assertEqual("unknown", MODULE.classify_check_run("WAT", ""))
        self.assertEqual("unknown", MODULE.classify_status_context("WAT"))


class NormalizeRollupTest(unittest.TestCase):
    def test_an_absent_rollup_is_an_empty_list(self):
        self.assertEqual([], MODULE.normalize_rollup(None))

    def test_normalizes_a_check_run(self):
        checks = MODULE.normalize_rollup(
            [
                {
                    "__typename": "CheckRun",
                    "name": "build",
                    "workflowName": "CI",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                    "detailsUrl": "https://github.com/o/r/actions/runs/1/job/2",
                }
            ]
        )
        self.assertEqual(1, len(checks))
        self.assertEqual("check:CI/build", checks[0]["key"])
        self.assertEqual("failed", checks[0]["class"])
        self.assertEqual("check_run", checks[0]["kind"])

    def test_normalizes_a_status_context(self):
        checks = MODULE.normalize_rollup(
            [
                {
                    "__typename": "StatusContext",
                    "context": "ci/external",
                    "state": "FAILURE",
                    "targetUrl": "https://ci.example.com/1",
                }
            ]
        )
        self.assertEqual("status:ci/external", checks[0]["key"])
        self.assertEqual("failed", checks[0]["class"])
        self.assertEqual("status", checks[0]["kind"])

    def test_a_check_run_without_a_workflow_keeps_a_bare_key(self):
        checks = MODULE.normalize_rollup(
            [{"__typename": "CheckRun", "name": "build", "status": "IN_PROGRESS"}]
        )
        self.assertEqual("check:build", checks[0]["key"])

    def test_duplicate_keys_are_suffixed_rather_than_dropped(self):
        checks = MODULE.normalize_rollup(
            [
                {"__typename": "CheckRun", "name": "test", "status": "COMPLETED",
                 "conclusion": "SUCCESS"},
                {"__typename": "CheckRun", "name": "test", "status": "COMPLETED",
                 "conclusion": "FAILURE"},
            ]
        )
        self.assertEqual(["check:test", "check:test#2"], [c["key"] for c in checks])
        self.assertEqual(["passed", "failed"], [c["class"] for c in checks])

    def test_infers_the_entry_type_when_typename_is_absent(self):
        checks = MODULE.normalize_rollup(
            [
                {"context": "legacy", "state": "SUCCESS"},
                {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ]
        )
        self.assertEqual(["status:legacy", "check:build"], [c["key"] for c in checks])

    def test_rejects_an_entry_with_no_recognizable_shape(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.normalize_rollup([{"nothing": True}])

    def test_rejects_a_rollup_that_is_not_a_list(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.normalize_rollup({"nodes": []})

    def test_rejects_a_named_check_with_an_empty_name(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.normalize_rollup([{"__typename": "CheckRun", "name": "  "}])


class CheckTrackingTest(unittest.TestCase):
    def test_stamps_the_first_sighting_of_each_check(self):
        tracking = MODULE.update_check_tracking(
            None, [check("check:a", klass="not_started")], NOW
        )
        self.assertEqual(stamp(), tracking["check:a"]["first_seen_at"])
        self.assertEqual(stamp(), tracking["check:a"]["not_started_since"])

    def test_keeps_the_not_started_clock_while_a_check_stays_queued(self):
        earlier = {
            "check:a": {
                "first_seen_at": stamp(30),
                "last_class": "not_started",
                "last_seen_at": stamp(30),
                "not_started_since": stamp(30),
            }
        }
        tracking = MODULE.update_check_tracking(
            earlier, [check("check:a", klass="not_started")], NOW
        )
        self.assertEqual(stamp(30), tracking["check:a"]["not_started_since"])
        self.assertEqual(1800.0, MODULE.not_started_seconds(tracking, "check:a", NOW))

    def test_a_requeued_check_gets_a_fresh_clock(self):
        earlier = {
            "check:a": {
                "first_seen_at": stamp(30),
                "last_class": "running",
                "last_seen_at": stamp(5),
                "not_started_since": stamp(30),
            }
        }
        tracking = MODULE.update_check_tracking(
            earlier, [check("check:a", klass="not_started")], NOW
        )
        self.assertEqual(stamp(), tracking["check:a"]["not_started_since"])
        self.assertEqual(0.0, MODULE.not_started_seconds(tracking, "check:a", NOW))

    def test_a_check_that_left_the_queue_carries_no_clock(self):
        tracking = MODULE.update_check_tracking(
            None, [check("check:a", klass="running")], NOW
        )
        self.assertNotIn("not_started_since", tracking["check:a"])
        self.assertEqual(0.0, MODULE.not_started_seconds(tracking, "check:a", NOW))

    def test_forgets_a_check_that_left_the_rollup(self):
        earlier = {"check:gone": {"first_seen_at": stamp(30)}}
        tracking = MODULE.update_check_tracking(earlier, [check("check:a")], NOW)
        self.assertEqual(["check:a"], list(tracking))


class DecideTest(unittest.TestCase):
    def decide(self, checks, **overrides):
        arguments = {"now": NOW, "tracking": {}, "deadline_expired": False}
        arguments.update(overrides)
        return MODULE.decide(checks, **arguments)

    def test_all_passing_checks_are_green(self):
        decision = self.decide(
            [check("check:a", klass="passed"), check("check:b", klass="neutral")]
        )
        self.assertEqual("green", decision["decision"])
        self.assertEqual("all_checks_passed", decision["reason"])

    def test_a_failure_reports_failures(self):
        decision = self.decide(
            [check("check:a", klass="failed"), check("check:b", klass="passed")]
        )
        self.assertEqual("failures", decision["decision"])
        self.assertEqual(["check:a"], decision["checks"])

    def test_an_empty_rollup_is_never_green(self):
        decision = self.decide([])
        self.assertEqual("no_checks", decision["decision"])
        self.assertEqual("no_applicable_checks", decision["reason"])

    def test_running_checks_wait(self):
        decision = self.decide([check("check:a", klass="running")])
        self.assertEqual("waiting", decision["decision"])

    def test_a_running_check_remains_nonterminal_when_the_poll_slice_ends(self):
        decision = self.decide(
            [check("check:a", klass="running")], deadline_expired=True
        )
        self.assertEqual("waiting", decision["decision"])
        self.assertEqual("still_running", decision["reason"])

    def test_an_approval_blocked_check_escalates_before_anything_else(self):
        decision = self.decide(
            [
                check("check:a", klass="approval_blocked"),
                check("check:b", klass="running"),
                check("check:c", klass="failed"),
                check("check:d", klass="unknown"),
            ]
        )
        self.assertEqual("escalate", decision["decision"])
        self.assertEqual("approval_required", decision["reason"])

    def test_an_empty_rollup_with_blocked_runs_escalates_for_approval(self):
        decision = self.decide(
            [], approval_runs=[{"id": 1, "name": "CI"}]
        )
        self.assertEqual("escalate", decision["decision"])
        self.assertEqual("approval_required", decision["reason"])
        self.assertIn("CI", decision["detail"])

    def test_an_unknown_state_escalates_rather_than_waiting(self):
        decision = self.decide(
            [check("check:a", klass="unknown"), check("check:b", klass="running")]
        )
        self.assertEqual("escalate", decision["decision"])
        self.assertEqual("unknown_check_state", decision["reason"])

    def test_a_stale_check_escalates_rather_than_waiting(self):
        decision = self.decide(
            [check("check:a", klass="stale"), check("check:b", klass="running")]
        )
        self.assertEqual("escalate", decision["decision"])
        self.assertEqual("stale_checks", decision["reason"])

    def test_a_queued_check_waits_inside_the_grace_period(self):
        tracking = {"check:a": {"not_started_since": stamp(5)}}
        decision = self.decide(
            [check("check:a", klass="not_started")], tracking=tracking
        )
        self.assertEqual("waiting", decision["decision"])

    def test_a_check_that_never_starts_escalates(self):
        tracking = {"check:a": {"not_started_since": stamp(30)}}
        decision = self.decide(
            [check("check:a", klass="not_started")], tracking=tracking
        )
        self.assertEqual("escalate", decision["decision"])
        self.assertEqual("checks_never_started", decision["reason"])
        self.assertEqual(["check:a"], decision["checks"])

    def test_a_never_started_check_escalates_even_beside_a_failure(self):
        tracking = {"check:a": {"not_started_since": stamp(30)}}
        decision = self.decide(
            [check("check:a", klass="not_started"), check("check:b", klass="failed")],
            tracking=tracking,
        )
        self.assertEqual("checks_never_started", decision["reason"])

    def test_a_running_check_defers_the_failure_decision(self):
        decision = self.decide(
            [check("check:a", klass="running"), check("check:b", klass="failed")]
        )
        self.assertEqual("waiting", decision["decision"])

    def test_the_grace_period_is_configurable(self):
        tracking = {"check:a": {"not_started_since": stamp(5)}}
        decision = self.decide(
            [check("check:a", klass="not_started")],
            tracking=tracking,
            not_started_grace=60,
        )
        self.assertEqual("checks_never_started", decision["reason"])


class BaselineAttributionTest(unittest.TestCase):
    def test_a_base_failure_reads_as_pre_existing(self):
        for conclusion in MODULE.FAILED_BASELINE_CONCLUSIONS:
            self.assertEqual("pre_existing", MODULE.baseline_verdict(conclusion))

    def test_a_base_success_reads_as_caused_by_the_pull_request(self):
        self.assertEqual("pr_caused", MODULE.baseline_verdict("SUCCESS"))

    def test_anything_else_reads_as_unknown(self):
        self.assertEqual("unknown", MODULE.baseline_verdict("QUEUED"))
        self.assertEqual("unknown", MODULE.baseline_verdict(None))
        self.assertEqual("unknown", MODULE.baseline_verdict(""))

    def test_a_base_failure_leaves_only_the_pre_existing_verdict_open(self):
        self.assertEqual(("pre_existing",), MODULE.allowed_verdicts("pre_existing"))

    def test_a_base_success_rules_out_calling_the_failure_pre_existing(self):
        self.assertEqual(("pr_caused", "flake"), MODULE.allowed_verdicts("pr_caused"))

    def test_no_base_evidence_leaves_every_verdict_open(self):
        self.assertEqual(MODULE.VERDICTS, MODULE.allowed_verdicts("unknown"))

    def test_attributes_only_the_failing_checks(self):
        attributions = MODULE.attribute_failures(
            [check("check:a", klass="failed"), check("check:b", klass="passed")],
            {"a": "FAILURE"},
        )
        self.assertEqual(["check:a"], list(attributions))
        self.assertEqual("pre_existing", attributions["check:a"]["verdict"])
        self.assertEqual("baseline", attributions["check:a"]["source"])

    def test_a_failure_the_base_never_ran_is_left_unattributed(self):
        attributions = MODULE.attribute_failures([check("check:a")], {})
        self.assertEqual("unknown", attributions["check:a"]["verdict"])
        self.assertEqual("unattributed", attributions["check:a"]["source"])

    def test_keeps_a_model_verdict_the_base_evidence_still_allows(self):
        previous = {
            "check:a": {
                "verdict": "flake",
                "source": "model",
                "rationale": "the runner vanished",
            }
        }
        attributions = MODULE.attribute_failures(
            [check("check:a")], {"a": "SUCCESS"}, previous
        )
        self.assertEqual("flake", attributions["check:a"]["verdict"])
        self.assertEqual("model", attributions["check:a"]["source"])
        self.assertEqual("the runner vanished", attributions["check:a"]["rationale"])

    def test_drops_a_model_verdict_the_base_evidence_now_contradicts(self):
        previous = {
            "check:a": {"verdict": "pr_caused", "source": "model", "rationale": "guess"}
        }
        attributions = MODULE.attribute_failures(
            [check("check:a")], {"a": "FAILURE"}, previous
        )
        self.assertEqual("pre_existing", attributions["check:a"]["verdict"])
        self.assertEqual("baseline", attributions["check:a"]["source"])

    def test_ignores_a_stored_baseline_verdict_that_was_never_a_model_choice(self):
        previous = {"check:a": {"verdict": "pr_caused", "source": "baseline"}}
        attributions = MODULE.attribute_failures(
            [check("check:a")], {"a": "FAILURE"}, previous
        )
        self.assertEqual("pre_existing", attributions["check:a"]["verdict"])


class NextActionTest(unittest.TestCase):
    def action(self, checks, attributions, **overrides):
        state = {
            "reruns": overrides.get("reruns", {}),
            "run": {"attributions": attributions, "batches": overrides.get("batches", [])},
        }
        decision = {
            "decision": "failures",
            "reason": "checks_failed",
            "checks": checks,
            "detail": "",
        }
        return MODULE.next_action(state, decision)

    def test_passes_a_non_failure_decision_straight_through(self):
        decision = {
            "decision": "green",
            "reason": "all_checks_passed",
            "checks": [],
            "detail": "fine",
        }
        action = MODULE.next_action({"run": {}}, decision)
        self.assertEqual("green", action["action"])
        self.assertEqual("fine", action["detail"])

    def test_asks_for_a_verdict_before_touching_anything(self):
        action = self.action(
            ["check:a"], {"check:a": attribution("check:a", "unknown")}
        )
        self.assertEqual("attribute", action["action"])
        self.assertEqual(["check:a"], action["checks"])

    def test_reruns_a_flake_that_has_not_been_rerun(self):
        action = self.action(
            ["check:a"], {"check:a": attribution("check:a", "flake")}
        )
        self.assertEqual("rerun", action["action"])
        self.assertEqual("suspected_flake", action["reason"])

    def test_escalates_a_flake_that_failed_after_its_one_rerun(self):
        action = self.action(
            ["check:a"],
            {"check:a": attribution("check:a", "flake")},
            reruns={"check:a": {"count": 1}},
        )
        self.assertEqual("escalate", action["action"])
        self.assertEqual("flake_failed_twice", action["reason"])

    def test_fixes_a_failure_the_pull_request_caused(self):
        action = self.action(
            ["check:a"], {"check:a": attribution("check:a", "pr_caused")}
        )
        self.assertEqual("fix", action["action"])

    def test_never_fixes_a_failure_the_base_branch_already_has(self):
        action = self.action(
            ["check:a"], {"check:a": attribution("check:a", "pre_existing")}
        )
        self.assertEqual("escalate", action["action"])
        self.assertEqual("pre_existing_failures", action["reason"])

    def test_a_fixable_failure_comes_before_a_pre_existing_escalation(self):
        action = self.action(
            ["check:a", "check:b"],
            {
                "check:a": attribution("check:a", "pre_existing"),
                "check:b": attribution("check:b", "pr_caused"),
            },
        )
        self.assertEqual("fix", action["action"])
        self.assertEqual(["check:b"], action["checks"])

    def test_escalates_a_failure_that_survived_its_recorded_fix(self):
        action = self.action(
            ["check:a"],
            {"check:a": attribution("check:a", "pr_caused")},
            batches=[{"id": "b1", "status": "recorded", "check_keys": ["check:a"]}],
        )
        self.assertEqual("escalate", action["action"])
        self.assertEqual("unfixable_failure", action["reason"])

    def test_attribution_comes_before_every_other_action(self):
        action = self.action(
            ["check:a", "check:b"],
            {
                "check:a": attribution("check:a", "unknown"),
                "check:b": attribution("check:b", "pr_caused"),
            },
        )
        self.assertEqual("attribute", action["action"])


class RunReferenceTest(unittest.TestCase):
    def test_reads_a_run_and_job_from_an_actions_url(self):
        reference = MODULE.parse_run_reference(
            "https://github.com/o/r/actions/runs/1234/job/5678"
        )
        self.assertEqual({"run_id": 1234, "job_id": 5678}, reference)

    def test_reads_a_run_from_a_url_without_a_job(self):
        reference = MODULE.parse_run_reference(
            "https://github.com/o/r/actions/runs/1234"
        )
        self.assertEqual({"run_id": 1234}, reference)

    def test_reads_a_legacy_job_url(self):
        reference = MODULE.parse_run_reference("https://github.com/o/r/runs/99")
        self.assertEqual({"job_id": 99}, reference)

    def test_an_external_url_has_no_run(self):
        self.assertIsNone(MODULE.parse_run_reference("https://ci.example.com/build/1"))
        self.assertIsNone(MODULE.parse_run_reference(None))
        self.assertIsNone(MODULE.parse_run_reference(""))

    def test_resolves_a_run_from_a_job_identifier(self):
        pr = {"upstream_owner": "o", "upstream_repo": "r"}
        with mock.patch.object(MODULE, "gh_json", return_value={"run_id": 7}) as api:
            self.assertEqual(7, MODULE.resolve_run_id(pr, {"job_id": 99}))
        self.assertIn("actions/jobs/99", api.call_args[0][0][1])

    def test_a_run_identifier_needs_no_lookup(self):
        with mock.patch.object(MODULE, "gh_json") as api:
            self.assertEqual(3, MODULE.resolve_run_id({}, {"run_id": 3, "job_id": 9}))
        api.assert_not_called()


class ApprovalRunTest(unittest.TestCase):
    def test_finds_runs_waiting_on_an_approval(self):
        blocked = MODULE.approval_blocked_runs(
            {
                "workflow_runs": [
                    {"id": 1, "name": "CI", "status": "waiting"},
                    {"id": 2, "name": "Lint", "status": "completed",
                     "conclusion": "action_required"},
                    {"id": 3, "name": "Done", "status": "completed",
                     "conclusion": "success"},
                ]
            }
        )
        self.assertEqual([1, 2], [entry["id"] for entry in blocked])

    def test_an_unexpected_payload_finds_nothing(self):
        self.assertEqual([], MODULE.approval_blocked_runs(None))
        self.assertEqual([], MODULE.approval_blocked_runs({"workflow_runs": None}))


class StateFileTest(unittest.TestCase):
    def test_round_trips_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_state(Path(directory))
            state = MODULE.load_state(path)
            state["marker"] = True
            MODULE.save_state(path, state)
            self.assertTrue(MODULE.load_state(path)["marker"])
            self.assertIn("updated_at", MODULE.load_state(path))

    def test_rejects_an_unsupported_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"version": 99}), encoding="utf-8")
            with self.assertRaises(MODULE.WorkflowError):
                MODULE.load_state(path)

    def test_rejects_a_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(MODULE.WorkflowError):
                MODULE.load_state(Path(directory) / "nope.json")

    def test_refuses_to_work_on_a_published_iteration(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.active_run({"run": {"status": "published"}})

    def test_refuses_to_work_without_an_iteration(self):
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.active_run({})


class ArchiveRunTest(unittest.TestCase):
    def test_archives_settled_batches_and_verdicts(self):
        state = {
            "history": [],
            "run": {
                "iteration": 1,
                "head_sha": "head1",
                "batches": [
                    {"id": "b1", "status": "recorded", "label": "fix",
                     "check_keys": ["check:a"], "check_names": ["a"],
                     "commit": "c1", "summary": "done"},
                    {"id": "b2", "status": "planned", "check_keys": ["check:b"]},
                ],
                "attributions": {
                    "check:c": attribution("check:c", "pre_existing"),
                    "check:d": attribution("check:d", "unknown"),
                },
            },
        }
        MODULE.archive_run(state)
        identifiers = [entry["id"] for entry in state["history"]]
        self.assertIn("1:b1", identifiers)
        self.assertNotIn("1:b2", identifiers)
        self.assertIn("1:verdict:check:c", identifiers)
        self.assertNotIn("1:verdict:check:d", identifiers)
        self.assertEqual(
            "addressed",
            next(e for e in state["history"] if e["id"] == "1:b1")["outcome"],
        )

    def test_archiving_twice_records_nothing_twice(self):
        state = {
            "history": [],
            "run": {
                "iteration": 1,
                "head_sha": "head1",
                "batches": [
                    {"id": "b1", "status": "recorded", "check_keys": [], "commit": None,
                     "rationale": "no code change"}
                ],
                "attributions": {},
            },
        }
        MODULE.archive_run(state)
        MODULE.archive_run(state)
        self.assertEqual(1, len(state["history"]))
        self.assertEqual("recorded", state["history"][0]["outcome"])


class SummaryHelperTest(unittest.TestCase):
    def test_counts_checks_by_class(self):
        counts = MODULE.class_counts(
            [check("check:a", klass="failed"), check("check:b", klass="passed")]
        )
        self.assertEqual(1, counts["failed"])
        self.assertEqual(1, counts["passed"])
        self.assertEqual(0, counts["unknown"])

    def test_counts_batches_by_status(self):
        self.assertEqual(
            {"planned": 1, "recorded": 2},
            MODULE.count_by_status(
                [{"status": "planned"}, {"status": "recorded"}, {"status": "recorded"}]
            ),
        )

    def test_describes_checks_by_their_human_name(self):
        checks = [check("check:CI/build", name="build")]
        self.assertEqual("build", MODULE.describe_checks(checks, ["check:CI/build"]))
        self.assertEqual("", MODULE.describe_checks(checks, ["check:missing"]))

    def test_counts_recorded_batches_as_handled(self):
        state = {
            "run": {
                "batches": [
                    {"status": "recorded", "check_keys": ["check:a"]},
                    {"status": "planned", "check_keys": ["check:b"]},
                ]
            }
        }
        self.assertEqual({"check:a"}, MODULE.handled_checks(state))

    def test_counts_reruns_per_check(self):
        self.assertEqual(0, MODULE.rerun_count({}, "check:a"))
        self.assertEqual(
            2, MODULE.rerun_count({"reruns": {"check:a": {"count": 2}}}, "check:a")
        )


class AttributeCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def state_with(self, baseline, conclusion=None):
        return write_state(
            self.root,
            run={
                "checks": [check("check:a", name="build")],
                "attributions": {
                    "check:a": attribution(
                        "check:a",
                        baseline,
                        baseline=baseline,
                        conclusion=conclusion,
                    )
                },
            },
        )

    def test_records_a_model_verdict_with_its_rationale(self):
        path = self.state_with("unknown")
        payload = call(
            "attribute",
            "--state",
            str(path),
            "--check",
            "check:a",
            "--verdict",
            "pr_caused",
            "--rationale",
            "the error names app.py, which this PR changed",
        )
        self.assertEqual("attributed", payload["result"])
        entry = MODULE.load_state(path)["run"]["attributions"]["check:a"]
        self.assertEqual("pr_caused", entry["verdict"])
        self.assertEqual("model", entry["source"])
        self.assertIn("app.py", entry["rationale"])

    def test_refuses_to_blame_the_pull_request_for_a_base_failure(self):
        path = self.state_with("pre_existing", "FAILURE")
        with self.assertRaises(MODULE.WorkflowError) as error:
            call(
                "attribute",
                "--state",
                str(path),
                "--check",
                "check:a",
                "--verdict",
                "pr_caused",
                "--rationale",
                "looks related",
            )
        self.assertIn("does not allow the verdict", str(error.exception))
        self.assertEqual(
            "pre_existing",
            MODULE.load_state(path)["run"]["attributions"]["check:a"]["verdict"],
        )

    def test_refuses_to_call_a_check_pre_existing_when_the_base_passed(self):
        path = self.state_with("pr_caused", "SUCCESS")
        with self.assertRaises(MODULE.WorkflowError):
            call(
                "attribute",
                "--state",
                str(path),
                "--check",
                "check:a",
                "--verdict",
                "pre_existing",
                "--rationale",
                "not my fault",
            )

    def test_allows_calling_a_check_a_flake_when_the_base_passed(self):
        path = self.state_with("pr_caused", "SUCCESS")
        payload = call(
            "attribute",
            "--state",
            str(path),
            "--check",
            "check:a",
            "--verdict",
            "flake",
            "--rationale",
            "the runner lost the network",
        )
        self.assertEqual("flake", payload["verdict"])

    def test_rejects_an_unknown_check(self):
        path = self.state_with("unknown")
        with self.assertRaises(MODULE.WorkflowError):
            call(
                "attribute",
                "--state",
                str(path),
                "--check",
                "check:missing",
                "--verdict",
                "flake",
                "--rationale",
                "x",
            )

    def test_reads_a_rationale_from_a_file(self):
        path = self.state_with("unknown")
        rationale = self.root / "rationale.txt"
        rationale.write_text("multi\nline (with parens)\n", encoding="utf-8")
        payload = call(
            "attribute",
            "--state",
            str(path),
            "--check",
            "check:a",
            "--verdict",
            "pre_existing",
            "--rationale-file",
            str(rationale),
        )
        self.assertIn("(with parens)", payload["rationale"])

    def test_rejects_an_empty_rationale(self):
        path = self.state_with("unknown")
        with self.assertRaises(MODULE.WorkflowError):
            call(
                "attribute",
                "--state",
                str(path),
                "--check",
                "check:a",
                "--verdict",
                "flake",
                "--rationale",
                "   ",
            )


class RerunCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patcher = mock.patch.object(MODULE, "require_tools")
        patcher.start()
        self.addCleanup(patcher.stop)

    def state_with(self, verdict="flake", url=None, reruns=None):
        return write_state(
            self.root,
            reruns=reruns or {},
            run={
                "checks": [
                    check(
                        "check:a",
                        name="build",
                        url=url or "https://github.com/o/r/actions/runs/5/job/6",
                    )
                ],
                "attributions": {"check:a": attribution("check:a", verdict)},
            },
        )

    def test_requests_one_rerun_and_records_it(self):
        path = self.state_with()
        with mock.patch.object(MODULE, "rerun_failed_jobs") as request:
            payload = call("rerun", "--state", str(path), "--check", "check:a")
        request.assert_called_once()
        self.assertEqual("rerun_requested", payload["result"])
        self.assertEqual(5, payload["run_id"])
        self.assertEqual(1, payload["reruns"])
        self.assertEqual(1, MODULE.load_state(path)["reruns"]["check:a"]["count"])

    def test_refuses_a_second_rerun_of_the_same_check(self):
        path = self.state_with(reruns={"check:a": {"count": 1}})
        with mock.patch.object(MODULE, "rerun_failed_jobs") as request:
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("rerun", "--state", str(path), "--check", "check:a")
        request.assert_not_called()
        self.assertIn("flake_failed_twice", str(error.exception))

    def test_refuses_to_rerun_a_check_that_is_not_a_flake(self):
        path = self.state_with(verdict="pr_caused")
        with mock.patch.object(MODULE, "rerun_failed_jobs") as request:
            with self.assertRaises(MODULE.WorkflowError):
                call("rerun", "--state", str(path), "--check", "check:a")
        request.assert_not_called()

    def test_escalates_a_check_with_no_actions_run_behind_it(self):
        path = self.state_with(url="https://ci.example.com/build/1")
        with mock.patch.object(MODULE, "rerun_failed_jobs") as request:
            payload = call("rerun", "--state", str(path), "--check", "check:a")
        request.assert_not_called()
        self.assertEqual("no_rerun_support", payload["result"])
        escalation = MODULE.load_state(path)["escalation"]
        self.assertEqual("no_rerun_support", escalation["reason"])
        self.assertTrue(escalation["next_action"])

    def test_rejects_a_check_outside_this_iteration(self):
        path = write_state(
            self.root,
            run={"attributions": {"check:a": attribution("check:a", "flake")}},
        )
        with self.assertRaises(MODULE.WorkflowError):
            call("rerun", "--state", str(path), "--check", "check:a")

    def test_stamps_the_watermark_before_it_asks_github_to_run_again(self):
        path = self.state_with()
        observed = {}

        def request(pr, run_id):
            observed["reruns"] = dict(MODULE.load_state(path).get("reruns") or {})
            observed["at"] = MODULE.utc_now()

        with mock.patch.object(MODULE, "rerun_failed_jobs", request):
            call("rerun", "--state", str(path), "--check", "check:a")

        # The stored watermark must predate the request, so a run that starts
        # and finishes immediately still counts as newer than the request.
        self.assertEqual({}, observed["reruns"])
        requested_at = MODULE.load_state(path)["reruns"]["check:a"]["requested_at"]
        self.assertLessEqual(
            MODULE.parse_timestamp(requested_at), MODULE.parse_timestamp(observed["at"])
        )

    def test_records_the_head_the_rerun_belongs_to(self):
        path = self.state_with()
        with mock.patch.object(MODULE, "rerun_failed_jobs"):
            call("rerun", "--state", str(path), "--check", "check:a")
        self.assertEqual("head1", MODULE.load_state(path)["reruns"]["check:a"]["head_sha"])


class RerunWatermarkTest(unittest.TestCase):
    def entry(self, minutes_ago=5, head_sha="head1"):
        return {
            "count": 1,
            "name": "build",
            "run_id": 5,
            "head_sha": head_sha,
            "requested_at": stamp(minutes_ago),
        }

    def test_holds_back_a_failure_recorded_before_the_rerun_was_asked_for(self):
        stale = check("check:a", completed_at=stamp(10))
        applied = MODULE.apply_rerun_watermark(
            [stale], {"check:a": self.entry()}, "head1"
        )
        self.assertEqual("running", applied[0]["class"])
        self.assertTrue(applied[0]["awaiting_rerun"])

    def test_credits_a_failure_that_landed_after_the_rerun_was_asked_for(self):
        fresh = check("check:a", completed_at=stamp(1))
        applied = MODULE.apply_rerun_watermark(
            [fresh], {"check:a": self.entry()}, "head1"
        )
        self.assertEqual("failed", applied[0]["class"])
        self.assertNotIn("awaiting_rerun", applied[0])

    def test_waits_when_a_failure_carries_no_completion_time(self):
        applied = MODULE.apply_rerun_watermark(
            [check("check:a")], {"check:a": self.entry()}, "head1"
        )
        self.assertEqual("running", applied[0]["class"])

    def test_ignores_a_rerun_recorded_for_a_different_head(self):
        stale = check("check:a", completed_at=stamp(10))
        applied = MODULE.apply_rerun_watermark(
            [stale], {"check:a": self.entry(head_sha="head9")}, "head1"
        )
        self.assertEqual("failed", applied[0]["class"])

    def test_leaves_every_other_check_alone(self):
        checks = [
            check("check:a", klass="passed", completed_at=stamp(10)),
            check("check:b", completed_at=stamp(10)),
        ]
        applied = MODULE.apply_rerun_watermark(
            checks, {"check:a": self.entry()}, "head1"
        )
        self.assertEqual(["passed", "failed"], [item["class"] for item in applied])

    def test_does_nothing_without_a_recorded_rerun(self):
        checks = [check("check:a", completed_at=stamp(10))]
        for reruns in (None, {}, "nonsense"):
            with self.subTest(reruns=reruns):
                self.assertEqual(
                    ["failed"],
                    [
                        item["class"]
                        for item in MODULE.apply_rerun_watermark(
                            checks, reruns, "head1"
                        )
                    ],
                )

    def test_never_reports_a_flake_as_failing_twice_on_the_old_result(self):
        state = {
            "reruns": {"check:a": {"count": 1}},
            "run": {
                "attributions": {"check:a": attribution("check:a", "flake")},
            },
        }
        stale = check("check:a", completed_at=stamp(10))
        applied = MODULE.apply_rerun_watermark(
            [stale], {"check:a": self.entry()}, "head1"
        )
        decision = MODULE.decide(
            applied,
            now=NOW,
            tracking={},
            not_started_grace=MODULE.DEFAULT_NOT_STARTED_GRACE,
            deadline_expired=False,
            approval_runs=[],
        )
        self.assertEqual("waiting", decision["decision"])
        self.assertEqual(
            "waiting", MODULE.next_action(state, decision)["action"]
        )


class PlanCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def state_with(self, verdict):
        return write_state(
            self.root,
            run={
                "checks": [check("check:a", name="build")],
                "attributions": {"check:a": attribution("check:a", verdict)},
            },
        )

    def test_stores_a_batch_for_a_failure_the_pull_request_caused(self):
        path = self.state_with("pr_caused")
        payload = call(
            "plan",
            "--state",
            str(path),
            "--batch",
            "b1",
            "--checks",
            "check:a",
            "--label",
            "fix the import",
            "--paths",
            "app.py",
            "--validation",
            "python -m pytest",
        )
        self.assertEqual("planned", payload["result"])
        self.assertEqual(["a"], payload["batch"]["check_names"])
        self.assertEqual("planned", payload["batch"]["status"])

    def test_refuses_a_pre_existing_failure(self):
        path = self.state_with("pre_existing")
        with self.assertRaises(MODULE.WorkflowError) as error:
            call(
                "plan",
                "--state",
                str(path),
                "--batch",
                "b1",
                "--checks",
                "check:a",
                "--label",
                "fix",
            )
        self.assertIn("pr_caused", str(error.exception))
        self.assertEqual([], MODULE.load_state(path)["run"]["batches"])

    def test_refuses_a_flake(self):
        path = self.state_with("flake")
        with self.assertRaises(MODULE.WorkflowError):
            call(
                "plan", "--state", str(path), "--batch", "b1", "--checks", "check:a",
                "--label", "fix",
            )

    def test_refuses_an_unattributed_failure(self):
        path = self.state_with("unknown")
        with self.assertRaises(MODULE.WorkflowError):
            call(
                "plan", "--state", str(path), "--batch", "b1", "--checks", "check:a",
                "--label", "fix",
            )

    def test_refuses_a_check_outside_this_iteration(self):
        path = self.state_with("pr_caused")
        with self.assertRaises(MODULE.WorkflowError):
            call(
                "plan", "--state", str(path), "--batch", "b1", "--checks", "check:z",
                "--label", "fix",
            )

    def test_replanning_a_batch_replaces_it(self):
        path = self.state_with("pr_caused")
        for label in ("first", "second"):
            call(
                "plan", "--state", str(path), "--batch", "b1", "--checks", "check:a",
                "--label", label,
            )
        batches = MODULE.load_state(path)["run"]["batches"]
        self.assertEqual(1, len(batches))
        self.assertEqual("second", batches[0]["label"])


class RecordAndSkipCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = write_state(
            self.root,
            run={
                "checks": [check("check:a", name="build")],
                "attributions": {"check:a": attribution("check:a", "pr_caused")},
                "batches": [
                    {
                        "id": "b1",
                        "label": "fix",
                        "check_keys": ["check:a"],
                        "check_names": ["build"],
                        "paths": ["app.py"],
                        "validation": None,
                        "status": "planned",
                        "commit": None,
                        "summary": None,
                        "rationale": None,
                    }
                ],
            },
        )

    def test_records_a_commit(self):
        with mock.patch.object(MODULE, "git", return_value="abc123"):
            payload = call(
                "record", "--state", str(self.path), "--batch", "b1",
                "--summary", "fixed the import", "--commit", "HEAD",
            )
        self.assertEqual("abc123", payload["commit"])
        batch = MODULE.load_state(self.path)["run"]["batches"][0]
        self.assertEqual("recorded", batch["status"])

    def test_records_a_no_code_outcome(self):
        payload = call(
            "record", "--state", str(self.path), "--batch", "b1",
            "--summary", "nothing to change", "--rationale", "the fix landed already",
        )
        self.assertIsNone(payload["commit"])
        self.assertEqual("the fix landed already", payload["rationale"])

    def test_requires_a_commit_or_a_rationale(self):
        with self.assertRaises(MODULE.WorkflowError):
            call("record", "--state", str(self.path), "--batch", "b1",
                 "--summary", "nothing")

    def test_rejects_an_unplanned_batch(self):
        with self.assertRaises(MODULE.WorkflowError):
            call("record", "--state", str(self.path), "--batch", "nope",
                 "--summary", "x", "--rationale", "y")

    def test_skipping_a_batch_records_an_escalation(self):
        payload = call(
            "skip", "--state", str(self.path), "--batch", "b1",
            "--rationale", "the failure needs a dependency this loop cannot add",
        )
        self.assertEqual("skipped", payload["result"])
        escalation = MODULE.load_state(self.path)["escalation"]
        self.assertEqual("unfixable_failure", escalation["reason"])
        self.assertEqual(["check:a"], escalation["checks"])

    def test_refuses_a_commit_that_deletes_a_test_file(self):
        """The refusal has to sit on the command, not only in the helper."""
        def fake_git(repo_root, *arguments):
            if arguments[0] == "rev-parse":
                return "abc123"
            if "--name-status" in arguments:
                return "D\ttests/test_widget.py"
            return ""

        with mock.patch.object(MODULE, "git", fake_git):
            with self.assertRaises(MODULE.WorkflowError) as error:
                call(
                    "record", "--state", str(self.path), "--batch", "b1",
                    "--summary", "made the build pass", "--commit", "HEAD",
                )
        self.assertIn("stopping a test from running", str(error.exception))
        batch = MODULE.load_state(self.path)["run"]["batches"][0]
        self.assertEqual("planned", batch["status"])

    def test_refuses_a_commit_that_disables_a_running_test(self):
        def fake_git(repo_root, *arguments):
            if arguments[0] == "rev-parse":
                return "abc123"
            if "--name-status" in arguments:
                return "M\ttests/test_widget.py"
            return "+++ b/tests/test_widget.py\n+@pytest.mark.skip(reason='ci')"

        with mock.patch.object(MODULE, "git", fake_git):
            with self.assertRaises(MODULE.WorkflowError) as error:
                call(
                    "record", "--state", str(self.path), "--batch", "b1",
                    "--summary", "made the build pass", "--commit", "HEAD",
                )
        self.assertIn("@pytest.mark.skip", str(error.exception))


class EscalateCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_records_the_reason_and_the_next_action(self):
        path = write_state(self.root)
        payload = call(
            "escalate", "--state", str(path), "--reason", "pre_existing_failures",
            "--checks", "check:a", "--detail", "build already fails on main",
        )
        self.assertEqual("escalated", payload["result"])
        self.assertEqual(
            MODULE.ESCALATION_ACTIONS["pre_existing_failures"], payload["next_action"]
        )
        self.assertEqual("head1", payload["head_sha"])
        self.assertEqual(
            "pre_existing_failures", MODULE.load_state(path)["escalation"]["reason"]
        )

    def test_rejects_a_reason_outside_the_catalog(self):
        path = write_state(self.root)
        with self.assertRaises(SystemExit):
            call("escalate", "--state", str(path), "--reason", "because",
                 "--detail", "x")

    def test_rejects_an_empty_detail(self):
        path = write_state(self.root)
        with self.assertRaises(MODULE.WorkflowError):
            call("escalate", "--state", str(path), "--reason", "timeout",
                 "--detail", "  ")


class ResolveCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patcher = mock.patch.object(MODULE, "require_tools")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_records_a_green_outcome_at_the_pinned_head(self):
        path = write_state(self.root)
        rollup = ("head1", [check("check:a", klass="passed")])
        with mock.patch.object(MODULE, "fetch_rollup", return_value=rollup):
            payload = call("resolve", "--state", str(path), "--outcome", "green")
        self.assertEqual("green", payload["outcome"])
        self.assertEqual("head1", payload["clean_at_head_sha"])
        self.assertIsNone(payload["skip_note"])
        state = MODULE.load_state(path)
        self.assertEqual("head1", state["clean_at_head_sha"])
        self.assertIsNone(state["escalation"])

    def test_records_a_no_checks_skip_with_a_visible_note(self):
        path = write_state(self.root)
        with mock.patch.object(MODULE, "fetch_rollup", return_value=("head1", [])):
            with mock.patch.object(MODULE, "fetch_workflow_runs", return_value={}):
                payload = call(
                    "resolve", "--state", str(path), "--outcome", "no_checks"
                )
        self.assertEqual("no_checks", payload["outcome"])
        self.assertIn("no applicable checks", payload["skip_note"])
        self.assertIn("owner/repo#7", payload["skip_note"])

    def test_refuses_an_outcome_the_live_checks_contradict(self):
        path = write_state(self.root)
        rollup = ("head1", [check("check:a", klass="failed")])
        with mock.patch.object(MODULE, "fetch_rollup", return_value=rollup):
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("resolve", "--state", str(path), "--outcome", "green")
        self.assertIn("'failures'", str(error.exception))

    def test_refuses_to_call_an_empty_rollup_green(self):
        path = write_state(self.root)
        with mock.patch.object(MODULE, "fetch_rollup", return_value=("head1", [])):
            with mock.patch.object(MODULE, "fetch_workflow_runs", return_value={}):
                with self.assertRaises(MODULE.WorkflowError):
                    call("resolve", "--state", str(path), "--outcome", "green")

    def test_refuses_when_the_head_moved(self):
        path = write_state(self.root)
        rollup = ("head2", [check("check:a", klass="passed")])
        with mock.patch.object(MODULE, "fetch_rollup", return_value=rollup):
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("resolve", "--state", str(path), "--outcome", "green")
        self.assertIn("head changed", str(error.exception))


class ChecksCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patcher = mock.patch.object(MODULE, "require_tools")
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, path, rollup, baseline=None, runs=None, *arguments):
        with mock.patch.object(MODULE, "fetch_rollup", return_value=rollup):
            with mock.patch.object(
                MODULE, "baseline_conclusions", return_value=baseline or {}
            ):
                with mock.patch.object(
                    MODULE, "fetch_workflow_runs", return_value=runs or {}
                ):
                    return call("checks", "--state", str(path), *arguments)

    def test_reports_green(self):
        path = write_state(self.root)
        with mock.patch.object(MODULE, "save_state", wraps=MODULE.save_state) as save:
            payload = self.read(path, ("head1", [check("check:a", klass="passed")]))
        self.assertEqual("green", payload["result"])
        self.assertEqual(1, payload["counts"]["passed"])
        self.assertTrue(Path(payload["checks_path"]).is_file())
        self.assertEqual(1, save.call_count)
        saved = save.call_args.args[1]
        self.assertEqual("green", saved["outcome"])
        self.assertEqual("head1", saved["clean_at_head_sha"])
        self.assertEqual("green", saved["run"]["outcome"])

    def test_reports_a_repository_with_no_checks(self):
        path = write_state(self.root)
        with mock.patch.object(MODULE, "save_state", wraps=MODULE.save_state) as save:
            payload = self.read(path, ("head1", []))
        self.assertEqual("no_checks", payload["result"])
        self.assertEqual("no_applicable_checks", payload["reason"])
        self.assertEqual(1, save.call_count)
        self.assertEqual("no_checks", save.call_args.args[1]["outcome"])
        state = MODULE.load_state(path)
        self.assertEqual("no_checks", state["outcome"])
        self.assertEqual("head1", state["clean_at_head_sha"])
        self.assertIn("no applicable checks", state["skip_note"])

    def test_asks_for_a_verdict_when_the_base_evidence_is_silent(self):
        path = write_state(self.root)
        payload = self.read(path, ("head1", [check("check:a", name="build")]))
        self.assertEqual("attribute", payload["result"])
        self.assertEqual(["check:a"], payload["action_checks"])
        self.assertEqual("build", payload["failing"][0]["name"])

    def test_escalates_without_editing_when_the_base_already_fails(self):
        path = write_state(self.root)
        payload = self.read(
            path, ("head1", [check("check:a", name="build")]), {"build": "FAILURE"}
        )
        self.assertEqual("escalate", payload["result"])
        self.assertEqual("pre_existing_failures", payload["reason"])
        self.assertTrue(payload["next_action"])
        self.assertEqual(
            "pre_existing_failures", MODULE.load_state(path)["escalation"]["reason"]
        )

    def test_asks_for_a_fix_when_the_base_passed(self):
        path = write_state(self.root)
        payload = self.read(
            path, ("head1", [check("check:a", name="build")]), {"build": "SUCCESS"}
        )
        self.assertEqual("fix", payload["result"])
        self.assertEqual("pr_caused", payload["failing"][0]["verdict"])

    def test_escalates_when_the_head_moved_under_the_iteration(self):
        path = write_state(self.root)
        payload = self.read(path, ("head9", [check("check:a", klass="passed")]))
        self.assertEqual("escalate", payload["result"])
        self.assertEqual("head_changed", payload["reason"])

    def test_reports_waiting_without_the_wait_flag(self):
        path = write_state(self.root)
        payload = self.read(path, ("head1", [check("check:a", klass="running")]))
        self.assertEqual("waiting", payload["result"])
        self.assertIsNone(MODULE.load_state(path)["escalation"])

    def test_wait_returns_still_running_after_the_default_five_minute_slice(self):
        self.assertEqual(300, MODULE.DEFAULT_POLL_TIMEOUT)
        path = write_state(self.root)
        rollup = ("head1", [check("check:a", klass="running")])
        with (
            mock.patch.object(MODULE, "fetch_rollup", return_value=rollup),
            mock.patch.object(MODULE, "baseline_conclusions", return_value={}),
            mock.patch.object(MODULE, "time") as clock,
        ):
            clock.monotonic.side_effect = [0.0, 0.0, 300.0]
            clock.sleep.return_value = None
            payload = call("checks", "--state", str(path), "--wait")
        self.assertEqual("waiting", payload["result"])
        self.assertEqual("still_running", payload["reason"])
        self.assertIsNone(MODULE.load_state(path)["escalation"])

    def test_escalates_an_approval_blocked_fork_run(self):
        path = write_state(self.root)
        payload = self.read(
            path,
            ("head1", []),
            None,
            {"workflow_runs": [{"id": 3, "name": "CI", "status": "waiting"}]},
        )
        self.assertEqual("escalate", payload["result"])
        self.assertEqual("approval_required", payload["reason"])

    def test_stores_the_snapshot_and_the_tracking_clock(self):
        path = write_state(self.root)
        self.read(path, ("head1", [check("check:a", klass="not_started")]))
        run_state = MODULE.load_state(path)["run"]
        self.assertEqual(1, len(run_state["checks"]))
        self.assertIn("not_started_since", run_state["tracking"]["check:a"])
        self.assertEqual("waiting", run_state["decision"]["decision"])

    def test_spends_an_iteration_only_on_a_run_with_work_to_do(self):
        for rollup, baseline, expected in (
            ([check("check:a", klass="passed")], None, 0),
            ([], None, 0),
            ([check("check:a", klass="running")], None, 0),
            ([check("check:a", name="build")], {"build": "FAILURE"}, 0),
            ([check("check:a", name="build")], None, 1),
            ([check("check:a", name="build")], {"build": "SUCCESS"}, 1),
        ):
            with self.subTest(expected=expected):
                path = write_state(self.root, iterations=0)
                self.read(path, ("head1", rollup), baseline)
                self.assertEqual(expected, MODULE.load_state(path)["iterations"])

    def test_charges_one_iteration_however_often_it_reads_the_checks(self):
        path = write_state(self.root, iterations=0)
        for _ in range(3):
            self.read(
                path, ("head1", [check("check:a", name="build")]), {"build": "SUCCESS"}
            )
        self.assertEqual(1, MODULE.load_state(path)["iterations"])

    def rerun_state(self, requested_minutes_ago=5):
        return write_state(
            self.root,
            reruns={
                "check:a": {
                    "count": 1,
                    "name": "build",
                    "run_id": 5,
                    "head_sha": "head1",
                    "requested_at": stamp(requested_minutes_ago),
                }
            },
            run={
                "attributions": {
                    "check:a": attribution("check:a", "flake", source="model")
                }
            },
        )

    def test_waits_rather_than_credit_the_failure_its_rerun_replaces(self):
        path = self.rerun_state()
        payload = self.read(
            path,
            ("head1", [check("check:a", name="build", completed_at=stamp(10))]),
        )
        self.assertEqual("waiting", payload["result"])
        self.assertIsNone(MODULE.load_state(path)["escalation"])

    def test_escalates_once_the_rerun_itself_fails(self):
        path = self.rerun_state()
        payload = self.read(
            path,
            ("head1", [check("check:a", name="build", completed_at=stamp(1))]),
        )
        self.assertEqual("escalate", payload["result"])
        self.assertEqual("flake_failed_twice", payload["reason"])


class PublishCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patcher = mock.patch.object(MODULE, "require_tools")
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_git(self, status="", rev_list="", show=""):
        def call_git(repo_root, *arguments):
            if arguments[0] == "status":
                return status
            if arguments[0] == "rev-list":
                return rev_list
            if arguments[0] == "rev-parse":
                return "local1"
            if arguments[0] == "show":
                return show
            raise AssertionError(f"unexpected git call: {arguments}")

        return call_git

    def test_refuses_a_dirty_worktree(self):
        path = write_state(self.root)
        with mock.patch.object(MODULE, "git", self.fake_git(status=" M app.py")):
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("publish", "--state", str(path))
        self.assertIn("worktree is not clean", str(error.exception))

    def test_refuses_a_batch_that_is_still_planned(self):
        path = write_state(
            self.root, run={"batches": [{"id": "b1", "status": "planned"}]}
        )
        with mock.patch.object(MODULE, "git", self.fake_git()):
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("publish", "--state", str(path))
        self.assertIn("neither recorded nor skipped", str(error.exception))

    def test_refuses_to_publish_partial_work_after_a_skip(self):
        path = write_state(
            self.root, run={"batches": [{"id": "b1", "status": "skipped"}]}
        )
        with mock.patch.object(MODULE, "git", self.fake_git()):
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("publish", "--state", str(path))
        self.assertIn("without publishing partial work", str(error.exception))

    def test_refuses_a_local_commit_no_batch_recorded(self):
        path = write_state(
            self.root,
            run={
                "batches": [
                    {"id": "b1", "status": "recorded", "commit": None,
                     "summary": "no code change", "rationale": "none"}
                ]
            },
        )
        with mock.patch.object(MODULE, "git", self.fake_git(rev_list="sneaky1")):
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("publish", "--state", str(path))
        self.assertIn("unrecorded ['sneaky1']", str(error.exception))

    def test_reports_nothing_to_publish_when_no_commit_was_made(self):
        path = write_state(
            self.root,
            run={
                "batches": [
                    {"id": "b1", "status": "recorded", "commit": None,
                     "summary": "no code change", "rationale": "none"}
                ]
            },
        )
        with mock.patch.object(MODULE, "git", self.fake_git()):
            payload = call("publish", "--state", str(path))
        self.assertEqual("nothing_to_publish", payload["result"])

    def test_pushes_and_verifies_the_new_head(self):
        path = write_state(
            self.root,
            run={
                "batches": [
                    {"id": "b1", "status": "recorded", "commit": "local1",
                     "summary": "fixed the import", "rationale": None}
                ]
            },
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(MODULE, "git", self.fake_git(rev_list="local1"))
            )
            stack.enter_context(
                mock.patch.object(MODULE, "find_push_remote", return_value="origin")
            )
            stack.enter_context(
                mock.patch.object(MODULE, "remote_head", side_effect=["head1", "local1"])
            )
            stack.enter_context(
                mock.patch.object(MODULE, "wait_for_remote_head", return_value="local1")
            )
            stack.enter_context(
                mock.patch.object(
                    MODULE, "metadata_for", return_value={"head_sha": "local1"}
                )
            )
            push = stack.enter_context(mock.patch.object(MODULE, "run"))
            payload = call("publish", "--state", str(path))
        push.assert_called_once()
        self.assertEqual("published", payload["result"])
        self.assertEqual(["local1"], payload["commits"])
        state = MODULE.load_state(path)
        self.assertEqual("published", state["run"]["status"])
        self.assertEqual({}, state["reruns"])

    def test_records_the_local_validation_behind_the_push(self):
        """The state has to say what ran, or a live run proves nothing.

        This loop pays for a wrong guess in whole CI cycles, so the record of
        what it ran before spending one is the point of the requirement.
        """
        path = write_state(
            self.root,
            run={
                "batches": [
                    {"id": "b1", "status": "recorded", "commit": "local1",
                     "summary": "fixed the import", "rationale": None}
                ]
            },
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(MODULE, "git", self.fake_git(rev_list="local1"))
            )
            stack.enter_context(
                mock.patch.object(MODULE, "find_push_remote", return_value="origin")
            )
            stack.enter_context(
                mock.patch.object(MODULE, "remote_head", side_effect=["head1", "local1"])
            )
            stack.enter_context(
                mock.patch.object(MODULE, "wait_for_remote_head", return_value="local1")
            )
            stack.enter_context(
                mock.patch.object(
                    MODULE, "metadata_for", return_value={"head_sha": "local1"}
                )
            )
            stack.enter_context(mock.patch.object(MODULE, "run"))
            payload = call(
                "publish",
                "--state",
                str(path),
                "--validated",
                "the failing check",
                "--rewrote",
                "the fixing form",
            )
        state = MODULE.load_state(path)
        self.assertEqual(
            [
                {
                    "head_sha": "local1",
                    "status": "passed",
                    "commands": ["the failing check", "the fixing form"],
                    "rewrote": ["the fixing form"],
                }
            ],
            state["local_validation"],
        )
        self.assertEqual(state["local_validation"][-1], payload["local_validation"])

    def test_publishes_a_fix_that_could_not_be_reproduced_locally(self):
        """A check that only runs in CI must not hold a fix back.

        Refusing to push there would turn an ordinary repository into an
        escalation, which costs more than the failure this record watches for.
        """
        path = write_state(
            self.root,
            run={
                "batches": [
                    {"id": "b1", "status": "recorded", "commit": "local1",
                     "summary": "fixed the import", "rationale": None}
                ]
            },
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(MODULE, "git", self.fake_git(rev_list="local1"))
            )
            stack.enter_context(
                mock.patch.object(MODULE, "find_push_remote", return_value="origin")
            )
            stack.enter_context(
                mock.patch.object(MODULE, "remote_head", side_effect=["head1", "local1"])
            )
            stack.enter_context(
                mock.patch.object(MODULE, "wait_for_remote_head", return_value="local1")
            )
            stack.enter_context(
                mock.patch.object(
                    MODULE, "metadata_for", return_value={"head_sha": "local1"}
                )
            )
            push = stack.enter_context(mock.patch.object(MODULE, "run"))
            payload = call(
                "publish",
                "--state",
                str(path),
                "--not-validated",
                "the check needs a container this workspace has no access to",
            )
        push.assert_called_once()
        self.assertEqual("published", payload["result"])
        state = MODULE.load_state(path)
        self.assertEqual("skipped", state["local_validation"][-1]["status"])
        self.assertEqual(
            "the check needs a container this workspace has no access to",
            state["local_validation"][-1]["reason"],
        )

    def test_refuses_when_the_pull_request_head_does_not_catch_up(self):
        path = write_state(
            self.root,
            run={
                "batches": [
                    {"id": "b1", "status": "recorded", "commit": "local1",
                     "summary": "fixed", "rationale": None}
                ]
            },
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(MODULE, "git", self.fake_git(rev_list="local1"))
            )
            stack.enter_context(
                mock.patch.object(MODULE, "find_push_remote", return_value="origin")
            )
            stack.enter_context(
                mock.patch.object(MODULE, "remote_head", return_value="head1")
            )
            stack.enter_context(
                mock.patch.object(MODULE, "wait_for_remote_head", return_value="local1")
            )
            stack.enter_context(
                mock.patch.object(
                    MODULE, "metadata_for", return_value={"head_sha": "head1"}
                )
            )
            stack.enter_context(mock.patch.object(MODULE, "run"))
            stack.enter_context(mock.patch.object(MODULE.time, "sleep"))
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("publish", "--state", str(path))
        self.assertIn("PR head mismatch", str(error.exception))

    def test_refuses_to_push_a_commit_amended_to_suppress_a_test(self):
        """`record` already passed. An amend after it would reach GitHub unseen.

        This is the last gate before anything leaves the machine, so it reads the
        commits it is about to push rather than trusting what was recorded.
        """
        path = write_state(
            self.root,
            run={
                "batches": [
                    {"id": "b1", "status": "recorded", "commit": "local1",
                     "summary": "fixed the import", "rationale": None}
                ]
            },
        )
        git = self.fake_git(rev_list="local1", show="D\tsrc/test/java/FooTest.java")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(MODULE, "git", git))
            push = stack.enter_context(mock.patch.object(MODULE, "run"))
            with self.assertRaises(MODULE.WorkflowError) as error:
                call("publish", "--state", str(path))
        self.assertIn("stopping a test from running", str(error.exception))
        self.assertIn("FooTest.java", str(error.exception))
        push.assert_not_called()
        self.assertNotEqual("published", MODULE.load_state(path)["run"]["status"])


class StatusCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_reports_a_machine_readable_snapshot(self):
        path = write_state(
            self.root,
            outcome="green",
            clean_at_head_sha="head1",
            run={
                "checks": [check("check:a", klass="passed")],
                "attributions": {"check:a": attribution("check:a", "pr_caused")},
                "decision": {"decision": "green", "action": "green",
                             "reason": "all_checks_passed"},
                "batches": [{"id": "b1", "status": "recorded"}],
            },
        )
        payload = call("status", "--state", str(path))
        self.assertEqual("ready", payload["result"])
        self.assertEqual("green", payload["outcome"])
        self.assertEqual("head1", payload["clean_at_head_sha"])
        self.assertEqual("green", payload["run"]["decision"])
        self.assertEqual({"recorded": 1}, payload["run"]["batch_statuses"])
        self.assertEqual({"check:a": "pr_caused"}, payload["verdicts"])
        self.assertEqual(1, payload["counts"]["passed"])
        self.assertTrue(Path(payload["status_path"]).is_file())

    def test_status_reports_when_the_helper_last_wrote_its_state(self):
        """The only signal a reader has for telling working from wedged.

        Every write stamps it, so a stamp minutes old and a stamp an hour old
        are different answers to the question a person actually asks.
        """
        path = write_state(self.root, updated_at="2026-02-03T04:05:06Z")
        payload = call("status", "--state", str(path))
        self.assertEqual("2026-02-03T04:05:06Z", payload["last_helper_activity"])
        snapshot = json.loads(
            Path(payload["status_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual("2026-02-03T04:05:06Z", snapshot["last_helper_activity"])

    def test_reports_an_escalation(self):
        path = write_state(
            self.root,
            escalation={
                "reason": "pre_existing_failures",
                "detail": "build already fails on main",
                "checks": ["check:a"],
                "next_action": MODULE.ESCALATION_ACTIONS["pre_existing_failures"],
                "head_sha": "head1",
                "recorded_at": "2026-01-01T00:00:00Z",
            },
        )
        payload = call("status", "--state", str(path))
        self.assertEqual("pre_existing_failures", payload["escalation"]["reason"])
        self.assertTrue(payload["escalation"]["next_action"])

    def test_reports_no_state_for_a_pull_request_the_loop_never_touched(self):
        target = MODULE.parse_target("owner/repo#404")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(MODULE, "require_tools"))
            stack.enter_context(
                mock.patch.object(MODULE, "resolve_repo_root", return_value=self.root)
            )
            stack.enter_context(
                mock.patch.object(MODULE, "current_pr_target", return_value=target)
            )
            stack.enter_context(
                mock.patch.object(
                    MODULE,
                    "default_state_path",
                    return_value=self.root / "missing.json",
                )
            )
            payload = call("status", "--current", "--repo-root", str(self.root))
        self.assertEqual("no_state", payload["result"])
        self.assertIsNone(payload["escalation"])

    def test_omits_the_stage_outcome_when_no_run_happened(self):
        """A missing state file is not a run that ended, so it names no ending.

        Emitting `no_progress` here would tell any reader that the stage ran and
        accomplished nothing, which is false both for a stage that was never
        launched and for one that cleared and then cleaned up after itself.
        """
        target = MODULE.parse_target("owner/repo#404")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(MODULE, "require_tools"))
            stack.enter_context(
                mock.patch.object(MODULE, "resolve_repo_root", return_value=self.root)
            )
            stack.enter_context(
                mock.patch.object(MODULE, "current_pr_target", return_value=target)
            )
            stack.enter_context(
                mock.patch.object(
                    MODULE,
                    "default_state_path",
                    return_value=self.root / "missing.json",
                )
            )
            payload = call("status", "--current", "--repo-root", str(self.root))
        self.assertEqual("no_state", payload["result"])
        self.assertNotIn("stage_outcome", payload)
        self.assertNotIn(
            "no_progress", json.dumps(payload), "no payload field may claim a run ended"
        )

    def test_requires_a_state_or_the_current_flag(self):
        with self.assertRaises(SystemExit):
            run_arguments("status")

    def test_names_the_ending_in_the_vocabulary_an_orchestrator_records(self):
        for overrides, expected in (
            ({"outcome": "green"}, "cleared"),
            ({"outcome": "no_checks", "skip_note": "no applicable checks"}, "skipped"),
            ({"escalation": {"reason": "timeout"}}, "escalated"),
            ({"outcome": "green", "escalation": {"reason": "timeout"}}, "escalated"),
            ({"escalation": {"reason": "max_iterations_reached"}}, "carried"),
        ):
            with self.subTest(expected=expected):
                path = write_state(self.root, **overrides)
                payload = call("status", "--state", str(path))
                self.assertEqual(expected, payload["stage_outcome"])

    def test_omits_the_stage_outcome_while_a_run_has_decided_nothing(self):
        """State exists from preflight on, so its bare presence names no ending.

        A run killed before it decided anything leaves the same state a run still
        in flight leaves. Reporting `no_progress` for either would assert that a
        run completed and achieved nothing, and two of those in a row escalate the
        whole pipeline, so a crash could escalate a healthy pull request.
        """
        for overrides in ({}, {"outcome": None}, {"clean_at_head_sha": "head1"}):
            with self.subTest(overrides=overrides):
                path = write_state(self.root, **overrides)
                payload = call("status", "--state", str(path))
                self.assertNotIn("stage_outcome", payload)
                self.assertNotIn(
                    "no_progress",
                    json.dumps(payload),
                    "no payload field may claim a run ended",
                )

    def test_reports_the_skip_note_a_reader_cannot_mistake_for_a_pass(self):
        path = write_state(
            self.root,
            outcome="no_checks",
            skip_note=(
                "CI Fix Loop skipped owner/repo#7: the pull request head reports no "
                "applicable checks, so this repository ran no CI on it."
            ),
        )
        payload = call("status", "--state", str(path))
        self.assertEqual("skipped", payload["stage_outcome"])
        self.assertIn("no applicable checks", payload["skip_note"])
        self.assertIsNone(payload["clean_at_head_sha"])


class StageOutcomeTest(unittest.TestCase):
    def test_a_run_that_did_nothing_is_never_reported_as_clear(self):
        self.assertIsNone(MODULE.stage_outcome({}))
        self.assertIsNone(MODULE.stage_outcome({"clean_at_head_sha": "head1"}))

    def test_never_manufactures_an_ending_the_state_cannot_support(self):
        """`no_progress` is the agent's claim to make, never the helper's.

        Only a live agent can report that a run ran to completion and achieved
        nothing. The helper reads state that a killed run leaves looking exactly
        like a run still in flight, so it withholds the field instead.
        """
        for state in ({}, {"outcome": None}, {"run": {"status": "active"}}):
            with self.subTest(state=state):
                self.assertIsNone(MODULE.stage_outcome(state))
                self.assertEqual({}, MODULE.stage_outcome_fields(state))

    def test_carries_the_field_only_for_an_ending_it_can_name(self):
        self.assertEqual(
            {"stage_outcome": "cleared"},
            MODULE.stage_outcome_fields({"outcome": "green"}),
        )

    def test_an_escalation_outranks_a_recorded_clearance(self):
        state = {"outcome": "green", "escalation": {"reason": "head_changed"}}
        self.assertEqual("escalated", MODULE.stage_outcome(state))

    def test_a_spent_iteration_cap_is_carried(self):
        self.assertEqual(
            "carried",
            MODULE.stage_outcome(
                {"escalation": {"reason": "max_iterations_reached"}}
            ),
        )

    def test_a_clearance_always_travels_with_the_head_it_was_measured_at(self):
        """The orchestrator refuses a clearance whose marker names another head.

        That guard reads one payload, so the marker has to be in the same payload
        as the word. A `cleared` with no `clean_at_head_sha` beside it would be
        rejected as a mismatch and read as a stage that answered nothing.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = write_state(
                Path(directory),
                outcome="green",
                clean_at_head_sha="head1",
                run={"head_sha": "head1", "status": "resolved"},
            )
            payload = call("status", "--state", str(path))
        self.assertEqual("cleared", payload["stage_outcome"])
        self.assertEqual("head1", payload["clean_at_head_sha"])
        self.assertEqual("head1", payload["run"]["head_sha"])


class ChargeIterationTest(unittest.TestCase):
    def test_spends_one_iteration_for_a_run_however_often_it_is_called(self):
        state = {"iterations": 2}
        run_state = {"head_sha": "head1"}
        self.assertTrue(MODULE.charge_iteration(state, run_state))
        self.assertFalse(MODULE.charge_iteration(state, run_state))
        self.assertFalse(MODULE.charge_iteration(state, run_state))
        self.assertEqual(3, state["iterations"])
        self.assertTrue(run_state["charged"])

    def test_each_run_spends_its_own_iteration(self):
        state = {"iterations": 0}
        for head in ("head1", "head2", "head3"):
            MODULE.charge_iteration(state, {"head_sha": head})
        self.assertEqual(3, state["iterations"])

    def test_a_fresh_run_at_an_unchanged_head_costs_nothing(self):
        """The budget bounds fix attempts, and an attempt is what moves the head.

        A relaunch that re-derives the same analysis at the head already charged
        is one logical attempt read twice, so charging it again would spend a
        fifth of the budget on nothing.
        """
        state = {"iterations": 0}

        self.assertTrue(MODULE.charge_iteration(state, {"head_sha": "head1"}))
        for _ in range(4):
            self.assertFalse(MODULE.charge_iteration(state, {"head_sha": "head1"}))

        self.assertEqual(1, state["iterations"])
        self.assertEqual("head1", state["charged_head_sha"])

    def test_a_moved_head_is_a_new_attempt_and_charges_again(self):
        state = {"iterations": 0}

        MODULE.charge_iteration(state, {"head_sha": "head1"})
        MODULE.charge_iteration(state, {"head_sha": "head2"})

        self.assertEqual(2, state["iterations"])
        self.assertEqual("head2", state["charged_head_sha"])

    def test_a_run_with_no_head_is_charged_rather_than_deduped_on_a_guess(self):
        state = {"iterations": 0}

        for _ in range(3):
            self.assertTrue(MODULE.charge_iteration(state, {}))

        self.assertEqual(3, state["iterations"])
        self.assertNotIn("charged_head_sha", state)


class TestSuppressionTest(unittest.TestCase):
    def test_recognizes_a_test_path_by_directory_or_by_file_name(self):
        for path in (
            "src/test/java/com/example/FooTest.java",
            "tests/test_widget.py",
            "app/__tests__/widget.test.tsx",
            "pkg/thing_test.go",
            "spec/models/user_spec.rb",
            "lib/WidgetTests.cs",
            "TESTS/Upper_Test.py",
            "src\\test\\java\\FooTest.java",
        ):
            with self.subTest(path=path):
                self.assertTrue(MODULE.is_test_path(path))

    def test_leaves_production_code_alone(self):
        for path in (
            "src/main/java/com/example/Widget.java",
            "app/widget.ts",
            "docs/testing.md",
            "src/latest/thing.py",
            "",
            None,
            42,
        ):
            with self.subTest(path=path):
                self.assertFalse(MODULE.is_test_path(path))

    def test_names_every_way_a_line_stops_a_test_running(self):
        cases = {
            "@pytest.mark.skip(reason='broken')": "@pytest.mark.skip",
            "    @pytest.mark.xfail": "@pytest.mark.skip",
            "@unittest.skipIf(sys.platform == 'win32', 'nope')": "@unittest.skip",
            "        pytest.skip('flaky')": "pytest.skip()",
            "        self.skipTest('flaky')": "self.skipTest()",
            "  @Disabled(\"fails on CI\")": "@Disabled",
            "  @Ignore": "@Ignore",
            "  @Test(enabled = false)": "@Test(enabled = false)",
            "  xit('adds two numbers', () => {": "xit()",
            "  it.skip('adds two numbers', () => {": ".skip()",
            "  test.todo('adds two numbers')": ".todo()",
            "\tt.Skip(\"broken\")": "t.Skip()",
            "#[ignore]": "#[ignore]",
            "[Ignore(\"broken\")]": "[Ignore]",
            "  Skip = \"broken on arm\"": 'Skip = "..."',
        }
        for line, marker in cases.items():
            with self.subTest(line=line):
                self.assertIn(marker, MODULE.suppression_markers(line))

    def test_prose_about_a_skip_is_not_a_skip(self):
        """A pattern that fired on prose would refuse an honest commit.

        The refusal has no override, so a false positive stops the loop dead.
        These lines all mention skipping without doing any.
        """
        for line in (
            "# this test used to be skipped, and is not any more",
            "    assert result.skip is False",
            "// Ignore the ordering here; the assertion below is what matters.",
            "        self.assertEqual(expected, disabled_reason)",
            "  @Test(expected = IllegalStateException.class)",
            "  boolean enabled = false;",
            None,
            17,
        ):
            with self.subTest(line=line):
                self.assertEqual([], MODULE.suppression_markers(line))


class CommitSuppressionTest(unittest.TestCase):
    """Read real commits, because the scan parses real `git show` output."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        MODULE.git(self.root, "init", "--quiet", ".")
        MODULE.git(self.root, "config", "user.email", "loop@example.invalid")
        MODULE.git(self.root, "config", "user.name", "Loop")
        MODULE.git(self.root, "config", "commit.gpgsign", "false")

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")

    def commit(self, message):
        MODULE.git(self.root, "add", "--all")
        MODULE.git(self.root, "commit", "--quiet", "--message", message)
        return MODULE.git(self.root, "rev-parse", "HEAD")

    def test_reports_a_deleted_test_file(self):
        self.write("tests/test_widget.py", "def test_widget():\n    assert True\n")
        self.write("app.py", "value = 1\n")
        self.commit("first")
        (self.root / "tests" / "test_widget.py").unlink()
        head = self.commit("drop the test")
        findings = MODULE.commit_suppressions(self.root, head)
        self.assertEqual(
            [{"kind": "deleted_test_file", "path": "tests/test_widget.py", "marker": None}],
            findings,
        )

    def test_ignores_a_deleted_source_file(self):
        self.write("app.py", "value = 1\n")
        self.write("helper.py", "value = 2\n")
        self.commit("first")
        (self.root / "helper.py").unlink()
        head = self.commit("drop the helper")
        self.assertEqual([], MODULE.commit_suppressions(self.root, head))

    def test_reports_a_skip_added_to_a_test_that_was_running(self):
        self.write(
            "tests/test_widget.py",
            "def test_widget():\n    assert compute() == 2\n",
        )
        self.commit("first")
        self.write(
            "tests/test_widget.py",
            "import pytest\n\n\n"
            "@pytest.mark.skip(reason='fails on CI')\n"
            "def test_widget():\n    assert compute() == 2\n",
        )
        head = self.commit("silence the test")
        findings = MODULE.commit_suppressions(self.root, head)
        self.assertEqual(1, len(findings))
        self.assertEqual("added_suppression", findings[0]["kind"])
        self.assertEqual("tests/test_widget.py", findings[0]["path"])
        self.assertEqual("@pytest.mark.skip", findings[0]["marker"])

    def test_ignores_a_skip_that_the_commit_removed(self):
        """Re-enabling a test is the opposite of suppressing one."""
        self.write(
            "tests/test_widget.py",
            "import pytest\n\n\n@pytest.mark.skip\ndef test_widget():\n    pass\n",
        )
        self.commit("first")
        self.write(
            "tests/test_widget.py",
            "import pytest\n\n\ndef test_widget():\n    pass\n",
        )
        head = self.commit("re-enable the test")
        self.assertEqual([], MODULE.commit_suppressions(self.root, head))

    def test_ignores_an_annotation_outside_a_test_file(self):
        self.write("app.py", "value = 1\n")
        self.commit("first")
        self.write("app.py", "value = 1\n# @Disabled\n")
        head = self.commit("comment")
        self.assertEqual([], MODULE.commit_suppressions(self.root, head))

    def test_a_new_test_file_that_is_born_skipped_is_reported(self):
        """Adding a test already disabled is coverage that never runs."""
        self.write("app.py", "value = 1\n")
        self.commit("first")
        self.write(
            "tests/test_new.py",
            "import pytest\n\n\n@pytest.mark.skip\ndef test_new():\n    pass\n",
        )
        head = self.commit("add a disabled test")
        markers = [item["marker"] for item in MODULE.commit_suppressions(self.root, head)]
        self.assertEqual(["@pytest.mark.skip"], markers)

    def test_refusal_names_the_commit_and_the_finding(self):
        self.write("tests/test_widget.py", "def test_widget():\n    pass\n")
        self.commit("first")
        (self.root / "tests" / "test_widget.py").unlink()
        head = self.commit("drop the test")
        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.refuse_test_suppression(self.root, [head])
        message = str(error.exception)
        self.assertIn("stopping a test from running", message)
        self.assertIn("tests/test_widget.py", message)
        self.assertIn(head, message)
        self.assertIn("unfixable_failure", message)

    def test_an_honest_fix_passes(self):
        self.write("app.py", "def compute():\n    return 1\n")
        self.write("tests/test_widget.py", "def test_widget():\n    assert True\n")
        self.commit("first")
        self.write("app.py", "def compute():\n    return 2\n")
        head = self.commit("fix the arithmetic")
        MODULE.refuse_test_suppression(self.root, [head])


class PipelineBudgetTest(unittest.TestCase):
    """A stage budget belongs to an outer loop's iteration, not to a launch."""

    RECORDED = {"run": "run-a", "iteration": 2, "baseline": 3, "run_baseline": 1}

    def scope(self, state, **pipeline):
        return MODULE.pipeline_scope(state, SimpleNamespace(**pipeline))

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

    def test_a_standalone_run_never_resets_anything(self):
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
                state = {"iterations": 4}
                self.assertIsNone(self.scope(state, **pipeline))
                self.assertEqual(4, state["iterations"])
                self.assertNotIn("pipeline_budget", state)

    def test_a_new_pipeline_run_clears_both_budgets(self):
        state = {"iterations": 5, "pipeline_budget": dict(self.RECORDED)}

        scope = self.scope(state, pipeline_run="run-b", pipeline_iteration=1)

        self.assertEqual(
            {"run": "run-b", "iteration": 1, "baseline": 5, "run_baseline": 5}, scope
        )
        self.assertEqual((0, 0), MODULE.budget_spent(state, scope))

    def test_the_pipeline_advancing_clears_only_the_per_iteration_budget(self):
        """The whole-run ceiling must survive an advance, or it bounds nothing."""
        state = {"iterations": 9, "pipeline_budget": dict(self.RECORDED)}

        scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=3)

        self.assertEqual(
            {"run": "run-a", "iteration": 3, "baseline": 9, "run_baseline": 1}, scope
        )
        self.assertEqual((0, 8), MODULE.budget_spent(state, scope))

    def test_a_relaunch_inside_one_iteration_buys_nothing(self):
        state = {"iterations": 9, "pipeline_budget": dict(self.RECORDED)}

        scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=2)

        self.assertEqual(self.RECORDED, scope)
        self.assertEqual((6, 8), MODULE.budget_spent(state, scope))

    def test_replaying_an_earlier_iteration_buys_nothing(self):
        """Strictly greater, so a repeat and a replay both buy nothing."""
        state = {"iterations": 9, "pipeline_budget": dict(self.RECORDED)}

        scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=1)

        self.assertEqual(self.RECORDED, scope)

    def test_a_second_run_resets_even_though_it_counts_from_one_again(self):
        """A pipeline numbers its iterations from one, so this must not be ordered.

        Comparing iterations across runs would leave a pull request that reached
        iteration three permanently unable to reset, and the ceiling would then
        refuse every future run on it. A deadlock outlasts the false start it
        would have prevented, so run identity is compared for equality instead.
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

    def test_a_reset_never_rewrites_the_durable_count_itself(self):
        """Both budgets are baselines, so the per-PR iteration numbering stays monotone.

        Zeroing the count instead would restart the numbering, and a run id built
        from it would collide with one already folded into history, where a
        duplicate is dropped rather than recorded.
        """
        state = {"iterations": 9, "pipeline_budget": dict(self.RECORDED)}

        self.scope(state, pipeline_run="run-b", pipeline_iteration=1)

        self.assertEqual(9, state["iterations"])

    def test_an_iteration_with_no_run_is_ignored_rather_than_half_applied(self):
        """A run token must come from the caller, never from what this loop recorded.

        Reading it back out of an earlier budget, a head it pushed, or an
        escalation it wrote would be this loop naming its own position.
        """
        states = (
            {},
            {"iterations": 4},
            {"iterations": 4, "pipeline_budget": dict(self.RECORDED)},
            {"iterations": 4, "pr": {"head_sha": "aaaa"}, "history": [{"id": "one"}]},
            {"iterations": 4, "escalation": {"reason": "max_iterations_reached"}},
            {"iterations": 4, "clean_at_head_sha": "aaaa"},
        )
        for state in states:
            with self.subTest(state=state):
                self.assertIsNone(
                    self.scope(dict(state), pipeline_iteration=2, pipeline_max_iterations=3)
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
            {"pr": {"head_sha": "another-head"}, "run": {"status": "published"}},
            {"escalation": {"reason": "max_iterations_reached"}},
            {"history": [{"id": "one"}, {"id": "two"}]},
            {"clean_at_head_sha": "new-head"},
            {"reruns": {"1": 2}},
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

    def test_an_omitted_outer_cap_falls_back_rather_than_disabling_the_ceiling(self):
        """Only the outer cap is optional, and omitting it must not remove the bound."""
        scope = {"run": "run-a", "iteration": 1, "baseline": 0, "run_baseline": 0}
        for value in (None, 0, -1, True, "3"):
            with self.subTest(value=value):
                self.assertEqual(
                    5 * MODULE.DEFAULT_PIPELINE_MAX_ITERATIONS,
                    MODULE.absolute_iteration_cap(scope, 5, value),
                )

    def test_the_ceiling_is_derived_from_the_callers_own_cap(self):
        scope = {"run": "run-a", "iteration": 1, "baseline": 0, "run_baseline": 0}
        self.assertEqual(15, MODULE.absolute_iteration_cap(scope, 5, 3))
        self.assertEqual(20, MODULE.absolute_iteration_cap(scope, 10, 2))

    def test_there_is_no_ceiling_without_a_pipeline(self):
        self.assertIsNone(MODULE.absolute_iteration_cap(None, 5, 3))

    def test_names_which_budget_ran_out(self):
        scope = {"run": "run-a", "iteration": 4, "baseline": 10, "run_baseline": 0}
        self.assertIsNone(MODULE.exhausted_budget({"iterations": 14}, scope, 5, 20))
        self.assertEqual(
            "iteration", MODULE.exhausted_budget({"iterations": 15}, scope, 5, 20)
        )
        self.assertEqual(
            "absolute", MODULE.exhausted_budget({"iterations": 10}, scope, 5, 10)
        )
        self.assertEqual(
            "iteration", MODULE.exhausted_budget({"iterations": 5}, None, 5, None)
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

    def test_the_running_total_survives_a_pipeline_iteration(self):
        """The ceiling only bounds anything if the per-iteration reset spares it."""
        state = {"iterations": 0}
        head = 0
        for iteration in (1, 2):
            scope = self.scope(state, pipeline_run="run-a", pipeline_iteration=iteration)
            state["pipeline_budget"] = scope
            state.pop("charged_head_sha", None)
            for _ in range(5):
                head += 1
                MODULE.charge_iteration(state, {"head_sha": f"head{head}"})
        self.assertEqual(10, state["iterations"])
        self.assertEqual((5, 10), MODULE.budget_spent(state, scope))
        self.assertEqual("absolute", MODULE.exhausted_budget(state, scope, 5, 10))

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


class BudgetAdvancedTest(unittest.TestCase):
    """The per-head charge lives exactly as long as the budget it protects."""

    RECORDED = {"run": "run-a", "iteration": 2, "baseline": 3, "run_baseline": 1}

    def test_a_new_run_or_a_later_iteration_both_count_as_an_advance(self):
        for scope in (
            {"run": "run-b", "iteration": 1},
            {"run": "run-a", "iteration": 3},
            {"run": "run-a", "iteration": 99},
        ):
            with self.subTest(scope=scope):
                self.assertTrue(MODULE.budget_advanced(self.RECORDED, scope))

    def test_a_repeat_a_replay_and_a_standalone_run_are_not_an_advance(self):
        for recorded, scope in (
            (self.RECORDED, {"run": "run-a", "iteration": 2}),
            (self.RECORDED, {"run": "run-a", "iteration": 1}),
            (self.RECORDED, {"run": "run-a", "iteration": None}),
            (self.RECORDED, None),
            (None, None),
        ):
            with self.subTest(recorded=recorded, scope=scope):
                self.assertFalse(MODULE.budget_advanced(recorded, scope))

    def test_a_run_scoped_budget_advances_the_first_time_it_learns_an_iteration(self):
        recorded = {"run": "run-a", "iteration": None, "baseline": 5, "run_baseline": 5}
        self.assertTrue(MODULE.budget_advanced(recorded, {"run": "run-a", "iteration": 1}))

    def test_nothing_recorded_yet_reads_as_an_advance(self):
        self.assertTrue(MODULE.budget_advanced(None, {"run": "run-a", "iteration": 1}))
        self.assertTrue(MODULE.budget_advanced("junk", {"run": "run-a", "iteration": 1}))


class CleanupCommandTest(unittest.TestCase):
    def test_deletes_the_state_and_every_side_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_state(root)
            for side in (
                MODULE.preflight_path_for(path),
                MODULE.checks_path_for(path),
                MODULE.status_path_for(path),
            ):
                side.write_text("{}", encoding="utf-8")
            payload = call("cleanup", "--state", str(path))
            self.assertEqual("cleaned_up", payload["result"])
            self.assertFalse(path.exists())
            self.assertFalse(MODULE.diff_path_for(path).exists())
            self.assertFalse(MODULE.preflight_path_for(path).exists())
            self.assertFalse(MODULE.checks_path_for(path).exists())
            self.assertFalse(MODULE.status_path_for(path).exists())


class PreflightCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
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
            "is_fork": True,
            "is_draft": True,
            "commits": [{"sha": "c1", "message": "Add a thing"}],
        }

    def preflight(self, stack, *, status="", head="head1", state_path=None, pipeline=()):
        def call_git(repo_root, *arguments):
            if arguments[0] == "status":
                return status
            if arguments[0] == "rev-parse":
                return head
            if arguments[0] == "branch":
                return "feature"
            raise AssertionError(f"unexpected git call: {arguments}")

        stack.enter_context(mock.patch.object(MODULE, "require_tools"))
        stack.enter_context(
            mock.patch.object(MODULE, "resolve_repo_root", return_value=self.root)
        )
        stack.enter_context(mock.patch.object(MODULE, "git", call_git))
        stack.enter_context(
            mock.patch.object(MODULE, "metadata_for", return_value=self.metadata)
        )
        stack.enter_context(mock.patch.object(MODULE, "checkout_pr", return_value=True))
        stack.enter_context(
            mock.patch.object(MODULE, "fetch_authoritative_diff", return_value=DIFF)
        )
        stack.enter_context(
            mock.patch.object(MODULE, "changed_files_for", return_value=["app.py"])
        )
        stack.enter_context(
            mock.patch.object(MODULE, "commit_provenance", return_value=[])
        )
        return call(
            "preflight",
            "owner/repo#7",
            "--repo-root",
            str(self.root),
            "--state",
            str(state_path or self.root / "state.json"),
            *pipeline,
        )

    def test_pins_the_head_and_the_diff(self):
        with contextlib.ExitStack() as stack:
            payload = self.preflight(stack)
        self.assertEqual("ready", payload["result"])
        self.assertEqual("head1", payload["head_sha"])
        self.assertEqual("base1", payload["base_sha"])
        self.assertEqual(1, payload["iteration"])
        self.assertEqual(5, payload["max_iterations"])
        self.assertEqual(DIFF, Path(payload["diff_path"]).read_text(encoding="utf-8"))
        self.assertTrue(Path(payload["preflight_path"]).is_file())

    def test_refuses_a_dirty_worktree(self):
        with contextlib.ExitStack() as stack:
            with self.assertRaises(MODULE.WorkflowError) as error:
                self.preflight(stack, status=" M app.py")
        self.assertIn("worktree is not clean", str(error.exception))

    def test_refuses_a_local_head_that_is_not_the_pull_request_head(self):
        with contextlib.ExitStack() as stack:
            with self.assertRaises(MODULE.WorkflowError) as error:
                self.preflight(stack, head="other1")
        self.assertIn("HEAD mismatch", str(error.exception))

    def test_reading_the_checks_again_spends_no_iteration(self):
        path = self.root / "state.json"
        for _ in range(3):
            with contextlib.ExitStack() as stack:
                payload = self.preflight(stack, state_path=path)
            self.assertEqual(1, payload["iteration"])
            self.assertEqual("ready", payload["result"])
        self.assertEqual(0, MODULE.load_state(path)["iterations"])

    def test_forgets_the_outcome_the_previous_run_recorded(self):
        path = write_state(
            self.root, outcome="green", clean_at_head_sha="head1", iterations=1
        )
        with contextlib.ExitStack() as stack:
            payload = self.preflight(stack, state_path=path)
        self.assertEqual("ready", payload["result"])
        state = MODULE.load_state(path)
        self.assertIsNone(state["outcome"])
        self.assertIsNone(state["clean_at_head_sha"])
        self.assertIsNone(MODULE.stage_outcome(state))

    def test_stops_at_the_iteration_cap(self):
        path = self.root / "state.json"
        for index in range(MODULE.DEFAULT_MAX_ITERATIONS):
            # A charge is per head, so each attempt has to land on its own head
            # the way a real fix does.
            head = f"head{index + 1}"
            self.metadata["head_sha"] = head
            with contextlib.ExitStack() as stack:
                self.preflight(stack, head=head, state_path=path)
            state = MODULE.load_state(path)
            MODULE.charge_iteration(state, state["run"])
            MODULE.save_state(path, state)
        self.metadata["head_sha"] = "head-last"
        with contextlib.ExitStack() as stack:
            payload = self.preflight(stack, head="head-last", state_path=path)
        self.assertEqual("max_iterations_reached", payload["result"])
        escalation = MODULE.load_state(path)["escalation"]
        self.assertEqual("max_iterations_reached", escalation["reason"])
        self.assertTrue(escalation["next_action"])

    def test_a_second_preflight_at_a_charged_head_costs_nothing_and_keeps_its_number(self):
        """One logical attempt, read twice, must be billed once and numbered once.

        Advancing the number without charging would let the label outrun the
        budget, and a third read would then mint ids that collide with the second
        read's archived entries, which `archive_run` drops rather than records.
        """
        path = self.root / "state.json"
        with contextlib.ExitStack() as stack:
            first = self.preflight(stack, state_path=path)
        state = MODULE.load_state(path)
        MODULE.charge_iteration(state, state["run"])
        MODULE.save_state(path, state)
        self.assertEqual(1, first["iteration"])
        self.assertEqual(1, state["iterations"])

        for _ in range(3):
            with contextlib.ExitStack() as stack:
                again = self.preflight(stack, state_path=path)
            self.assertEqual("ready", again["result"])
            self.assertEqual(1, again["iteration"])
            self.assertEqual(1, MODULE.load_state(path)["iterations"])

    def test_a_preflight_after_the_head_moved_is_a_new_attempt(self):
        path = self.root / "state.json"
        with contextlib.ExitStack() as stack:
            self.preflight(stack, state_path=path)
        state = MODULE.load_state(path)
        MODULE.charge_iteration(state, state["run"])
        MODULE.save_state(path, state)

        self.metadata["head_sha"] = "head2"
        with contextlib.ExitStack() as stack:
            moved = self.preflight(stack, head="head2", state_path=path)

        self.assertEqual(2, moved["iteration"])

    def test_a_pipeline_iteration_frees_the_head_it_already_charged(self):
        """The per-head charge protects one budget and must not outlive it."""
        path = self.root / "state.json"
        with contextlib.ExitStack() as stack:
            self.preflight(
                stack,
                state_path=path,
                pipeline=["--pipeline-run", "run-a", "--pipeline-iteration", "1"],
            )
        state = MODULE.load_state(path)
        MODULE.charge_iteration(state, state["run"])
        MODULE.save_state(path, state)
        self.assertEqual("head1", MODULE.load_state(path)["charged_head_sha"])

        with contextlib.ExitStack() as stack:
            advanced = self.preflight(
                stack,
                state_path=path,
                pipeline=["--pipeline-run", "run-a", "--pipeline-iteration", "2"],
            )

        self.assertNotIn("charged_head_sha", MODULE.load_state(path))
        self.assertEqual(2, advanced["iteration"])
        complete = json.loads(
            Path(advanced["preflight_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(0, complete["completed_iterations"])

    def test_a_pipeline_iteration_never_rewrites_the_durable_count(self):
        """Zeroing it would restart the numbering and collide with archived ids.

        `archive_run` keys history on the iteration number, and it drops a
        duplicate rather than recording it, so a budget that rewrote the count
        would silently lose the second attempt's verdicts.
        """
        path = self.root / "state.json"
        for index, head in enumerate(("head1", "head2")):
            self.metadata["head_sha"] = head
            with contextlib.ExitStack() as stack:
                self.preflight(
                    stack,
                    head=head,
                    state_path=path,
                    pipeline=[
                        "--pipeline-run",
                        "run-a",
                        "--pipeline-iteration",
                        str(index + 1),
                    ],
                )
            state = MODULE.load_state(path)
            MODULE.charge_iteration(state, state["run"])
            state["run"]["attributions"] = {
                "check:a": {"verdict": "pr_caused", "name": "a"}
            }
            MODULE.save_state(path, state)

        self.metadata["head_sha"] = "head3"
        with contextlib.ExitStack() as stack:
            self.preflight(
                stack,
                head="head3",
                state_path=path,
                pipeline=["--pipeline-run", "run-a", "--pipeline-iteration", "2"],
            )

        state = MODULE.load_state(path)
        self.assertEqual(2, state["iterations"])
        self.assertEqual(
            ["1:verdict:check:a", "2:verdict:check:a"],
            sorted(entry["id"] for entry in state["history"]),
        )

    def test_a_new_head_forgets_the_reruns_of_the_old_one(self):
        path = write_state(self.root, reruns={"check:a": {"count": 1}})
        self.metadata["head_sha"] = "head2"
        with contextlib.ExitStack() as stack:
            self.preflight(stack, head="head2", state_path=path)
        self.assertEqual({}, MODULE.load_state(path)["reruns"])

    def test_keeps_the_reruns_of_the_same_head(self):
        path = write_state(self.root, reruns={"check:a": {"count": 1}})
        with contextlib.ExitStack() as stack:
            self.preflight(stack, state_path=path)
        self.assertEqual(1, MODULE.load_state(path)["reruns"]["check:a"]["count"])


class MainTest(unittest.TestCase):
    def test_reports_a_workflow_error_as_json_and_a_failure_code(self):
        stream = io.StringIO()
        with mock.patch.object(
            MODULE.sys, "argv", ["ci_fix_loop.py", "cleanup", "--state", "missing.json"]
        ):
            with contextlib.redirect_stdout(stream):
                code = MODULE.main()
        self.assertEqual(1, code)
        payload = json.loads(stream.getvalue())
        self.assertEqual("error", payload["result"])
        self.assertIn("state file does not exist", payload["error"])

    def test_reports_success_with_a_zero_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_state(Path(directory))
            stream = io.StringIO()
            with mock.patch.object(
                MODULE.sys, "argv", ["ci_fix_loop.py", "cleanup", "--state", str(path)]
            ):
                with contextlib.redirect_stdout(stream):
                    code = MODULE.main()
            self.assertEqual(0, code)
            self.assertEqual("cleaned_up", json.loads(stream.getvalue())["result"])


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
