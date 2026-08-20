import contextlib
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import tempfile
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


class AgentInstructionsTest(unittest.TestCase):
    def setUp(self):
        self.instructions = AGENT.read_text(encoding="utf-8")

    def test_declares_the_frontmatter_the_siblings_use(self):
        self.assertIn("name: CI Fix Loop", self.instructions)
        self.assertIn(
            'argument-hint: "PR URL, PR number, or owner/repo#number; omit to use '
            "the current branch's PR\"",
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
            "It reports `cleared`, `skipped`, and `escalated`, and it leaves the "
            "field out entirely when the state names no ending.",
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
            "`--pipeline-run` and `--pipeline-iteration` go together",
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
            "`resolve --state <path> --outcome no_checks`", self.instructions
        )
        self.assertIn(
            "A broken continuous integration configuration must never look like a "
            "green pipeline.",
            self.instructions,
        )

    def test_caps_the_loop_at_five_iterations(self):
        self.assertIn("The maximum is 5 iterations.", self.instructions)
        self.assertIn("max_iterations_reached", self.instructions)

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

    def test_a_running_check_escalates_once_the_wait_runs_out(self):
        decision = self.decide(
            [check("check:a", klass="running")], deadline_expired=True
        )
        self.assertEqual("escalate", decision["decision"])
        self.assertEqual("timeout", decision["reason"])

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
        payload = self.read(path, ("head1", [check("check:a", klass="passed")]))
        self.assertEqual("green", payload["result"])
        self.assertEqual(1, payload["counts"]["passed"])
        self.assertTrue(Path(payload["checks_path"]).is_file())

    def test_reports_a_repository_with_no_checks(self):
        path = write_state(self.root)
        payload = self.read(path, ("head1", []))
        self.assertEqual("no_checks", payload["result"])
        self.assertEqual("no_applicable_checks", payload["reason"])

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
        run_state = {}
        self.assertTrue(MODULE.charge_iteration(state, run_state))
        self.assertFalse(MODULE.charge_iteration(state, run_state))
        self.assertFalse(MODULE.charge_iteration(state, run_state))
        self.assertEqual(3, state["iterations"])
        self.assertTrue(run_state["charged"])

    def test_each_run_spends_its_own_iteration(self):
        state = {"iterations": 0}
        for _ in range(3):
            MODULE.charge_iteration(state, {})
        self.assertEqual(3, state["iterations"])


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
    def test_a_position_needs_both_halves(self):
        self.assertEqual(("run1", 2), MODULE.pipeline_position("run1", 2))
        for run, iteration in (
            (None, 2),
            ("", 2),
            ("run1", None),
            ("run1", 0),
            ("run1", -1),
            ("run1", "2"),
            ("run1", True),
            (7, 2),
        ):
            with self.subTest(run=run, iteration=iteration):
                self.assertIsNone(MODULE.pipeline_position(run, iteration))

    def test_a_standalone_run_never_resets_anything(self):
        state = {"iterations": 4, "total_iterations": 9}
        self.assertIsNone(MODULE.apply_pipeline_position(state, None, None))
        self.assertEqual(4, state["iterations"])
        self.assertEqual(9, state["total_iterations"])
        self.assertNotIn("pipeline_run", state)

    def test_a_new_pipeline_run_clears_both_budgets(self):
        state = {"iterations": 5, "total_iterations": 10, "pipeline_run": "old"}
        self.assertEqual("run", MODULE.apply_pipeline_position(state, "new", 1))
        self.assertEqual(0, state["iterations"])
        self.assertEqual(0, state["total_iterations"])
        self.assertEqual("new", state["pipeline_run"])
        self.assertEqual(1, state["pipeline_iteration"])

    def test_the_pipeline_advancing_clears_only_the_per_iteration_budget(self):
        state = {
            "iterations": 5,
            "total_iterations": 5,
            "pipeline_run": "run1",
            "pipeline_iteration": 1,
        }
        self.assertEqual("iteration", MODULE.apply_pipeline_position(state, "run1", 2))
        self.assertEqual(0, state["iterations"])
        self.assertEqual(5, state["total_iterations"])
        self.assertEqual(2, state["pipeline_iteration"])

    def test_a_relaunch_inside_one_iteration_buys_nothing(self):
        state = {
            "iterations": 3,
            "total_iterations": 3,
            "pipeline_run": "run1",
            "pipeline_iteration": 2,
        }
        self.assertIsNone(MODULE.apply_pipeline_position(state, "run1", 2))
        self.assertEqual(3, state["iterations"])

    def test_replaying_an_earlier_iteration_buys_nothing(self):
        state = {
            "iterations": 3,
            "total_iterations": 3,
            "pipeline_run": "run1",
            "pipeline_iteration": 4,
        }
        self.assertIsNone(MODULE.apply_pipeline_position(state, "run1", 2))
        self.assertEqual(3, state["iterations"])
        self.assertEqual(4, state["pipeline_iteration"])

    def test_a_second_run_resets_even_though_it_counts_from_one_again(self):
        """A pipeline numbers its iterations from one, so this must not be ordered.

        Comparing iterations across runs would leave a pull request that reached
        iteration three permanently unable to reset, and the ceiling would then
        refuse every future run on it. A deadlock outlasts the false start it
        would have prevented, so run identity is compared for equality instead.
        """
        state = {"iterations": 0, "total_iterations": 0}
        MODULE.apply_pipeline_position(state, "run1", 1)
        MODULE.apply_pipeline_position(state, "run1", 2)
        MODULE.apply_pipeline_position(state, "run1", 3)
        state["iterations"] = 5
        state["total_iterations"] = 10
        self.assertEqual("run", MODULE.apply_pipeline_position(state, "run2", 1))
        self.assertEqual(0, state["iterations"])
        self.assertEqual(0, state["total_iterations"])

    def test_a_lone_half_of_the_position_is_ignored_rather_than_half_applied(self):
        """The agent file promises this, so the helper has to honour it.

        A caller naming only one half has not said where its loop stands. Acting
        on it would reset on a number with no run to scope it, which is the
        cross-run deadlock in the other direction.
        """
        for run, iteration in ((None, 2), ("run1", None)):
            with self.subTest(run=run, iteration=iteration):
                state = {"iterations": 4, "total_iterations": 9}
                self.assertIsNone(MODULE.apply_pipeline_position(state, run, iteration))
                self.assertIsNone(MODULE.absolute_iteration_cap(run, iteration, 5, 3))
                self.assertEqual(4, state["iterations"])
                self.assertEqual(9, state["total_iterations"])

    def test_an_omitted_outer_cap_falls_back_rather_than_disabling_the_ceiling(self):
        """Only the outer cap is optional, and omitting it must not remove the bound."""
        state = {"iterations": 4, "total_iterations": 9}
        self.assertEqual("run", MODULE.apply_pipeline_position(state, "run1", 1))
        self.assertEqual(
            5 * MODULE.DEFAULT_PIPELINE_MAX_ITERATIONS,
            MODULE.absolute_iteration_cap("run1", 1, 5, None),
        )

    def test_the_ceiling_is_derived_from_the_callers_own_cap(self):
        self.assertEqual(15, MODULE.absolute_iteration_cap("run1", 1, 5, 3))
        self.assertEqual(
            5 * MODULE.DEFAULT_PIPELINE_MAX_ITERATIONS,
            MODULE.absolute_iteration_cap("run1", 1, 5, None),
        )
        self.assertEqual(
            5 * MODULE.DEFAULT_PIPELINE_MAX_ITERATIONS,
            MODULE.absolute_iteration_cap("run1", 1, 5, 0),
        )

    def test_there_is_no_ceiling_without_a_pipeline(self):
        self.assertIsNone(MODULE.absolute_iteration_cap(None, None, 5, 3))
        self.assertIsNone(MODULE.absolute_iteration_cap("run1", None, 5, 3))

    def test_names_which_budget_ran_out(self):
        self.assertIsNone(MODULE.exhausted_budget({"iterations": 4}, 5, 10))
        self.assertEqual(
            "iteration", MODULE.exhausted_budget({"iterations": 5}, 5, 10)
        )
        self.assertEqual(
            "absolute",
            MODULE.exhausted_budget({"iterations": 0, "total_iterations": 10}, 5, 10),
        )
        self.assertEqual(
            "iteration", MODULE.exhausted_budget({"iterations": 5}, 5, None)
        )

    def test_the_running_total_survives_a_pipeline_iteration(self):
        """The ceiling only bounds anything if the per-iteration reset spares it."""
        state = {"iterations": 0, "total_iterations": 0}
        for iteration in (1, 2):
            MODULE.apply_pipeline_position(state, "run1", iteration)
            for _ in range(5):
                MODULE.charge_iteration(state, {})
        self.assertEqual(5, state["iterations"])
        self.assertEqual(10, state["total_iterations"])
        self.assertEqual("absolute", MODULE.exhausted_budget(state, 5, 10))



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

    def preflight(self, stack, *, status="", head="head1", state_path=None):
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
        for _ in range(MODULE.DEFAULT_MAX_ITERATIONS):
            with contextlib.ExitStack() as stack:
                self.preflight(stack, state_path=path)
            state = MODULE.load_state(path)
            MODULE.charge_iteration(state, state["run"])
            MODULE.save_state(path, state)
        with contextlib.ExitStack() as stack:
            payload = self.preflight(stack, state_path=path)
        self.assertEqual("max_iterations_reached", payload["result"])
        escalation = MODULE.load_state(path)["escalation"]
        self.assertEqual("max_iterations_reached", escalation["reason"])
        self.assertTrue(escalation["next_action"])

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


if __name__ == "__main__":
    unittest.main()
