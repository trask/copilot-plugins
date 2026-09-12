import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "pr_conflict_resolver.py"
AGENT = Path(__file__).parents[1] / "agents" / "pr-conflict-resolver.agent.md"
SPEC = importlib.util.spec_from_file_location("pr_conflict_resolver", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class WindowsSubprocessTest(unittest.TestCase):
    def windows_patches(self, completed):
        return (
            mock.patch.object(MODULE, "IS_WINDOWS", True),
            mock.patch.object(
                MODULE.subprocess,
                "CREATE_NO_WINDOW",
                0x08000000,
                create=True,
            ),
            mock.patch.object(MODULE.subprocess, "run", return_value=completed),
        )

    def test_run_hides_arbitrary_windows_console_processes(self):
        completed = MODULE.subprocess.CompletedProcess(["formatter"], 0, "", "")
        windows, no_window, subprocess_run = self.windows_patches(completed)
        with windows, no_window, subprocess_run as run:
            MODULE.run(["formatter"])

        self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)

    def test_git_bytes_hides_windows_console_processes(self):
        completed = MODULE.subprocess.CompletedProcess(["git"], 0, b"", b"")
        windows, no_window, subprocess_run = self.windows_patches(completed)
        with windows, no_window, subprocess_run as run:
            MODULE.git_bytes(Path("repo"), "show", "HEAD:file")

        self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)

    def test_run_leaves_non_windows_process_options_unchanged(self):
        completed = MODULE.subprocess.CompletedProcess(["formatter"], 0, "", "")
        with (
            mock.patch.object(MODULE, "IS_WINDOWS", False),
            mock.patch.object(
                MODULE.subprocess, "run", return_value=completed
            ) as subprocess_run,
        ):
            MODULE.run(["formatter"])

        self.assertNotIn("creationflags", subprocess_run.call_args.kwargs)


ALL_MERGE_METHODS = {
    "allow_merge_commit": True,
    "allow_squash_merge": True,
    "allow_rebase_merge": True,
}
REBASE_ONLY_MERGE_METHODS = {
    "allow_merge_commit": False,
    "allow_squash_merge": False,
    "allow_rebase_merge": True,
}
NO_RELATIONS = {"dependents": [], "stacked_on": None}


def native_stack_detection(default_branch="main", trunk="main", members=None):
    """A `stack_membership` result describing a healthy two-member native stack.

    The invoked pull request is #7 on `feature`, stacked on #19483 on `v143`,
    which is stacked on the trunk. Overriding `members` builds a different shape.
    """
    if members is None:
        members = [
            {
                "position": 0,
                "number": 19483,
                "head_branch": "v143",
                "base_branch": "main",
                "mergeable": "MERGEABLE",
                "head_sha": "aaa",
                "base_sha": "base1",
            },
            {
                "position": 1,
                "number": 7,
                "head_branch": "feature",
                "base_branch": "v143",
                "mergeable": "CONFLICTING",
                "head_sha": "head1",
                "base_sha": "old-a",
            },
        ]
    return {
        "default_branch": default_branch,
        "stack": {
            "id": "S_1",
            "number": 19578,
            "size": len(members),
            "trunk": trunk,
            "members": members,
        },
    }


def temporary_directory(test: unittest.TestCase) -> Path:
    """Make a temporary directory that survives read-only files on cleanup."""

    directory = Path(tempfile.mkdtemp()).resolve()

    def force_remove(function, path, _info):
        os.chmod(path, stat.S_IWRITE)
        function(path)

    test.addCleanup(shutil.rmtree, directory, ignore_errors=False, onerror=force_remove)
    return directory


class GitTestCase(unittest.TestCase):
    @staticmethod
    def git_in(root, *args, check=True, input_bytes=None):
        text_options = (
            {"text": True, "encoding": "utf-8"}
            if input_bytes is None
            else {"input": input_bytes}
        )
        process = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            **text_options,
        )
        stdout = (
            process.stdout
            if isinstance(process.stdout, str)
            else process.stdout.decode("utf-8")
        )
        stderr = (
            process.stderr
            if isinstance(process.stderr, str)
            else process.stderr.decode("utf-8")
        )
        if check and process.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed ({process.returncode}): "
                f"{stderr or stdout}"
            )
        return stdout.strip() if check else process

    @staticmethod
    def write_in(root, name, content):
        (root / name).write_text(content, encoding="utf-8", newline="\n")

    @classmethod
    def commit_in(cls, root, message):
        cls.git_in(root, "add", "--all")
        cls.git_in(root, "commit", "--no-gpg-sign", "--message", message)


def pr_metadata(**overrides):
    metadata = {
        "number": 7,
        "title": "Add a thing",
        "pr_url": "https://github.com/owner/repo/pull/7",
        "repo_name": "owner/repo",
        "upstream_owner": "owner",
        "upstream_repo": "repo",
        "state": "OPEN",
        "is_draft": False,
        "mergeable": "CONFLICTING",
        "merge_state_status": "DIRTY",
        "head_owner": "fork",
        "head_repo": "repo",
        "head_branch": "feature",
        "head_sha": "head1",
        "base_branch": "main",
        "base_sha": "base1",
        "commits": [{"sha": "head1", "message": "Add a thing"}],
    }
    metadata.update(overrides)
    return metadata


def conflict_record(path="app.py", **overrides):
    conflict = {
        "path": path,
        "code": "UU",
        "kind": "both modified",
        "binary": False,
        "deletion": False,
        "marker_regions": [],
        "marker_problems": [],
        "present_stages": ["ancestor", "base", "head"],
        "head_commits": [],
        "base_commits": [],
        "status": "conflicted",
        "rationale": None,
        "one_side": None,
    }
    conflict.update(overrides)
    return conflict


def attempt_record(**overrides):
    attempt = {
        "id": "pr-7-attempt-1",
        "status": "conflicted",
        "attempt_number": 1,
        "strategy": "merge",
        "strategy_reason": "a merge resolves the conflict without rewriting the branch",
        "strategy_warnings": [],
        "head_sha": "head1",
        "base_sha": "base1",
        "merge_base": "merge0",
        "original_subjects": ["Add a thing"],
        "mergeable": "CONFLICTING",
        "merge_state_status": "DIRTY",
        "started_at": "2026-01-01T00:00:00Z",
        "conflicts": [conflict_record()],
        "conflict_signature": MODULE.conflict_signature(["app.py"]),
        "published_head_sha": None,
        "mergeable_at_head_sha": None,
    }
    attempt.update(overrides)
    return attempt


def write_state(directory: Path, **overrides) -> Path:
    state = {
        "version": MODULE.STATE_VERSION,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "attempts": 0,
        "history": [],
        "escalation": None,
        "repo_root": str(directory),
        "pr": pr_metadata(),
        "relations": dict(NO_RELATIONS),
        "merge_methods": dict(ALL_MERGE_METHODS),
        "attempt": attempt_record(),
    }
    state.update(overrides)
    path = directory / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def emitted(emit_mock):
    return emit_mock.call_args[0][0]


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


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

    def test_the_no_target_path_is_not_offered_to_a_detached_worktree(self):
        """The run detaches, so a step telling a caller to omit the target traps them.

        `preflight` and `status --current` both resolve the pull request from the
        checked-out branch, and a detached worktree names none.
        """
        self.assertIn(
            "A detached worktree names no branch to look up", self.instructions
        )
        self.assertIn(
            "ask the user which pull request to resolve and pass it explicitly",
            self.instructions,
        )
        self.assertIn("`--current` reads the branch this worktree has checked out", self.instructions)

    def test_declares_the_frontmatter_keys_the_sibling_loops_use(self):
        self.assertIn("name: PR Conflict Resolver", self.instructions)
        self.assertIn(
            "another user-selected maintenance agent explicitly dispatches it",
            self.instructions,
        )
        self.assertIn(
            "argument-hint: \"PR URL, PR number, or owner/repo#number; omit only "
            "from a worktree attached to the PR's branch\"",
            self.instructions,
        )
        self.assertIn(
            "tools: [read, edit, search, execute, todo, rename_session]",
            self.instructions,
        )
        self.assertIn("user-invocable: true", self.instructions)
        self.assertIn("disable-model-invocation: true", self.instructions)
        self.assertIn(
            "Never select or start this agent automatically.",
            self.instructions,
        )

    def test_the_target_help_carries_the_argument_hint_condition(self):
        """Derived, not copied: drift in either surface fails here.

        A literal only catches the change you thought of. Reading the clause out
        of the agent file catches the agent file drifting too.
        """

        hint = next(
            line for line in self.instructions.splitlines()
            if line.startswith("argument-hint:")
        )
        clause = hint.strip().rstrip('"').rsplit("; ", 1)[1]
        self.assertEqual(
            "omit only from a worktree attached to the PR's branch", clause
        )
        parser = MODULE.build_parser()
        subparsers = [
            action
            for action in parser._actions
            if isinstance(action, MODULE.argparse._SubParsersAction)
        ][0]
        targets = [
            action
            for sub in subparsers.choices.values()
            for action in sub._actions
            if action.dest == "target" and not action.option_strings
        ]
        self.assertTrue(targets, "the helper must take a target somewhere")
        for action in targets:
            with self.subTest(help=action.help):
                self.assertTrue(
                    action.help.endswith(clause),
                    f"{action.help!r} does not end with {clause!r}",
                )

    def test_a_bare_number_is_refused_by_this_parser(self):
        # The agent file may offer a bare number because the agent combines it
        # with the workspace's repository first. This parser takes no repository,
        # so its own help must not offer a form it refuses.
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.parse_target("19517")

    def test_does_not_invent_a_model_frontmatter_key(self):
        frontmatter = self.instructions.split("---")[1]
        self.assertNotIn("model:", frontmatter)

    def test_bare_pr_reference_starts_the_run(self):
        self.assertIn(
            "## Activation: Bare PR References Start The Run", self.instructions
        )
        self.assertIn("asks you to run PR Conflict Resolver on it", self.instructions)
        self.assertIn("Start the helper's `preflight` workflow at once", self.instructions)
        self.assertIn(
            "Never hand the work to a generic rebase or merge skill", self.instructions
        )

    def test_every_invocation_starts_a_fresh_attempt_without_a_token_or_a_cap(self):
        """Without this a repeat selection looks spent rather than fresh.

        The one-shot contract only holds when the user can select the agent
        again and get a whole new run.
        """
        self.assertIn("Every explicit invocation starts a fresh attempt", self.instructions)
        self.assertIn(
            "There is no token to carry and no cap to spend", self.instructions
        )

    def test_names_the_session_from_preflight_metadata_idempotently(self):
        self.assertIn("## Session Naming", self.instructions)
        self.assertIn(
            "ensure the session name is `PR Conflict Resolver: <PR number> - <PR title>`",
            self.instructions,
        )
        self.assertIn(
            "If the harness has already supplied a name beginning "
            "`PR Conflict Resolver: <PR number> - `",
            self.instructions,
        )
        self.assertIn("do not call `rename_session`", self.instructions)
        self.assertIn("Otherwise call `rename_session` once", self.instructions)
        self.assertIn("Never use an interim number-only name", self.instructions)

    def test_never_posts_anything_to_github(self):
        self.assertIn("You never post anything to GitHub", self.instructions)
        self.assertIn(
            "Never post an issue comment, a pull request comment, a review, a review "
            "comment, a reply, or a discussion post.",
            self.instructions,
        )
        self.assertIn("Never resolve a review thread.", self.instructions)
        self.assertIn(
            "Never edit the pull request title, description, labels, reviewers, or "
            "draft state.",
            self.instructions,
        )
        self.assertIn(
            "Pushing commits to the head branch is the only write this agent performs",
            self.instructions,
        )

    def test_requires_keeping_what_both_sides_meant_to_do(self):
        self.assertIn("Keep what both sides meant to do.", self.instructions)
        self.assertIn(
            "Never just pick one side because it is easier", self.instructions
        )
        self.assertIn("## Reading The Conflict", self.instructions)
        self.assertIn("`head_commits`", self.instructions)
        self.assertIn("`base_commits`", self.instructions)
        self.assertIn(
            "Git's own `ours` and `theirs` swap meaning between a merge and a rebase",
            self.instructions,
        )
        self.assertIn("git show <sha>", self.instructions)

    def test_documents_recoverable_helper_interruptions_and_stack_fallbacks(self):
        self.assertIn(
            "records `integrating` before it starts git", self.instructions
        )
        self.assertIn(
            "`HEAD` and `MERGE_HEAD` match the frozen attempt",
            self.instructions,
        )
        self.assertIn(
            "`orig-head` and `onto` match",
            self.instructions,
        )
        self.assertIn(
            "complete ordered range above it equals GitHub's complete recorded "
            "commit list",
            self.instructions,
        )
        self.assertIn(
            "complete frozen predecessor and child commit lists",
            self.instructions,
        )
        self.assertIn("reports `single_branch_fallback`", self.instructions)
        self.assertIn(
            "If it reports `single_branch_fallback`, run `attempt` with the same "
            "state.",
            self.instructions,
        )

    def test_scans_all_replayed_paths_for_api_migrations(self):
        self.assertIn("### Checking API migrations", self.instructions)
        self.assertIn(
            "Treat an interface, type, or method rename as a change to the whole "
            "replay commit",
            self.instructions,
        )
        self.assertIn(
            "git diff-tree --no-commit-id --name-only -r --root REBASE_HEAD",
            self.instructions,
        )
        self.assertIn(
            "Search those paths and the conflicted files for both the old and new "
            "symbol names",
            self.instructions,
        )
        self.assertIn(
            "A replayed file that had no textual conflict still needs this check",
            self.instructions,
        )

    def test_escalates_on_a_genuine_contradiction(self):
        self.assertIn("## Escalating On A Contradiction", self.instructions)
        self.assertIn(
            "Two sides contradict each other when both cannot hold at the same time",
            self.instructions,
        )
        self.assertIn("escalate --kind contradiction", self.instructions)
        self.assertIn(
            "Escalate only when combining both sides is impossible, not when it is work",
            self.instructions,
        )
        self.assertIn(
            "This agent runs unattended, so a guess is worse than a stop",
            self.instructions,
        )

    def test_states_that_one_run_integrates_once(self):
        """A cap would only matter to an outer loop, and there is none.

        The run has to end after a single integration whatever the last
        mergeability answer says.
        """
        self.assertIn("One run integrates once", self.instructions)
        self.assertIn(
            "you are finished, even when the pull request is still conflicting",
            self.instructions,
        )
        self.assertIn(
            "Never start a second `preflight` in the same run", self.instructions
        )
        self.assertNotIn("--max-iterations", self.instructions)
        self.assertNotIn("iteration", self.instructions.lower())

    def test_guards_the_push_and_the_stack(self):
        self.assertIn("Never push to the base branch", self.instructions)
        self.assertIn(
            "Never push to any branch other than the pull request's own head branch",
            self.instructions,
        )
        self.assertIn(
            "Never rewrite a branch that another open pull request stacks on",
            self.instructions,
        )
        self.assertIn(
            "prove the base branch and every dependent pull request did not move",
            self.instructions,
        )
        self.assertIn(
            "Never run `git merge`, `git rebase`, `git push`, `git add`, `git commit`, "
            "`git reset`, or `git checkout` yourself",
            self.instructions,
        )

    def test_documents_every_helper_subcommand(self):
        for command in (
            "preflight",
            "attempt",
            "resolved",
            "continue",
            "abort",
            "escalate",
            "publish",
            "status",
            "cleanup",
        ):
            with self.subTest(command=command):
                self.assertIn(f"- `{command}", self.instructions)

    def test_documents_the_native_stack_subcommands(self):
        for command in (
            "stack-rebase",
            "stack-continue",
            "stack-format",
            "stack-abort",
            "stack-validation-fix",
            "stack-publish",
        ):
            with self.subTest(command=command):
                self.assertIn(f"- `{command}", self.instructions)

    def test_documents_every_preflight_result(self):
        for result in (
            "ready",
            "mergeable",
            "unknown_mergeability",
            "unsafe_push",
            "no_safe_strategy",
            "stack_rebase",
            "stack_external_dependents",
            "ad_hoc_base",
        ):
            with self.subTest(result=result):
                self.assertIn(f"`{result}`", self.instructions)

    def test_refuses_a_cascade_that_would_orphan_an_external_dependent(self):
        self.assertIn(
            "an open pull request outside the stack is based on a branch the "
            "cascade would force-push",
            self.instructions,
        )
        self.assertIn(
            "names each such pull request and the branch it targets",
            self.instructions,
        )
        self.assertIn(
            "The trunk is not rewritten, so pull requests targeting the trunk "
            "are not dependents",
            self.instructions,
        )

    def test_documents_the_atomic_stack_publish(self):
        self.assertIn(
            "one atomic git push with an exact expected-head lease for each branch",
            self.instructions,
        )
        self.assertIn("durable `published_refs` checkpoint", self.instructions)
        self.assertIn(
            "The remote accepts every member update or none",
            self.instructions,
        )
        self.assertIn(
            "keeps a self-contained throwaway workspace for inspection",
            self.instructions,
        )
        self.assertIn(
            "requires a new preflight before another publish",
            self.instructions,
        )

    def test_explains_the_native_stack_cascade(self):
        self.assertIn("## Native GitHub Stacks", self.instructions)
        self.assertIn(
            "Detection is the API's `pullRequest.stack`, never the branch name",
            self.instructions,
        )
        self.assertIn(
            "the cascade runs in a throwaway clone", self.instructions
        )
        self.assertIn(
            "compares native order with direct PR bases",
            self.instructions,
        )
        self.assertIn(
            "split through GitHub's stacks API along those direct-base chains",
            self.instructions,
        )
        self.assertIn(
            "Do not run `stack-abort` for a topology repair failure",
            self.instructions,
        )
        self.assertIn(
            "force-pushes every member of the stack, including ones that are "
            "currently mergeable and under review",
            self.instructions,
        )
        self.assertIn(
            "The helper never runs formatting between individual conflict stops "
            "in one member",
            self.instructions,
        )
        self.assertIn(
            "Never widen the single-branch push guards to let a cascade through",
            self.instructions,
        )

    def test_names_the_conflict_on_an_ad_hoc_base_rather_than_guessing(self):
        self.assertIn(
            "targets a branch that is neither the repository default branch nor a "
            "native stack trunk",
            self.instructions,
        )
        self.assertIn(
            "names the branch and file that actually conflict", self.instructions
        )
        self.assertIn(
            "do not rebase onto the declared base", self.instructions
        )

    def test_reads_mergeability_live_and_never_claims_it_otherwise(self):
        self.assertIn(
            "read mergeability live from GitHub and wait out an `UNKNOWN` answer",
            self.instructions,
        )
        self.assertIn(
            "Never claim the pull request is mergeable unless the helper read that "
            "live from GitHub",
            self.instructions,
        )

    def test_explains_the_strategy_choice(self):
        self.assertIn("## Strategy", self.instructions)
        self.assertIn("It rewrites nothing", self.instructions)
        self.assertIn("It rewrites the branch", self.instructions)
        self.assertIn(
            "refuses it outright when another open pull request stacks on this branch",
            self.instructions,
        )

    def test_lists_the_stop_conditions_and_the_final_report(self):
        self.assertIn("## Stop Conditions", self.instructions)
        self.assertIn("## Final Report", self.instructions)
        self.assertIn("Send one message that calls no tool", self.instructions)

    def test_an_escalated_run_cannot_read_like_an_uneventful_one(self):
        self.assertIn("never let an escalated run read like an uneventful one", self.instructions)
        self.assertIn("names the escalation kind", self.instructions)
        self.assertIn("`requires_user_decision`", self.instructions)
        self.assertIn("does not claim that a person has to choose", self.instructions)
        self.assertIn("still conflicted and the branch untouched", self.instructions)

    def test_the_report_agrees_with_the_machine_readable_outcome(self):
        self.assertIn("run `status` and read its `stage_outcome`", self.instructions)
        for word in ("`cleared`", "`skipped`", "`completed`", "`escalated`"):
            self.assertIn(word, self.instructions)
        self.assertIn("it never says the stage is green", self.instructions)

    def test_a_published_run_that_is_still_conflicting_is_completed(self):
        """Calling that outcome a failure invites a retry the contract forbids.

        The run pushed its resolution, so it did its whole job even when
        GitHub still reads the pull request as conflicting.
        """
        self.assertIn(
            "this run published its resolution and GitHub then read the pull request "
            "as conflicting or would not say",
            self.instructions,
        )
        self.assertIn("The run did its whole job", self.instructions)
        self.assertIn(
            "Do not run `preflight` again, do not escalate, and do not claim the pull "
            "request is mergeable",
            self.instructions,
        )
        self.assertIn(
            "A conflicting or unknown answer after a successful publish is a completed "
            "run, not a failure",
            self.instructions,
        )

    def test_a_single_integration_may_still_take_many_continue_steps(self):
        """One pass means one integration, not one helper call.

        A run that read the cascade as spent after its first `stack-continue`
        would abandon the members above it.
        """
        self.assertIn(
            "a cascade that climbs from one stack member to the next, is still one "
            "integration",
            self.instructions,
        )
        self.assertIn(
            "Run `continue` or `stack-continue` as many times as that single "
            "integration needs",
            self.instructions,
        )

    def test_an_absent_outcome_is_not_read_as_a_failed_run(self):
        self.assertIn("the field is absent", self.instructions)
        self.assertIn("there is no run to describe", self.instructions)
        self.assertIn("reaching for one of the four words", self.instructions)

    def test_an_unfinished_run_is_finished_rather_than_guessed_at(self):
        self.assertIn("absent while a run is still going", self.instructions)
        self.assertIn("no command recorded an ending", self.instructions)

    def test_closes_every_run_with_a_tagged_retrospective(self):
        self.assertIn("## PR Conflict Resolver Agent Retrospective", self.instructions)
        for category in (
            "**Agent**",
            "**Helper**",
            "**General instructions**",
            "**Repository**",
        ):
            with self.subTest(category=category):
                self.assertIn(category, self.instructions)
        self.assertIn("belongs in chat only", self.instructions)
        self.assertIn("Silence is the normal outcome", self.instructions)


class TargetParsingTest(unittest.TestCase):
    def test_accepts_a_pull_request_url(self):
        target = MODULE.parse_target("https://github.com/owner/repo/pull/12")
        self.assertEqual(
            {
                "owner": "owner",
                "repo": "repo",
                "number": 12,
                "repo_name": "owner/repo",
                "pr_url": "https://github.com/owner/repo/pull/12",
            },
            target,
        )

    def test_accepts_a_url_with_a_trailing_slash_or_fragment(self):
        for value in (
            "https://github.com/owner/repo/pull/12/",
            "https://github.com/owner/repo/pull/12#issuecomment-1",
        ):
            with self.subTest(value=value):
                self.assertEqual(12, MODULE.parse_target(value)["number"])

    def test_accepts_the_short_form(self):
        target = MODULE.parse_target("owner/repo#3")
        self.assertEqual("https://github.com/owner/repo/pull/3", target["pr_url"])

    def test_rejects_anything_else(self):
        for value in (
            "12",
            "#12",
            "owner/repo",
            "https://github.com/owner/repo/issues/12",
            "https://example.com/owner/repo/pull/12",
            "owner/repo#",
        ):
            with self.subTest(value=value):
                with self.assertRaises(MODULE.WorkflowError):
                    MODULE.parse_target(value)

    def test_state_path_encodes_the_target(self):
        path = MODULE.default_state_path(MODULE.parse_target("owner/repo#9"))
        self.assertEqual("owner--repo--9.json", path.name)
        self.assertEqual("pr-conflict-resolver", path.parent.name)
        self.assertEqual("run", path.parent.parent.name)

    def test_sidecar_paths_sit_beside_the_state_file(self):
        state_path = Path("/tmp/owner--repo--9.json")
        self.assertEqual(
            "owner--repo--9.json.preflight.json",
            MODULE.preflight_path_for(state_path).name,
        )
        self.assertEqual(
            "owner--repo--9.json.conflicts.json",
            MODULE.conflicts_path_for(state_path).name,
        )
        self.assertEqual(
            "owner--repo--9.json.status.json", MODULE.status_path_for(state_path).name
        )

    def test_resolve_target_uses_the_current_branch_when_no_value_is_given(self):
        with mock.patch.object(
            MODULE, "current_pr_target", return_value={"number": 4}
        ) as current:
            self.assertEqual({"number": 4}, MODULE.resolve_target(None, Path(".")))
        current.assert_called_once()

    def test_normalize_cli_path_only_rewrites_git_bash_paths_on_windows(self):
        self.assertEqual("C:/work/repo", MODULE.normalize_cli_path("/c/work/repo", windows=True))
        self.assertEqual("C:/", MODULE.normalize_cli_path("/c", windows=True))
        self.assertEqual("/c/work/repo", MODULE.normalize_cli_path("/c/work/repo", windows=False))
        self.assertEqual("/usr/local", MODULE.normalize_cli_path("/usr/local", windows=True))


class RemoteUrlTest(unittest.TestCase):
    def test_recognizes_every_github_remote_shape(self):
        for url in (
            "https://github.com/owner/repo.git",
            "https://github.com/owner/repo",
            "git@github.com:owner/repo.git",
            "ssh://git@github.com/owner/repo.git",
            "ssh://git@github.com:443/owner/repo",
            "https://GITHUB.com/owner/repo/",
        ):
            with self.subTest(url=url):
                self.assertEqual("owner/repo", MODULE.github_repo_from_remote(url))

    def test_rejects_other_hosts(self):
        for url in (
            "https://gitlab.com/owner/repo.git",
            "git@example.com:owner/repo.git",
            "not a url",
        ):
            with self.subTest(url=url):
                self.assertIsNone(MODULE.github_repo_from_remote(url))


class ConfiguredUpstreamTest(unittest.TestCase):
    def configure(self, remote, merge, url="https://github.com/owner/repo.git"):
        def fake_git_try(_root, *arguments):
            if arguments[-1].endswith(".remote"):
                return remote
            return merge

        return fake_git_try, url

    def test_returns_none_when_the_branch_tracks_nothing(self):
        fake_git_try, _ = self.configure(completed(1), completed(1))
        with mock.patch.object(MODULE, "git_try", side_effect=fake_git_try):
            self.assertIsNone(MODULE.configured_upstream(Path("."), "feature"))

    def test_rejects_a_half_configured_upstream(self):
        fake_git_try, _ = self.configure(completed(0, "origin\n"), completed(1))
        with mock.patch.object(MODULE, "git_try", side_effect=fake_git_try):
            with self.assertRaisesRegex(MODULE.WorkflowError, "incomplete upstream"):
                MODULE.configured_upstream(Path("."), "feature")

    def test_rejects_a_local_tracking_remote(self):
        fake_git_try, _ = self.configure(
            completed(0, ".\n"), completed(0, "refs/heads/feature\n")
        )
        with mock.patch.object(MODULE, "git_try", side_effect=fake_git_try):
            with self.assertRaisesRegex(MODULE.WorkflowError, "GitHub remote branch"):
                MODULE.configured_upstream(Path("."), "feature")

    def test_rejects_an_unsupported_merge_ref(self):
        fake_git_try, _ = self.configure(
            completed(0, "origin\n"), completed(0, "refs/tags/v1\n")
        )
        with mock.patch.object(MODULE, "git_try", side_effect=fake_git_try):
            with self.assertRaisesRegex(MODULE.WorkflowError, "unsupported upstream"):
                MODULE.configured_upstream(Path("."), "feature")

    def test_rejects_a_remote_that_is_not_github(self):
        fake_git_try, _ = self.configure(
            completed(0, "origin\n"), completed(0, "refs/heads/feature\n")
        )
        with mock.patch.object(MODULE, "git_try", side_effect=fake_git_try), mock.patch.object(
            MODULE, "git", return_value="https://gitlab.com/owner/repo.git"
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "supported GitHub URL"):
                MODULE.configured_upstream(Path("."), "feature")

    def test_returns_the_remote_repository_and_branch(self):
        fake_git_try, url = self.configure(
            completed(0, "origin\n"), completed(0, "refs/heads/trunk\n")
        )
        with mock.patch.object(MODULE, "git_try", side_effect=fake_git_try), mock.patch.object(
            MODULE, "git", return_value=url
        ):
            self.assertEqual(
                {"remote": "origin", "repo": "owner/repo", "branch": "trunk"},
                MODULE.configured_upstream(Path("."), "feature"),
            )


class CurrentPullRequestTest(unittest.TestCase):
    def test_payload_without_a_url_is_rejected(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "pull request URL"):
            MODULE.pr_target_from_payload({"state": "OPEN"})

    def test_closed_payload_is_ignored(self):
        payload = {"url": "https://github.com/owner/repo/pull/2", "state": "CLOSED"}
        self.assertIsNone(MODULE.pr_target_from_payload(payload))

    def test_payload_must_match_the_expected_upstream(self):
        payload = {
            "url": "https://github.com/owner/repo/pull/2",
            "state": "OPEN",
            "headRefName": "feature",
            "headRepositoryOwner": {"login": "fork"},
            "headRepository": {"name": "repo"},
        }
        self.assertIsNone(
            MODULE.pr_target_from_payload(
                payload, {"repo": "owner/repo", "branch": "feature"}
            )
        )
        self.assertEqual(
            2,
            MODULE.pr_target_from_payload(
                payload, {"repo": "Fork/Repo", "branch": "feature"}
            )["number"],
        )

    def test_detached_head_cannot_resolve_a_pull_request(self):
        with mock.patch.object(MODULE, "git", return_value=""):
            with self.assertRaisesRegex(MODULE.WorkflowError, "detached HEAD"):
                MODULE.current_pr_target(Path("."))

    def test_the_refusal_names_the_way_out(self):
        with mock.patch.object(MODULE, "git", return_value=""):
            with self.assertRaises(MODULE.WorkflowError) as refusal:
                MODULE.current_pr_target(Path("."))
        self.assertIn("pass the pull request explicitly", str(refusal.exception))

    def test_no_upstream_and_no_pull_request_reports_the_branch(self):
        with mock.patch.object(MODULE, "git", return_value="feature"), mock.patch.object(
            MODULE, "configured_upstream", return_value=None
        ), mock.patch.object(MODULE, "simple_current_pr_target", return_value=None):
            with self.assertRaisesRegex(MODULE.WorkflowError, "no configured upstream"):
                MODULE.current_pr_target(Path("."))

    def test_no_upstream_but_a_pull_request_is_accepted(self):
        target = MODULE.parse_target("owner/repo#5")
        with mock.patch.object(MODULE, "git", return_value="feature"), mock.patch.object(
            MODULE, "configured_upstream", return_value=None
        ), mock.patch.object(MODULE, "simple_current_pr_target", return_value=target):
            self.assertEqual(target, MODULE.current_pr_target(Path(".")))

    def test_upstream_with_no_pull_request_is_reported(self):
        upstream = {"remote": "origin", "repo": "owner/repo", "branch": "feature"}
        with mock.patch.object(MODULE, "git", return_value="feature"), mock.patch.object(
            MODULE, "configured_upstream", return_value=upstream
        ), mock.patch.object(
            MODULE, "simple_current_pr_target", return_value=None
        ), mock.patch.object(MODULE, "exact_upstream_pr_targets", return_value=[]):
            with self.assertRaisesRegex(MODULE.WorkflowError, "no open pull request"):
                MODULE.current_pr_target(Path("."))

    def test_two_pull_requests_for_one_upstream_are_reported(self):
        upstream = {"remote": "origin", "repo": "owner/repo", "branch": "feature"}
        targets = [MODULE.parse_target("owner/repo#1"), MODULE.parse_target("owner/repo#2")]
        with mock.patch.object(MODULE, "git", return_value="feature"), mock.patch.object(
            MODULE, "configured_upstream", return_value=upstream
        ), mock.patch.object(
            MODULE, "simple_current_pr_target", return_value=None
        ), mock.patch.object(MODULE, "exact_upstream_pr_targets", return_value=targets):
            with self.assertRaisesRegex(MODULE.WorkflowError, "multiple open pull requests"):
                MODULE.current_pr_target(Path("."))

    def test_exact_upstream_targets_filter_and_paginate(self):
        pages = [
            {
                "data": {
                    "repository": {
                        "ref": {
                            "target": {
                                "associatedPullRequests": {
                                    "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                                    "nodes": [
                                        {
                                            "url": "https://github.com/owner/repo/pull/1",
                                            "state": "OPEN",
                                            "headRefName": "feature",
                                            "headRepository": {
                                                "nameWithOwner": "Owner/Repo"
                                            },
                                        },
                                        {
                                            "url": "https://github.com/owner/repo/pull/2",
                                            "state": "CLOSED",
                                            "headRefName": "feature",
                                            "headRepository": {
                                                "nameWithOwner": "owner/repo"
                                            },
                                        },
                                    ],
                                }
                            }
                        }
                    }
                }
            },
            {
                "data": {
                    "repository": {
                        "ref": {
                            "target": {
                                "associatedPullRequests": {
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": [
                                        {
                                            "url": "https://github.com/owner/repo/pull/1",
                                            "state": "OPEN",
                                            "headRefName": "feature",
                                            "headRepository": {
                                                "nameWithOwner": "owner/repo"
                                            },
                                        },
                                        {
                                            "url": "https://github.com/owner/repo/pull/3",
                                            "state": "OPEN",
                                            "headRefName": "other",
                                            "headRepository": {
                                                "nameWithOwner": "owner/repo"
                                            },
                                        },
                                    ],
                                }
                            }
                        }
                    }
                }
            },
        ]
        with mock.patch.object(MODULE, "graphql", side_effect=pages) as graphql:
            targets = MODULE.exact_upstream_pr_targets(
                {"repo": "owner/repo", "branch": "feature"}
            )
        self.assertEqual([1], [target["number"] for target in targets])
        self.assertEqual(2, graphql.call_count)
        self.assertEqual("c1", graphql.call_args[0][1]["after"])

    def test_missing_association_connection_returns_nothing(self):
        payload = {"data": {"repository": {"ref": {"target": {}}}}}
        with mock.patch.object(MODULE, "graphql", return_value=payload):
            self.assertEqual(
                [],
                MODULE.exact_upstream_pr_targets(
                    {"repo": "owner/repo", "branch": "feature"}
                ),
            )


def gh_metadata(**overrides):
    payload = {
        "number": 7,
        "title": "Add a thing",
        "url": "https://github.com/owner/repo/pull/7",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "CONFLICTING",
        "mergeStateStatus": "DIRTY",
        "headRefName": "feature",
        "headRefOid": "head1",
        "headRepositoryOwner": {"login": "fork"},
        "headRepository": {"name": "repo"},
        "baseRefName": "main",
        "baseRefOid": "base1",
        "commits": [{"oid": "head1", "messageHeadline": "Add a thing "}],
    }
    payload.update(overrides)
    return payload


class PullRequestMetadataTest(unittest.TestCase):
    def metadata(self, *, base_tip="live-tip", **overrides):
        target = MODULE.parse_target("owner/repo#7")
        with mock.patch.object(
            MODULE, "gh_json", return_value=gh_metadata(**overrides)
        ), mock.patch.object(MODULE, "base_ref_tip", return_value=base_tip):
            return MODULE.metadata_for(target)

    def test_normalizes_the_fields_the_resolver_uses(self):
        metadata = self.metadata()
        self.assertEqual("owner/repo", metadata["repo_name"])
        self.assertEqual("owner", metadata["upstream_owner"])
        self.assertEqual("fork", metadata["head_owner"])
        self.assertEqual("repo", metadata["head_repo"])
        self.assertEqual("feature", metadata["head_branch"])
        self.assertEqual("head1", metadata["head_sha"])
        self.assertEqual("main", metadata["base_branch"])
        self.assertEqual("live-tip", metadata["base_sha"])
        self.assertEqual("CONFLICTING", metadata["mergeable"])
        self.assertEqual("DIRTY", metadata["merge_state_status"])
        self.assertEqual([{"sha": "head1", "message": "Add a thing"}], metadata["commits"])

    def test_base_sha_is_the_live_base_branch_tip_not_the_frozen_base_ref_oid(self):
        target = MODULE.parse_target("owner/repo#7")
        with mock.patch.object(
            MODULE, "gh_json", return_value=gh_metadata(baseRefOid="frozen")
        ), mock.patch.object(
            MODULE, "base_ref_tip", return_value="live-tip"
        ) as tip:
            metadata = MODULE.metadata_for(target)
        self.assertEqual("live-tip", metadata["base_sha"])
        tip.assert_called_once_with("owner/repo", "main")

    def test_rejects_a_non_object_response(self):
        target = MODULE.parse_target("owner/repo#7")
        with mock.patch.object(MODULE, "gh_json", return_value=[]):
            with self.assertRaisesRegex(MODULE.WorkflowError, "did not return PR metadata"):
                MODULE.metadata_for(target)

    def test_rejects_metadata_for_another_pull_request(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "does not match the requested"):
            self.metadata(number=8)

    def test_rejects_a_deleted_head_repository(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "head repository is unavailable"):
            self.metadata(headRepository=None)

    def test_rejects_missing_commit_details(self):
        for overrides, message in (
            ({"headRefOid": ""}, "no head commit"),
            ({"baseRefName": None}, "no base branch"),
            ({"title": "  "}, "no title"),
            ({"commits": None}, "no commit list"),
            ({"commits": ["nope"]}, "is not an object"),
            ({"commits": [{"messageHeadline": "x"}]}, "has no OID"),
            ({"commits": [{"oid": "a"}]}, "no message headline"),
            ({"url": None}, "no URL"),
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(MODULE.WorkflowError, message):
                    self.metadata(**overrides)

    def test_requires_an_open_pull_request(self):
        MODULE.require_open_pull_request(pr_metadata())
        with self.assertRaisesRegex(MODULE.WorkflowError, "is closed"):
            MODULE.require_open_pull_request(pr_metadata(state="CLOSED"))


class BaseRefTipTest(unittest.TestCase):
    def test_returns_the_live_tip_from_the_branch_ref(self):
        response = completed(
            0, json.dumps({"object": {"sha": "live-tip", "type": "commit"}})
        )
        with mock.patch.object(MODULE, "run", return_value=response) as run:
            self.assertEqual("live-tip", MODULE.base_ref_tip("owner/repo", "main"))
        self.assertEqual(
            ["gh", "api", "repos/owner/repo/git/ref/heads/main"],
            run.call_args.args[0],
        )

    def test_a_deleted_base_branch_raises_rather_than_falling_back(self):
        response = completed(1, "", "gh: Not Found (HTTP 404)")
        with mock.patch.object(MODULE, "run", return_value=response):
            with self.assertRaisesRegex(MODULE.WorkflowError, "may have been deleted"):
                MODULE.base_ref_tip("owner/repo", "gone")


class MergeabilityTest(unittest.TestCase):
    def test_classification_maps_the_three_answers(self):
        self.assertEqual("mergeable", MODULE.classify_mergeability({"mergeable": "MERGEABLE"}))
        self.assertEqual(
            "conflicting", MODULE.classify_mergeability({"mergeable": "CONFLICTING"})
        )
        self.assertEqual("unknown", MODULE.classify_mergeability({"mergeable": "UNKNOWN"}))
        self.assertEqual("unknown", MODULE.classify_mergeability({}))

    def test_a_settled_answer_is_read_once(self):
        target = MODULE.parse_target("owner/repo#7")
        with mock.patch.object(
            MODULE, "metadata_for", return_value=pr_metadata()
        ) as metadata_for, mock.patch.object(MODULE, "time") as clock:
            result = MODULE.live_mergeability(target)
        self.assertEqual("CONFLICTING", result["mergeable"])
        self.assertEqual(1, metadata_for.call_count)
        clock.sleep.assert_not_called()

    def test_an_unknown_answer_is_read_again_until_it_settles(self):
        target = MODULE.parse_target("owner/repo#7")
        answers = [
            pr_metadata(mergeable="UNKNOWN"),
            pr_metadata(mergeable="UNKNOWN"),
            pr_metadata(mergeable="MERGEABLE"),
        ]
        with mock.patch.object(
            MODULE, "metadata_for", side_effect=answers
        ) as metadata_for, mock.patch.object(MODULE, "time") as clock:
            result = MODULE.live_mergeability(target, delays=(0.1, 0.2, 0.3))
        self.assertEqual("MERGEABLE", result["mergeable"])
        self.assertEqual(3, metadata_for.call_count)
        self.assertEqual([mock.call(0.1), mock.call(0.2)], clock.sleep.call_args_list)

    def test_an_answer_that_never_settles_is_returned_as_unknown(self):
        target = MODULE.parse_target("owner/repo#7")
        with mock.patch.object(
            MODULE, "metadata_for", return_value=pr_metadata(mergeable="UNKNOWN")
        ) as metadata_for, mock.patch.object(MODULE, "time"):
            result = MODULE.live_mergeability(target, delays=(0, 0))
        self.assertEqual("unknown", MODULE.classify_mergeability(result))
        self.assertEqual(3, metadata_for.call_count)

    def test_a_settled_answer_needs_no_expected_head(self):
        self.assertTrue(MODULE.mergeability_settled(pr_metadata(mergeable="MERGEABLE")))
        self.assertFalse(MODULE.mergeability_settled(pr_metadata(mergeable="UNKNOWN")))

    def test_an_answer_describing_another_head_has_not_settled(self):
        metadata = pr_metadata(head_sha="old1", mergeable="CONFLICTING")
        self.assertFalse(MODULE.mergeability_settled(metadata, "new1"))
        self.assertTrue(MODULE.mergeability_settled(metadata, "old1"))

    def test_an_answer_for_the_previous_head_is_read_again(self):
        target = MODULE.parse_target("owner/repo#7")
        answers = [
            pr_metadata(head_sha="old1", mergeable="CONFLICTING"),
            pr_metadata(head_sha="new1", mergeable="UNKNOWN"),
            pr_metadata(head_sha="new1", mergeable="MERGEABLE"),
        ]
        with mock.patch.object(
            MODULE, "metadata_for", side_effect=answers
        ) as metadata_for, mock.patch.object(MODULE, "time"):
            result = MODULE.live_mergeability(
                target, delays=(0, 0, 0), expected_head="new1"
            )
        self.assertEqual("MERGEABLE", result["mergeable"])
        self.assertEqual("new1", result["head_sha"])
        self.assertEqual(3, metadata_for.call_count)

    def test_a_stale_answer_is_classified_unknown_rather_than_believed(self):
        stale = pr_metadata(head_sha="old1", mergeable="MERGEABLE")
        self.assertEqual("mergeable", MODULE.classify_mergeability(stale))
        self.assertEqual(
            "unknown", MODULE.classify_mergeability(stale, expected_head="new1")
        )
        self.assertEqual(
            "mergeable", MODULE.classify_mergeability(stale, expected_head="old1")
        )

    def test_the_default_delays_back_off(self):
        self.assertEqual((2, 4, 8, 16), MODULE.MERGEABILITY_RETRY_DELAYS)


class RepositorySettingsTest(unittest.TestCase):
    def test_merge_methods_default_to_allowed(self):
        with mock.patch.object(MODULE, "gh_json", return_value={}):
            self.assertEqual(ALL_MERGE_METHODS, MODULE.repository_merge_methods("owner/repo"))

    def test_merge_methods_are_read_from_the_repository(self):
        payload = {
            "allow_merge_commit": False,
            "allow_squash_merge": False,
            "allow_rebase_merge": True,
        }
        with mock.patch.object(MODULE, "gh_json", return_value=payload):
            self.assertEqual(
                REBASE_ONLY_MERGE_METHODS, MODULE.repository_merge_methods("owner/repo")
            )

    def test_a_non_object_response_is_rejected(self):
        with mock.patch.object(MODULE, "gh_json", return_value=None):
            with self.assertRaisesRegex(MODULE.WorkflowError, "repository settings"):
                MODULE.repository_merge_methods("owner/repo")

    def test_open_pull_requests_are_listed_with_query_parameters(self):
        with mock.patch.object(MODULE, "gh_json", return_value=[{"number": 1}, "junk"]) as gh:
            items = MODULE.list_open_pulls("owner/repo", {"base": "feature"})
        self.assertEqual([{"number": 1}], items)
        self.assertEqual(
            [
                "api",
                "--paginate",
                "--method",
                "GET",
                "repos/owner/repo/pulls",
                "-f",
                "state=open",
                "-f",
                "base=feature",
            ],
            gh.call_args[0][0],
        )

    def test_an_empty_listing_is_tolerated(self):
        with mock.patch.object(MODULE, "gh_json", return_value=None):
            self.assertEqual([], MODULE.list_open_pulls("owner/repo", {}))

    def test_a_non_list_listing_is_rejected(self):
        with mock.patch.object(MODULE, "gh_json", return_value={"message": "boom"}):
            with self.assertRaisesRegex(MODULE.WorkflowError, "unexpected pull request"):
                MODULE.list_open_pulls("owner/repo", {})

    def test_summarize_pull_tolerates_missing_sections(self):
        self.assertEqual(
            {
                "number": None,
                "url": None,
                "head_branch": None,
                "head_sha": None,
                "head_repo": None,
                "base_branch": None,
            },
            MODULE.summarize_pull({}),
        )


def pull_payload(number, head_branch, base_branch, head_sha="sha", repo="owner/repo"):
    return {
        "number": number,
        "html_url": f"https://github.com/owner/repo/pull/{number}",
        "head": {"ref": head_branch, "sha": head_sha, "repo": {"full_name": repo}},
        "base": {"ref": base_branch},
    }


class StackRelationsTest(unittest.TestCase):
    def relations(self, dependents, below):
        def fake_list(_repo, parameters):
            return dependents if "base" in parameters else below

        with mock.patch.object(MODULE, "list_open_pulls", side_effect=fake_list):
            return MODULE.stack_relations(pr_metadata())

    def test_finds_the_pull_requests_stacked_on_this_branch(self):
        dependents = [
            pull_payload(9, "child", "feature", head_sha="child1"),
            pull_payload(8, "other", "feature", head_sha="other1"),
            pull_payload(7, "feature", "feature"),
        ]
        relations = self.relations(dependents, [])
        self.assertEqual([8, 9], [item["number"] for item in relations["dependents"]])
        self.assertIsNone(relations["stacked_on"])

    def test_finds_the_pull_request_this_branch_stacks_on(self):
        relations = self.relations([], [pull_payload(4, "main", "trunk")])
        self.assertEqual(4, relations["stacked_on"]["number"])
        self.assertEqual("main", relations["stacked_on"]["head_branch"])

    def test_ignores_this_pull_request_in_both_directions(self):
        relations = self.relations(
            [pull_payload(7, "feature", "feature")], [pull_payload(7, "main", "trunk")]
        )
        self.assertEqual([], relations["dependents"])
        self.assertIsNone(relations["stacked_on"])

    def test_queries_the_upstream_repository_with_the_owner_prefix(self):
        with mock.patch.object(MODULE, "list_open_pulls", return_value=[]) as listing:
            MODULE.stack_relations(pr_metadata())
        self.assertEqual(
            [
                mock.call("owner/repo", {"base": "feature"}),
                mock.call("owner/repo", {"head": "owner:main"}),
            ],
            listing.call_args_list,
        )


class ExternalStackDependentsTest(unittest.TestCase):
    def stack(self):
        return {
            "number": 100,
            "trunk": "main",
            "members": [
                {
                    "number": 19483,
                    "head_branch": "v143",
                    "base_branch": "main",
                    "head_sha": "aaa",
                },
                {
                    "number": 7,
                    "head_branch": "feature",
                    "base_branch": "v143",
                    "head_sha": "head1",
                },
            ],
        }

    def by_branch(self, mapping):
        def fake_list(_repo, parameters):
            return mapping.get(parameters["base"], [])

        return fake_list

    def test_names_open_pull_requests_based_on_a_member_branch(self):
        mapping = {"v143": [pull_payload(42, "outside", "v143")], "feature": []}
        with mock.patch.object(
            MODULE, "list_open_pulls", side_effect=self.by_branch(mapping)
        ):
            found = MODULE.external_stack_dependents(pr_metadata(), self.stack())
        self.assertEqual([42], [item["number"] for item in found])
        self.assertEqual("v143", found[0]["base_branch"])
        self.assertEqual("outside", found[0]["head_branch"])

    def test_excludes_the_stack_members_themselves(self):
        # #7's base is v143, which is another member's head, so #7 shows up when
        # v143 is queried. A member is never its own external dependent.
        mapping = {"v143": [pull_payload(7, "feature", "v143")], "feature": []}
        with mock.patch.object(
            MODULE, "list_open_pulls", side_effect=self.by_branch(mapping)
        ):
            found = MODULE.external_stack_dependents(pr_metadata(), self.stack())
        self.assertEqual([], found)

    def test_does_not_query_the_trunk(self):
        # The trunk is a member's base but never a member's head, so the cascade
        # does not rewrite it and pull requests targeting it are not dependents.
        with mock.patch.object(
            MODULE, "list_open_pulls", return_value=[]
        ) as listing:
            MODULE.external_stack_dependents(pr_metadata(), self.stack())
        queried = sorted(call.args[1]["base"] for call in listing.call_args_list)
        self.assertEqual(["feature", "v143"], queried)
        self.assertNotIn("main", queried)


def dependent(number=9, head_branch="child"):
    return {
        "number": number,
        "url": f"https://github.com/owner/repo/pull/{number}",
        "head_branch": head_branch,
        "head_sha": f"child{number}",
        "head_repo": "owner/repo",
        "base_branch": "feature",
    }


class StrategyChoiceTest(unittest.TestCase):
    def choose(self, requested, merge_methods=None, relations=None):
        return MODULE.choose_strategy(
            requested,
            merge_methods=dict(merge_methods or ALL_MERGE_METHODS),
            relations=dict(relations or NO_RELATIONS),
        )

    def test_rejects_an_unknown_strategy(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "strategy must be one of"):
            self.choose("squash")

    def test_auto_prefers_the_merge_that_keeps_the_history(self):
        decision = self.choose("auto")
        self.assertEqual("merge", decision["strategy"])
        self.assertIn("without rewriting", decision["reason"])
        self.assertEqual([], decision["warnings"])
        self.assertEqual([], decision["rewrite_blockers"])

    def test_auto_still_merges_when_a_dependent_pull_request_exists(self):
        decision = self.choose("auto", relations={"dependents": [dependent()], "stacked_on": None})
        self.assertEqual("merge", decision["strategy"])
        self.assertEqual(1, len(decision["rewrite_blockers"]))
        self.assertIn("#9", decision["rewrite_blockers"][0])

    def test_auto_rebases_only_when_the_repository_forbids_merge_commits(self):
        decision = self.choose("auto", merge_methods=REBASE_ONLY_MERGE_METHODS)
        self.assertEqual("rebase", decision["strategy"])
        self.assertIn("only rebase merging", decision["reason"])

    def test_auto_refuses_when_neither_strategy_is_safe(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "no safe strategy is available"):
            self.choose(
                "auto",
                merge_methods=REBASE_ONLY_MERGE_METHODS,
                relations={"dependents": [dependent()], "stacked_on": None},
            )

    def test_a_repository_that_allows_squash_merging_can_still_merge(self):
        methods = {
            "allow_merge_commit": False,
            "allow_squash_merge": True,
            "allow_rebase_merge": True,
        }
        self.assertEqual("merge", self.choose("auto", merge_methods=methods)["strategy"])

    def test_an_explicit_merge_reports_the_blocker_as_a_warning(self):
        decision = self.choose("merge", merge_methods=REBASE_ONLY_MERGE_METHODS)
        self.assertEqual("merge", decision["strategy"])
        self.assertEqual(1, len(decision["warnings"]))
        self.assertIn("only rebase merging", decision["warnings"][0])

    def test_an_explicit_rebase_is_refused_while_a_dependent_exists(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "refusing to rebase"):
            self.choose(
                "rebase", relations={"dependents": [dependent()], "stacked_on": None}
            )

    def test_an_explicit_rebase_is_allowed_with_no_dependents(self):
        decision = self.choose("rebase")
        self.assertEqual("rebase", decision["strategy"])
        self.assertEqual("rebase", decision["requested"])

    def test_a_pull_request_below_this_one_does_not_block_a_rewrite(self):
        relations = {"dependents": [], "stacked_on": {"number": 3, "head_branch": "main"}}
        self.assertEqual("rebase", self.choose("rebase", relations=relations)["strategy"])


class PushSafetyTest(unittest.TestCase):
    def test_a_normal_fork_branch_has_no_blockers(self):
        self.assertEqual([], MODULE.push_safety_blockers(pr_metadata(), dict(NO_RELATIONS)))

    def test_a_missing_head_branch_blocks_the_push(self):
        blockers = MODULE.push_safety_blockers(
            pr_metadata(head_branch=""), dict(NO_RELATIONS)
        )
        self.assertEqual(["the pull request has no head branch"], blockers)

    def test_a_head_branch_equal_to_the_base_branch_blocks_the_push(self):
        pr = pr_metadata(head_owner="owner", head_repo="repo", base_branch="feature")
        blockers = MODULE.push_safety_blockers(pr, dict(NO_RELATIONS))
        self.assertEqual(1, len(blockers))
        self.assertIn("would write to the base branch", blockers[0])

    def test_the_same_branch_name_in_a_fork_is_not_the_base_branch(self):
        pr = pr_metadata(base_branch="feature")
        self.assertEqual([], MODULE.push_safety_blockers(pr, dict(NO_RELATIONS)))

    def test_a_stacked_pull_request_resolving_to_this_head_blocks_the_push(self):
        relations = {
            "dependents": [],
            "stacked_on": {"number": 3, "head_branch": "feature"},
        }
        blockers = MODULE.push_safety_blockers(pr_metadata(), relations)
        self.assertEqual(1, len(blockers))
        self.assertIn("#3", blockers[0])

    def test_a_stacked_pull_request_on_another_branch_does_not_block(self):
        relations = {"dependents": [], "stacked_on": {"number": 3, "head_branch": "main"}}
        self.assertEqual([], MODULE.push_safety_blockers(pr_metadata(), relations))


class StatusParsingTest(unittest.TestCase):
    def test_parses_ordinary_records(self):
        output = "UU app.py\0 M other.py\0"
        self.assertEqual(
            [{"code": "UU", "path": "app.py"}, {"code": " M", "path": "other.py"}],
            MODULE.parse_status_z(output),
        )

    def test_consumes_the_origin_path_of_a_rename(self):
        output = "R  new.py\0old.py\0UU app.py\0"
        self.assertEqual(
            [
                {"code": "R ", "path": "new.py", "origin": "old.py"},
                {"code": "UU", "path": "app.py"},
            ],
            MODULE.parse_status_z(output),
        )

    def test_consumes_the_origin_path_of_a_copy(self):
        entries = MODULE.parse_status_z("C  copy.py\0source.py\0")
        self.assertEqual("source.py", entries[0]["origin"])

    def test_reports_a_rename_with_no_origin_path(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "no origin path"):
            MODULE.parse_status_z("R  new.py\0")

    def test_reports_an_unparsable_record(self):
        for output in ("UU\0", "UUapp.py\0", "U\0"):
            with self.subTest(output=output):
                with self.assertRaisesRegex(MODULE.WorkflowError, "unparsable"):
                    MODULE.parse_status_z(output)

    def test_an_empty_status_has_no_records(self):
        self.assertEqual([], MODULE.parse_status_z(""))
        self.assertEqual([], MODULE.parse_status_z("\0"))

    def test_paths_with_spaces_survive(self):
        entries = MODULE.parse_status_z("UU src/my file.py\0")
        self.assertEqual("src/my file.py", entries[0]["path"])

    def test_unmerged_entries_keep_only_conflict_codes_and_name_them(self):
        output = "UU app.py\0 M clean.py\0DU gone.py\0?? new.py\0"
        with mock.patch.object(MODULE, "run", return_value=completed(0, output)):
            entries = MODULE.unmerged_entries(Path("."))
        self.assertEqual(
            [
                {"code": "UU", "path": "app.py", "kind": "both modified"},
                {"code": "DU", "path": "gone.py", "kind": "deleted by us"},
            ],
            entries,
        )

    def test_every_unmerged_code_has_a_name(self):
        self.assertEqual(
            {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}, set(MODULE.UNMERGED_CODES)
        )
        self.assertEqual({"DD", "UD", "DU"}, MODULE.DELETION_CONFLICT_CODES)


class ConflictMarkerTest(unittest.TestCase):
    def test_finds_a_two_sided_region(self):
        text = "a\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> base\nz\n"
        result = MODULE.parse_conflict_markers(text)
        self.assertEqual([], result["problems"])
        self.assertEqual(
            [
                {
                    "start_line": 2,
                    "ancestor_line": None,
                    "separator_line": 4,
                    "end_line": 6,
                }
            ],
            result["regions"],
        )

    def test_finds_the_ancestor_line_of_a_diff3_region(self):
        text = "<<<<<<< HEAD\nours\n||||||| merged common ancestors\nbefore\n=======\ntheirs\n>>>>>>> base\n"
        region = MODULE.parse_conflict_markers(text)["regions"][0]
        self.assertEqual(3, region["ancestor_line"])
        self.assertEqual(5, region["separator_line"])

    def test_finds_several_regions(self):
        text = (
            "<<<<<<< HEAD\na\n=======\nb\n>>>>>>> base\n"
            "middle\n"
            "<<<<<<< HEAD\nc\n=======\nd\n>>>>>>> base\n"
        )
        self.assertEqual(2, len(MODULE.parse_conflict_markers(text)["regions"]))

    def test_reports_a_region_that_never_closed(self):
        result = MODULE.parse_conflict_markers("<<<<<<< HEAD\nours\n=======\ntheirs\n")
        self.assertEqual(1, len(result["regions"]))
        self.assertIn("never closed", result["problems"][0])

    def test_reports_a_region_that_closed_unopened(self):
        result = MODULE.parse_conflict_markers("ours\n>>>>>>> base\n")
        self.assertEqual([], result["regions"])
        self.assertIn("closed unopened", result["problems"][0])

    def test_reports_a_region_with_no_separator(self):
        result = MODULE.parse_conflict_markers("<<<<<<< HEAD\nours\n>>>>>>> base\n")
        self.assertIn("has no separator", result["problems"][0])
        self.assertEqual(1, len(result["regions"]))

    def test_reports_a_region_opened_inside_another(self):
        text = "<<<<<<< HEAD\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> base\n"
        result = MODULE.parse_conflict_markers(text)
        self.assertIn("opened inside the region", result["problems"][0])

    def test_a_separator_outside_a_region_is_not_a_conflict(self):
        text = "Heading\n=======\nbody\n"
        self.assertEqual(
            {"regions": [], "problems": []}, MODULE.parse_conflict_markers(text)
        )

    def test_longer_marker_runs_are_not_conflict_markers(self):
        text = "<<<<<<<<< HEAD\nours\n========\ntheirs\n>>>>>>>> base\n"
        self.assertEqual([], MODULE.parse_conflict_markers(text)["regions"])

    def test_a_bare_marker_with_no_label_still_counts(self):
        text = "<<<<<<<\nours\n=======\ntheirs\n>>>>>>>\n"
        self.assertEqual(1, len(MODULE.parse_conflict_markers(text)["regions"]))

    def test_clean_text_has_no_regions(self):
        self.assertEqual(
            {"regions": [], "problems": []},
            MODULE.parse_conflict_markers("just some code\n"),
        )


class LineEndingTest(unittest.TestCase):
    def test_the_style_of_a_file_is_named(self):
        for data, expected in (
            (b"a\nb\n", "lf"),
            (b"a\r\nb\r\n", "crlf"),
            (b"a\r\nb\n", "mixed"),
            (b"a", "none"),
            (b"", "none"),
            (None, "none"),
        ):
            with self.subTest(data=data):
                self.assertEqual(expected, MODULE.line_ending_style(data))

    def test_a_style_neither_side_used_is_reported(self):
        self.assertEqual(
            "crlf", MODULE.introduced_line_ending(b"a\r\n", (b"a\n", b"b\n"))
        )
        self.assertEqual(
            "lf", MODULE.introduced_line_ending(b"a\n", (b"a\r\n", b"b\r\n"))
        )
        self.assertEqual(
            "crlf", MODULE.introduced_line_ending(b"a\r\nb\n", (b"a\n", b"b\n"))
        )

    def test_both_endings_arriving_at_once_are_not_judged(self):
        self.assertIsNone(MODULE.introduced_line_ending(b"a\r\nb\n", (b"a", b"b")))

    def test_a_style_a_side_already_used_is_not_reported(self):
        self.assertIsNone(MODULE.introduced_line_ending(b"a\n", (b"a\n", b"b\n")))
        self.assertIsNone(MODULE.introduced_line_ending(b"a\r\n", (b"a\n", b"b\r\n")))
        self.assertIsNone(MODULE.introduced_line_ending(b"a\r\n", (b"a\r\nb\n", None)))

    def test_a_mixed_side_permits_either_ending(self):
        for resolved in (b"a\n", b"a\r\n", b"a\r\nb\n"):
            with self.subTest(resolved=resolved):
                self.assertIsNone(
                    MODULE.introduced_line_ending(resolved, (b"a\r\nb\n", None))
                )

    def test_a_file_without_a_line_ending_is_not_reported(self):
        self.assertIsNone(MODULE.introduced_line_ending(b"a", (b"a\n", b"b\n")))
        self.assertIsNone(MODULE.introduced_line_ending(b"", (b"a\n", b"b\n")))

    def test_a_side_that_carries_no_line_ending_decides_nothing(self):
        self.assertIsNone(MODULE.introduced_line_ending(b"a\r\n", (b"a", None)))


class ConflictEvidenceTest(unittest.TestCase):
    def test_the_signature_ignores_the_order_of_the_paths(self):
        self.assertEqual(
            MODULE.conflict_signature(["b.py", "a.py"]),
            MODULE.conflict_signature(["a.py", "b.py"]),
        )
        self.assertNotEqual(
            MODULE.conflict_signature(["a.py"]), MODULE.conflict_signature(["a.py", "b.py"])
        )

    def test_normalizing_content_folds_windows_line_endings(self):
        self.assertEqual(b"a\nb\n", MODULE.normalize_content(b"a\r\nb\r\n"))
        self.assertIsNone(MODULE.normalize_content(None))

    def test_reading_a_binary_or_missing_file_yields_nothing(self):
        directory = temporary_directory(self)
        binary = directory / "image.bin"
        binary.write_bytes(b"\x89PNG\0\0")
        self.assertIsNone(MODULE.read_worktree_text(binary))
        self.assertIsNone(MODULE.read_worktree_text(directory / "absent.txt"))

    def test_reading_a_text_file_replaces_undecodable_bytes(self):
        directory = temporary_directory(self)
        path = directory / "app.py"
        path.write_bytes(b"caf\xe9\n")
        self.assertEqual("caf\ufffd\n", MODULE.read_worktree_text(path))

    def test_commits_touching_parses_the_record_separator(self):
        output = "sha1\x1fAda\x1f2026-01-01T00:00:00Z\x1fFirst\nsha2\x1fGrace\x1f2026-01-02T00:00:00Z\x1fSecond\n"
        with mock.patch.object(MODULE, "git_try", return_value=completed(0, output)):
            commits = MODULE.commits_touching(Path("."), "a..b", "app.py")
        self.assertEqual(["sha1", "sha2"], [item["sha"] for item in commits])
        self.assertEqual("Ada", commits[0]["author"])
        self.assertEqual("Second", commits[1]["subject"])

    def test_commits_touching_skips_malformed_and_failed_output(self):
        with mock.patch.object(MODULE, "git_try", return_value=completed(0, "junk\n\n")):
            self.assertEqual([], MODULE.commits_touching(Path("."), "a..b", "app.py"))
        with mock.patch.object(MODULE, "git_try", return_value=completed(128, "", "bad")):
            self.assertEqual([], MODULE.commits_touching(Path("."), "a..b", "app.py"))

    def test_collect_conflicts_gathers_evidence_for_every_path(self):
        directory = temporary_directory(self)
        (directory / "app.py").write_text(
            "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> base\n", encoding="utf-8"
        )
        (directory / "logo.png").write_bytes(b"\x89PNG\0")
        entries = [
            {"code": "UU", "path": "app.py", "kind": "both modified"},
            {"code": "DU", "path": "logo.png", "kind": "deleted by us"},
        ]
        blobs = {
            "app.py": {"ancestor": b"a", "head": b"b", "base": b"c"},
            "logo.png": {"ancestor": None, "head": None, "base": b"c"},
        }
        with mock.patch.object(
            MODULE, "unmerged_entries", return_value=entries
        ), mock.patch.object(
            MODULE, "stage_blobs", side_effect=lambda _root, path: blobs[path]
        ), mock.patch.object(
            MODULE, "commits_touching", return_value=[{"sha": "c1", "subject": "Edit"}]
        ) as commits:
            conflicts = MODULE.collect_conflicts(
                directory, head_sha="head1", base_sha="base1", merge_base="merge0"
            )

        self.assertEqual(["app.py", "logo.png"], [item["path"] for item in conflicts])
        source = conflicts[0]
        self.assertFalse(source["binary"])
        self.assertFalse(source["deletion"])
        self.assertEqual(1, len(source["marker_regions"]))
        self.assertEqual(["ancestor", "base", "head"], source["present_stages"])
        self.assertEqual("conflicted", source["status"])
        self.assertIsNone(source["rationale"])
        image = conflicts[1]
        self.assertTrue(image["binary"])
        self.assertTrue(image["deletion"])
        self.assertEqual(["base"], image["present_stages"])
        self.assertEqual([], image["marker_regions"])
        self.assertEqual(
            [
                mock.call(directory, "merge0..head1", "app.py"),
                mock.call(directory, "merge0..base1", "app.py"),
                mock.call(directory, "merge0..head1", "logo.png"),
                mock.call(directory, "merge0..base1", "logo.png"),
            ],
            commits.call_args_list,
        )

    def test_collect_conflicts_sorts_by_path(self):
        directory = temporary_directory(self)
        entries = [
            {"code": "UU", "path": "z.py", "kind": "both modified"},
            {"code": "UU", "path": "a.py", "kind": "both modified"},
        ]
        with mock.patch.object(
            MODULE, "unmerged_entries", return_value=entries
        ), mock.patch.object(
            MODULE, "stage_blobs", return_value={"ancestor": None, "head": None, "base": None}
        ), mock.patch.object(MODULE, "commits_touching", return_value=[]):
            conflicts = MODULE.collect_conflicts(
                directory, head_sha="h", base_sha="b", merge_base="m"
            )
        self.assertEqual(["a.py", "z.py"], [item["path"] for item in conflicts])


class WorktreeStateTest(unittest.TestCase):
    def test_a_dirty_worktree_is_refused(self):
        with mock.patch.object(MODULE, "git", return_value=" M app.py"):
            with self.assertRaisesRegex(MODULE.WorkflowError, "worktree is not clean"):
                MODULE.require_clean_worktree(Path("."))

    def test_a_clean_worktree_passes(self):
        with mock.patch.object(MODULE, "git", return_value=""):
            MODULE.require_clean_worktree(Path("."))

    def test_a_rebase_in_progress_is_detected_from_either_directory(self):
        directory = temporary_directory(self)
        (directory / "rebase-apply").mkdir()

        def fake_git_try(_root, *arguments):
            name = arguments[-1]
            if name == "rebase-merge":
                return completed(0, str(directory / "rebase-merge"))
            return completed(0, str(directory / "rebase-apply"))

        with mock.patch.object(MODULE, "git_try", side_effect=fake_git_try):
            self.assertTrue(MODULE.rebase_in_progress(directory))

    def test_a_relative_rebase_path_is_resolved_inside_the_repository(self):
        directory = temporary_directory(self)
        (directory / ".git" / "rebase-merge").mkdir(parents=True)
        with mock.patch.object(
            MODULE,
            "git_try",
            side_effect=[
                completed(0, ".git/rebase-merge"),
                completed(0, ".git/rebase-apply"),
            ],
        ):
            self.assertTrue(MODULE.rebase_in_progress(directory))

    def test_no_rebase_when_neither_directory_exists(self):
        directory = temporary_directory(self)
        with mock.patch.object(
            MODULE, "git_try", return_value=completed(0, str(directory / "missing"))
        ):
            self.assertFalse(MODULE.rebase_in_progress(directory))

    def test_a_merge_in_progress_is_detected_from_merge_head(self):
        with mock.patch.object(MODULE, "git_try", return_value=completed(0, "sha")):
            self.assertTrue(MODULE.merge_in_progress(Path(".")))
        with mock.patch.object(MODULE, "git_try", return_value=completed(1)):
            self.assertFalse(MODULE.merge_in_progress(Path(".")))

    def test_integration_in_progress_names_the_operation(self):
        with mock.patch.object(MODULE, "rebase_in_progress", return_value=True):
            self.assertEqual("rebase", MODULE.integration_in_progress(Path(".")))
        with mock.patch.object(
            MODULE, "rebase_in_progress", return_value=False
        ), mock.patch.object(MODULE, "merge_in_progress", return_value=True):
            self.assertEqual("merge", MODULE.integration_in_progress(Path(".")))
        with mock.patch.object(
            MODULE, "rebase_in_progress", return_value=False
        ), mock.patch.object(MODULE, "merge_in_progress", return_value=False):
            self.assertIsNone(MODULE.integration_in_progress(Path(".")))

    def test_an_integration_in_progress_blocks_a_new_attempt(self):
        with mock.patch.object(MODULE, "integration_in_progress", return_value="merge"):
            with self.assertRaisesRegex(MODULE.WorkflowError, "already in progress"):
                MODULE.require_no_integration_in_progress(Path("."))

    def test_find_remote_matches_the_repository_case_insensitively(self):
        def fake_git(_root, *arguments):
            if arguments == ("remote",):
                return "origin\nupstream"
            if arguments[-1] == "origin":
                return "https://github.com/fork/repo.git"
            return "git@github.com:Owner/Repo.git"

        with mock.patch.object(MODULE, "git", side_effect=fake_git):
            self.assertEqual("upstream", MODULE.find_remote(Path("."), "owner/repo", push=False))

    def test_find_remote_reports_when_nothing_matches(self):
        def fake_git(_root, *arguments):
            return "origin" if arguments == ("remote",) else "https://github.com/other/repo.git"

        with mock.patch.object(MODULE, "git", side_effect=fake_git):
            with self.assertRaisesRegex(MODULE.WorkflowError, "no git remote points to"):
                MODULE.find_remote(Path("."), "owner/repo", push=True)

    def test_find_remote_asks_for_the_push_url_when_pushing(self):
        with mock.patch.object(
            MODULE, "git", side_effect=["origin", "https://github.com/owner/repo.git"]
        ) as git_call:
            MODULE.find_remote(Path("."), "owner/repo", push=True)
        self.assertEqual(
            ("remote", "get-url", "--push", "origin"), git_call.call_args[0][1:]
        )


class RemoteHeadTest(unittest.TestCase):
    def test_a_missing_branch_reads_as_none(self):
        with mock.patch.object(
            MODULE, "run", return_value=completed(1, "", "gh: HTTP 404 Not Found")
        ):
            self.assertIsNone(MODULE.remote_head("owner", "repo", "feature"))

    def test_any_other_failure_is_reported(self):
        with mock.patch.object(MODULE, "run", return_value=completed(1, "", "boom")):
            with self.assertRaisesRegex(MODULE.WorkflowError, "failed to read remote ref"):
                MODULE.remote_head("owner", "repo", "feature")

    def test_the_object_sha_is_returned(self):
        payload = json.dumps({"object": {"sha": "abc"}})
        with mock.patch.object(MODULE, "run", return_value=completed(0, payload)):
            self.assertEqual("abc", MODULE.remote_head("owner", "repo", "feature"))

    def test_waiting_stops_as_soon_as_the_head_matches(self):
        with mock.patch.object(
            MODULE, "remote_head", side_effect=["old", "new"]
        ) as head, mock.patch.object(MODULE, "time") as clock:
            self.assertEqual("new", MODULE.wait_for_remote_head("o", "r", "b", "new"))
        self.assertEqual(2, head.call_count)
        self.assertEqual(1, clock.sleep.call_count)

    def test_waiting_gives_up_and_returns_what_it_saw(self):
        with mock.patch.object(
            MODULE, "remote_head", return_value="old"
        ) as head, mock.patch.object(MODULE, "time"):
            self.assertEqual("old", MODULE.wait_for_remote_head("o", "r", "b", "new"))
        self.assertEqual(1 + len(MODULE.REMOTE_REF_LAG_RETRY_DELAYS), head.call_count)

    def test_a_fork_head_never_needs_the_upstream_branch_check(self):
        with mock.patch.object(MODULE, "remote_head") as head:
            MODULE.require_fork_head(pr_metadata())
        head.assert_not_called()

    def test_an_upstream_head_branch_must_already_exist(self):
        pr = pr_metadata(head_owner="owner", head_repo="repo")
        with mock.patch.object(MODULE, "remote_head", return_value=None):
            with self.assertRaisesRegex(MODULE.WorkflowError, "refusing to push directly"):
                MODULE.require_fork_head(pr)
        with mock.patch.object(MODULE, "remote_head", return_value="abc"):
            MODULE.require_fork_head(pr)


class PushRangeVerificationTest(unittest.TestCase):
    def verify(self, **overrides):
        arguments = {
            "strategy": "merge",
            "previous_remote_head": "remote1",
            "local_head": "local1",
            "merge_base": "merge0",
            "original_subjects": ["Add a thing"],
        }
        arguments.update(overrides)
        return MODULE.verify_push_range(Path("."), **arguments)

    def test_missing_subjects_reports_each_dropped_commit_once(self):
        self.assertEqual([], MODULE.missing_subjects(["a", "b"], ["b", "a", "c"]))
        self.assertEqual(["b"], MODULE.missing_subjects(["a", "b"], ["a"]))
        self.assertEqual(["a"], MODULE.missing_subjects(["a", "a"], ["a"]))
        self.assertEqual([], MODULE.missing_subjects([], []))

    def test_a_merge_onto_a_missing_branch_is_refused(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "head branch that does not exist"):
            self.verify(previous_remote_head=None)

    def test_a_merge_that_would_not_fast_forward_is_refused(self):
        with mock.patch.object(MODULE, "git_try", return_value=completed(1)):
            with self.assertRaisesRegex(MODULE.WorkflowError, "not be a fast-forward"):
                self.verify()

    def test_a_merge_reports_the_commits_it_adds(self):
        with mock.patch.object(
            MODULE, "git_try", return_value=completed(0)
        ), mock.patch.object(MODULE, "git", return_value="c2\nc1\n"):
            report = self.verify()
        self.assertEqual(["c2", "c1"], report["added_commits"])
        self.assertEqual(
            ["the pushed head contains the previous remote head"], report["checks"]
        )

    def test_a_rebase_that_dropped_a_commit_is_refused(self):
        with mock.patch.object(MODULE, "commit_subjects", return_value=["Other"]):
            with self.assertRaisesRegex(MODULE.WorkflowError, "dropped commits"):
                self.verify(strategy="rebase")

    def test_a_rebase_that_kept_every_subject_is_accepted(self):
        with mock.patch.object(
            MODULE, "commit_subjects", return_value=["Add a thing"]
        ) as subjects:
            report = self.verify(strategy="rebase")
        self.assertEqual(["Add a thing"], report["rewritten_commits"])
        self.assertIn("survived the rebase", report["checks"][0])
        self.assertEqual("merge0..local1", subjects.call_args[0][1])

    def test_a_rebase_does_not_need_a_previous_remote_head(self):
        with mock.patch.object(MODULE, "commit_subjects", return_value=["Add a thing"]):
            self.verify(strategy="rebase", previous_remote_head=None)

    def test_commit_subjects_drops_blank_lines_and_reports_failure(self):
        with mock.patch.object(
            MODULE, "git_try", return_value=completed(0, "First\n\nSecond\n")
        ):
            self.assertEqual(["First", "Second"], MODULE.commit_subjects(Path("."), "a..b"))
        with mock.patch.object(MODULE, "git_try", return_value=completed(128)):
            with self.assertRaisesRegex(MODULE.WorkflowError, "could not list commits"):
                MODULE.commit_subjects(Path("."), "a..b")


class StateFileTest(unittest.TestCase):
    def test_a_saved_state_round_trips_and_gains_a_timestamp(self):
        directory = temporary_directory(self)
        path = directory / "nested" / "state.json"
        state = {"version": MODULE.STATE_VERSION, "attempts": 1}
        MODULE.save_state(path, state)
        loaded = MODULE.load_state(path)
        self.assertEqual(1, loaded["attempts"])
        self.assertTrue(loaded["updated_at"].endswith("Z"))
        self.assertEqual([path.name], [item.name for item in path.parent.iterdir()])

    def test_a_missing_state_file_is_reported(self):
        directory = temporary_directory(self)
        with self.assertRaisesRegex(MODULE.WorkflowError, "state file does not exist"):
            MODULE.load_state(directory / "state.json")

    def test_a_state_from_another_version_is_refused(self):
        directory = temporary_directory(self)
        path = directory / "state.json"
        path.write_text(json.dumps({"version": MODULE.STATE_VERSION + 1}), encoding="utf-8")
        with self.assertRaisesRegex(MODULE.WorkflowError, "unsupported state version"):
            MODULE.load_state(path)

    def test_a_failed_save_leaves_no_temporary_file(self):
        directory = temporary_directory(self)
        path = directory / "state.json"

        class Unserializable:
            pass

        with self.assertRaises(TypeError):
            MODULE.save_state(path, {"version": 1, "bad": Unserializable()})
        self.assertEqual([], list(directory.iterdir()))

    def test_a_result_file_is_written_with_sorted_keys(self):
        directory = temporary_directory(self)
        path = directory / "out" / "result.json"
        MODULE.write_result_file(path, {"b": 2, "a": 1}, "preflight")
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith('{\n  "a": 1'))
        self.assertTrue(text.endswith("\n"))

    def test_a_result_file_that_cannot_be_written_is_reported(self):
        directory = temporary_directory(self)
        blocker = directory / "blocker"
        blocker.write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.WorkflowError, "could not write the preflight"):
            MODULE.write_result_file(blocker / "result.json", {}, "preflight")

    def test_counting_by_status_tolerates_missing_values(self):
        items = [{"status": "resolved"}, {"status": "resolved"}, {}, None or {"status": None}]
        self.assertEqual({"resolved": 2, "unknown": 2}, MODULE.count_by_status(items))
        self.assertEqual({}, MODULE.count_by_status(None))

    def test_text_input_comes_from_a_file_or_standard_input(self):
        directory = temporary_directory(self)
        path = directory / "reason.txt"
        path.write_text("  both sides kept  \n", encoding="utf-8")
        self.assertEqual("both sides kept", MODULE.load_text_input(str(path), "rationale"))
        with mock.patch.object(MODULE.sys, "stdin", SimpleNamespace(read=lambda: " piped ")):
            self.assertEqual("piped", MODULE.load_text_input("-", "rationale"))

    def test_empty_or_unreadable_text_input_is_refused(self):
        directory = temporary_directory(self)
        empty = directory / "empty.txt"
        empty.write_text("   \n", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.WorkflowError, "must not be empty"):
            MODULE.load_text_input(str(empty), "rationale")
        with self.assertRaisesRegex(MODULE.WorkflowError, "could not read the rationale"):
            MODULE.load_text_input(str(directory / "absent.txt"), "rationale")


class StateHelpersTest(unittest.TestCase):
    def test_the_active_attempt_must_exist_and_be_unfinished(self):
        self.assertEqual(
            attempt_record(), MODULE.active_attempt({"attempt": attempt_record()})
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "run preflight first"):
            MODULE.active_attempt({"attempt": None})
        for status in ("published", "aborted"):
            with self.subTest(status=status):
                with self.assertRaisesRegex(MODULE.WorkflowError, f"already {status}"):
                    MODULE.active_attempt({"attempt": attempt_record(status=status)})

    def test_finding_conflicts_reports_a_path_that_is_not_conflicted(self):
        attempt = attempt_record(
            conflicts=[conflict_record("a.py"), conflict_record("b.py")]
        )
        self.assertEqual(
            ["b.py", "a.py"],
            [item["path"] for item in MODULE.find_conflicts(attempt, ["b.py", "a.py"])],
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "not conflicted in this attempt"):
            MODULE.find_conflicts(attempt, ["c.py"])

    def test_archiving_records_a_finished_attempt_once(self):
        state = {
            "history": [],
            "attempt": attempt_record(
                status="published",
                published_head_sha="head2",
                conflicts=[
                    conflict_record("a.py", status="resolved", rationale="kept both"),
                    conflict_record("b.py"),
                ],
                stack={
                    "validation_fix_checkpoints": [
                        {
                            "paths": ["caller.py"],
                            "rationale": "The caller follows the resolved contract.",
                        }
                    ]
                },
            ),
        }
        MODULE.archive_attempt(state)
        MODULE.archive_attempt(state)
        self.assertEqual(1, len(state["history"]))
        entry = state["history"][0]
        self.assertEqual("published", entry["status"])
        self.assertEqual(["a.py", "b.py"], entry["conflict_paths"])
        self.assertEqual(
            [{"path": "a.py", "kind": "both modified", "rationale": "kept both", "one_side": None}],
            entry["resolutions"],
        )
        self.assertEqual(
            ["caller.py"],
            entry["validation_fix_checkpoints"][0]["paths"],
        )
        self.assertTrue(entry["ended_at"].endswith("Z"))

    def test_archiving_ignores_an_unfinished_or_absent_attempt(self):
        state = {"history": [], "attempt": attempt_record(status="conflicted")}
        MODULE.archive_attempt(state)
        self.assertEqual([], state["history"])
        empty = {"history": []}
        MODULE.archive_attempt(empty)
        self.assertEqual([], empty["history"])

    def test_archiving_accepts_every_finished_status(self):
        for status in MODULE.RECORDED_ENDINGS:
            with self.subTest(status=status):
                state = {"history": [], "attempt": attempt_record(status=status)}
                MODULE.archive_attempt(state)
                self.assertEqual(1, len(state["history"]))

    def test_recording_an_escalation_replaces_the_previous_one(self):
        state = {"escalation": {"kind": "other"}}
        escalation = MODULE.record_escalation(
            state,
            kind="contradiction",
            reason="the two sides contradict each other",
            recommended_action="a person must decide",
            attempt_number=2,
        )
        self.assertEqual(escalation, state["escalation"])
        self.assertEqual("contradiction", escalation["kind"])
        self.assertEqual(2, escalation["attempt_number"])
        self.assertTrue(escalation["recorded_at"].endswith("Z"))

    def test_loading_an_old_escalation_defaults_to_a_user_decision(self):
        directory = temporary_directory(self)
        path = write_state(
            directory,
            escalation={"kind": "contradiction", "reason": "both cannot hold"},
        )
        self.assertTrue(
            MODULE.load_state(path)["escalation"]["requires_user_decision"]
        )

    def test_only_automation_blockers_omit_a_user_decision(self):
        for kind in MODULE.ESCALATION_KINDS:
            with self.subTest(kind=kind):
                state = {}
                escalation = MODULE.record_escalation(
                    state,
                    kind=kind,
                    reason="reason",
                    recommended_action="action",
                    attempt_number=1,
                )
                self.assertEqual(
                    kind != MODULE.AUTOMATION_BLOCKER_KIND,
                    escalation["requires_user_decision"],
                )

    def test_every_escalation_kind_is_declared(self):
        self.assertEqual(
            (
                "contradiction",
                "unsafe_push",
                "unknown_mergeability",
                "ad_hoc_base",
                "stack_external_dependents",
                "validation",
                "automation_blocker",
                "other",
            ),
            MODULE.ESCALATION_KINDS,
        )

    def test_the_attempt_summary_counts_conflict_statuses(self):
        attempt = attempt_record(
            conflicts=[
                conflict_record("a.py", status="resolved"),
                conflict_record("b.py"),
            ]
        )
        summary = MODULE.attempt_summary(attempt)
        self.assertEqual({"resolved": 1, "conflicted": 1}, summary["conflict_statuses"])
        self.assertEqual("pr-7-attempt-1", summary["id"])
        self.assertNotIn("conflicts", summary)
        self.assertIsNone(MODULE.attempt_summary(None))

    def test_the_merge_commit_message_names_every_resolution(self):
        state = {"pr": pr_metadata()}
        attempt = attempt_record(
            conflicts=[
                conflict_record("a.py", status="resolved", rationale="  kept both  "),
                conflict_record("b.py", status="resolved", rationale=None),
                conflict_record("c.py"),
            ]
        )
        message = MODULE.merge_commit_message(state, attempt)
        self.assertTrue(message.startswith("Merge branch 'main' into feature\n"))
        self.assertIn("Keep what both sides meant to do", message)
        self.assertIn("a.py: kept both", message)
        self.assertIn("b.py: resolved", message)
        self.assertNotIn("c.py", message)
        self.assertTrue(message.endswith("\n"))


class FetchReferenceTest(unittest.TestCase):
    def test_a_direct_sha_fetch_is_enough(self):
        with mock.patch.object(MODULE, "git_try", return_value=completed(0)) as git_try:
            MODULE.fetch_reference(Path("."), "origin", "main", "base1")
        self.assertEqual(2, git_try.call_count)

    def test_a_server_that_refuses_a_sha_falls_back_to_the_branch(self):
        with mock.patch.object(
            MODULE, "git_try", side_effect=[completed(1), completed(0), completed(0)]
        ) as git_try:
            MODULE.fetch_reference(Path("."), "origin", "main", "base1")
        self.assertEqual(
            ("fetch", "--no-tags", "origin", "refs/heads/main"), git_try.call_args_list[1][0][1:]
        )

    def test_a_failed_fetch_is_reported(self):
        with mock.patch.object(
            MODULE, "git_try", side_effect=[completed(1), completed(1, "", "gone")]
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "could not fetch origin/main"):
                MODULE.fetch_reference(Path("."), "origin", "main", "base1")

    def test_a_missing_commit_after_the_fetch_is_reported(self):
        with mock.patch.object(
            MODULE, "git_try", side_effect=[completed(0), completed(1)]
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "is missing after fetching"):
                MODULE.fetch_reference(Path("."), "origin", "main", "base1")


class CheckoutTest(unittest.TestCase):
    def checkout(self, branches):
        """Run the checkout with `git` answering the branch readings in turn."""
        with mock.patch.object(MODULE, "run") as runner, mock.patch.object(
            MODULE, "git", side_effect=[*branches, "head1"]
        ):
            attached = MODULE.checkout_pr_branch(
                Path("."), MODULE.parse_target("owner/repo#7"), pr_metadata()
            )
        return attached, runner.call_args[0][0]

    def test_a_worktree_elsewhere_detaches_onto_the_head(self):
        attached, command = self.checkout(["other", ""])
        self.assertFalse(attached)
        self.assertEqual(
            [
                "gh",
                "pr",
                "checkout",
                "https://github.com/owner/repo/pull/7",
                "--detach",
            ],
            command,
        )

    def test_a_worktree_already_holding_the_branch_keeps_it(self):
        attached, command = self.checkout(["feature", "feature"])
        self.assertTrue(attached)
        self.assertEqual(
            ["gh", "pr", "checkout", "https://github.com/owner/repo/pull/7"],
            command,
        )

    def test_landing_on_some_other_branch_is_refused(self):
        with mock.patch.object(MODULE, "run"), mock.patch.object(
            MODULE, "git", side_effect=["other", "unrelated", "head1"]
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "branch mismatch"):
                MODULE.checkout_pr_branch(
                    Path("."), MODULE.parse_target("owner/repo#7"), pr_metadata()
                )

    def test_local_work_ahead_of_the_pull_request_head_is_refused(self):
        with mock.patch.object(MODULE, "run"), mock.patch.object(
            MODULE, "git", side_effect=["feature", "feature", "local9"]
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "HEAD mismatch"):
                MODULE.checkout_pr_branch(
                    Path("."), MODULE.parse_target("owner/repo#7"), pr_metadata()
                )

    def test_a_detached_head_is_not_read_as_another_line_of_work(self):
        with mock.patch.object(MODULE, "git", return_value=""):
            self.assertIsNone(MODULE.attached_to_other_branch(Path("."), "feature"))


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory(self)
        self.state_path = self.directory / "state.json"

    def preflight(
        self,
        *,
        metadata=None,
        relations=None,
        merge_methods=None,
        strategy="auto",
        state=None,
        detection=None,
        ad_hoc=None,
        external=None,
        whole_stack=False,
        whole_stack_mergeable=False,
    ):
        args = SimpleNamespace(
            target="owner/repo#7",
            repo_root=str(self.directory),
            state=str(state or self.state_path),
            strategy=strategy,
            whole_stack=whole_stack,
        )
        ad_hoc = ad_hoc or {
            "reason": "the head conflicts with develop in docs/list.yaml",
            "recommended_action": "a person must resolve the conflict with develop",
            "base_branch": "feature-base",
            "base_conflicts": [],
            "default_branch": "develop",
            "default_conflicts": ["docs/list.yaml"],
        }
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "resolve_repo_root", return_value=self.directory
        ), mock.patch.object(
            MODULE, "require_clean_worktree"
        ), mock.patch.object(
            MODULE, "require_no_integration_in_progress"
        ), mock.patch.object(
            MODULE, "live_mergeability", return_value=metadata or pr_metadata()
        ), mock.patch.object(
            MODULE, "checkout_pr_branch"
        ) as checkout, mock.patch.object(
            MODULE, "stack_relations", return_value=dict(relations or NO_RELATIONS)
        ), mock.patch.object(
            MODULE,
            "stack_membership",
            return_value=detection or {"default_branch": "main", "stack": None},
        ), mock.patch.object(
            MODULE, "find_remote", return_value="origin"
        ), mock.patch.object(
            MODULE, "base_ref_tip", return_value="defaultsha"
        ), mock.patch.object(
            MODULE, "ad_hoc_escalation", return_value=ad_hoc
        ), mock.patch.object(
            MODULE, "external_stack_dependents", return_value=external or []
        ), mock.patch.object(
            MODULE, "stack_is_mergeable", return_value=whole_stack_mergeable
        ), mock.patch.object(
            MODULE, "record_stack_member_clearances"
        ) as record_stack_clearances, mock.patch.object(
            MODULE,
            "repository_merge_methods",
            return_value=dict(merge_methods or ALL_MERGE_METHODS),
        ), mock.patch.object(
            MODULE, "emit"
        ) as emit:
            MODULE.command_preflight(args)
        self.checkout = checkout
        self.record_stack_clearances = record_stack_clearances
        return emitted(emit)

    def saved(self, path=None):
        return json.loads(Path(path or self.state_path).read_text(encoding="utf-8"))

    def test_a_conflicted_pull_request_is_ready_and_plans_an_attempt(self):
        payload = self.preflight()
        self.assertEqual("ready", payload["result"])
        self.assertEqual("conflicting", payload["mergeability"])
        self.assertEqual("merge", payload["strategy"])
        self.assertEqual(1, payload["attempt_number"])
        self.assertNotIn("max_iterations", payload)
        self.assertIsNone(payload["escalation"])
        state = self.saved()
        self.assertEqual("planned", state["attempt"]["status"])
        self.assertEqual("pr-7-attempt-1", state["attempt"]["id"])
        self.assertEqual("head1", state["attempt"]["head_sha"])
        self.assertIsNone(state["attempt"]["mergeable_at_head_sha"])
        self.checkout.assert_called_once()

    def test_the_preflight_file_carries_the_full_detail(self):
        payload = self.preflight()
        detail = json.loads(
            Path(payload["preflight_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual("ready", detail["result"])
        self.assertEqual(pr_metadata(), detail["pr"])
        self.assertEqual([], detail["push_blockers"])
        self.assertEqual("merge", detail["strategy"]["strategy"])
        self.assertIsNone(detail["strategy_error"])

    def test_a_mergeable_pull_request_needs_no_work(self):
        payload = self.preflight(metadata=pr_metadata(mergeable="MERGEABLE"))
        self.assertEqual("mergeable", payload["result"])
        state = self.saved()
        self.assertEqual("mergeable", state["attempt"]["status"])
        self.assertEqual("head1", state["attempt"]["mergeable_at_head_sha"])

    def test_mergeability_github_never_computed_escalates(self):
        payload = self.preflight(metadata=pr_metadata(mergeable="UNKNOWN"))
        self.assertEqual("unknown_mergeability", payload["result"])
        self.assertEqual("unknown_mergeability", payload["escalation"]["kind"])
        state = self.saved()
        self.assertEqual("escalated", state["attempt"]["status"])
        self.assertEqual("pr-7-attempt-1", state["history"][0]["id"])

    def test_a_closed_pull_request_is_refused(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "is closed"):
            self.preflight(metadata=pr_metadata(state="CLOSED"))

    def test_an_unsafe_push_escalates_before_anything_else(self):
        relations = {"dependents": [], "stacked_on": {"number": 3, "head_branch": "feature"}}
        payload = self.preflight(
            metadata=pr_metadata(mergeable="MERGEABLE"), relations=relations
        )
        self.assertEqual("unsafe_push", payload["result"])
        self.assertEqual("unsafe_push", payload["escalation"]["kind"])
        self.assertIn("#3", payload["push_blockers"][0])
        self.assertEqual("escalated", self.saved()["attempt"]["status"])

    def test_no_safe_strategy_escalates_instead_of_guessing(self):
        relations = {"dependents": [dependent()], "stacked_on": None}
        payload = self.preflight(
            relations=relations, merge_methods=REBASE_ONLY_MERGE_METHODS
        )
        self.assertEqual("no_safe_strategy", payload["result"])
        self.assertIn("no safe strategy", payload["strategy_error"])
        self.assertEqual("unsafe_push", payload["escalation"]["kind"])
        self.assertIsNone(payload["strategy"])

    def test_every_preflight_opens_the_next_attempt_without_a_cap(self):
        """A cap would strand a pull request the user asked about again.

        Numbering only has to keep climbing so two runs never share an
        attempt id.
        """
        write_state(self.directory, attempts=40, attempt=None, history=[])
        payload = self.preflight()
        self.assertEqual("ready", payload["result"])
        self.assertEqual(41, payload["attempt_number"])
        self.assertEqual(41, self.saved()["attempts"])
        self.assertIsNone(payload["escalation"])

    def test_a_second_preflight_archives_the_finished_attempt(self):
        write_state(
            self.directory,
            attempts=1,
            attempt=attempt_record(status="published", published_head_sha="head2"),
            history=[],
        )
        payload = self.preflight()
        self.assertEqual(1, payload["counts"]["history"])
        state = self.saved()
        self.assertEqual("published", state["history"][0]["status"])
        self.assertEqual("pr-7-attempt-2", state["attempt"]["id"])

    def test_a_new_run_clears_a_stale_escalation(self):
        write_state(
            self.directory,
            attempt=None,
            escalation={"kind": "contradiction", "reason": "stuck"},
        )
        payload = self.preflight()
        self.assertIsNone(payload["escalation"])
        self.assertIsNone(self.saved()["escalation"])

    def test_an_explicit_rebase_request_is_honoured(self):
        payload = self.preflight(strategy="rebase")
        self.assertEqual("rebase", payload["strategy"])
        self.assertEqual("rebase", self.saved()["attempt"]["strategy"])

    def test_a_native_stack_is_routed_to_a_cascade(self):
        detection = {
            "default_branch": "main",
            "stack": {
                "id": "S_1",
                "number": 19578,
                "size": 2,
                "trunk": "main",
                "members": [
                    {
                        "position": 0,
                        "number": 19483,
                        "head_branch": "v143",
                        "base_branch": "main",
                        "mergeable": "MERGEABLE",
                        "head_sha": "aaa",
                        "base_sha": "base1",
                    },
                    {
                        "position": 1,
                        "number": 7,
                        "head_branch": "feature",
                        "base_branch": "v143",
                        "mergeable": "CONFLICTING",
                        "head_sha": "head1",
                        "base_sha": "old-a",
                        "commits": ["head1"],
                        "commits_complete": True,
                        "merged_predecessor": {
                            "number": 5,
                            "head_branch": "old-parent",
                            "head_sha": "parent-head",
                            "merge_sha": "old-a",
                            "commits": ["parent-head"],
                            "commits_complete": True,
                        },
                    },
                ],
            },
        }
        payload = self.preflight(detection=detection)
        self.assertEqual("stack_rebase", payload["result"])
        self.assertIsNone(payload["escalation"])
        state = self.saved()
        attempt = state["attempt"]
        self.assertEqual("planned", attempt["status"])
        self.assertEqual("stack", attempt["strategy"])
        self.assertEqual("main", attempt["stack"]["trunk"])
        self.assertEqual(7, attempt["stack"]["invoked_number"])
        self.assertEqual(
            [19483, 7], [member["number"] for member in attempt["stack"]["members"]]
        )
        # The baseline head SHAs are captured so publish can prove what moved.
        self.assertEqual("aaa", attempt["stack"]["members"][0]["head_sha"])
        frozen_child = attempt["stack"]["members"][1]
        self.assertTrue(frozen_child["commits_complete"])
        self.assertEqual(
            ["parent-head"], frozen_child["merged_predecessor"]["commits"]
        )

    def test_whole_stack_preflight_records_clean_members_without_a_cascade(self):
        members = [
            {
                "position": 0,
                "number": 19483,
                "head_branch": "v143",
                "base_branch": "main",
                "head_sha": "aaa",
                "base_sha": "base1",
            },
            {
                "position": 1,
                "number": 7,
                "head_branch": "feature",
                "base_branch": "v143",
                "head_sha": "head1",
                "base_sha": "aaa",
            },
        ]
        detection = {
            "default_branch": "main",
            "stack": {
                "id": "S_1",
                "number": 19578,
                "size": 2,
                "trunk": "main",
                "members": members,
            },
        }

        payload = self.preflight(
            metadata=pr_metadata(mergeable="MERGEABLE"),
            detection=detection,
            whole_stack=True,
            whole_stack_mergeable=True,
        )

        self.assertEqual("stack_mergeable", payload["result"])
        self.assertEqual("mergeable", self.saved()["attempt"]["status"])
        self.record_stack_clearances.assert_called_once_with(
            mock.ANY, members, 7
        )

    def test_a_native_stack_reads_its_default_branch_from_the_api(self):
        # The trunk here is not the repository default branch, and neither is
        # named `main`; the routing must still send a native stack to the cascade
        # rather than assuming any particular branch name.
        detection = {
            "default_branch": "develop",
            "stack": {
                "id": "S_2",
                "number": 40,
                "size": 1,
                "trunk": "release",
                "members": [
                    {
                        "position": 0,
                        "number": 7,
                        "head_branch": "feature",
                        "base_branch": "release",
                        "mergeable": "CONFLICTING",
                        "head_sha": "head1",
                        "base_sha": "base1",
                    }
                ],
            },
        }
        payload = self.preflight(
            metadata=pr_metadata(base_branch="release"), detection=detection
        )
        self.assertEqual("stack_rebase", payload["result"])
        self.assertEqual("develop", payload["default_branch"])

    def test_a_native_stack_with_no_head_branch_still_escalates(self):
        # A push-safety blocker is degenerate pull request metadata, not an
        # artifact of the single-branch refspec, so it must fire on a native
        # stack too rather than being skipped into the cascade.
        payload = self.preflight(
            metadata=pr_metadata(head_branch=None),
            detection=native_stack_detection(),
        )
        self.assertEqual("unsafe_push", payload["result"])
        self.assertEqual("unsafe_push", payload["escalation"]["kind"])
        self.assertIn("no head branch", payload["push_blockers"][0])
        self.assertEqual("escalated", self.saved()["attempt"]["status"])

    def test_a_native_stack_whose_head_is_its_base_still_escalates(self):
        # The head branch and base branch are the same ref in the upstream
        # repository, so a push would write to the base branch. That is broken
        # however it is pushed, cascade or not.
        payload = self.preflight(
            metadata=pr_metadata(head_owner="owner", base_branch="feature"),
            detection=native_stack_detection(),
        )
        self.assertEqual("unsafe_push", payload["result"])
        self.assertEqual("unsafe_push", payload["escalation"]["kind"])
        self.assertIn("write to the base branch", payload["push_blockers"][0])
        self.assertEqual("escalated", self.saved()["attempt"]["status"])

    def test_a_native_stack_stacked_on_its_own_head_still_escalates(self):
        # The declared base resolves to this same head branch through an open
        # pull request, so a push cannot be safe regardless of the stack.
        relations = {
            "dependents": [],
            "stacked_on": {"number": 3, "head_branch": "feature"},
        }
        payload = self.preflight(
            relations=relations, detection=native_stack_detection()
        )
        self.assertEqual("unsafe_push", payload["result"])
        self.assertEqual("unsafe_push", payload["escalation"]["kind"])
        self.assertIn("#3", payload["push_blockers"][0])
        self.assertEqual("escalated", self.saved()["attempt"]["status"])

    def test_a_native_stack_with_an_external_dependent_escalates(self):
        # An open pull request based on a branch the cascade will force-push,
        # but which is not a stack member, would be orphaned. The user approved
        # rewriting the stack's own members, not this one, so the cascade is
        # refused and the dependent is named rather than silently orphaned.
        external = [
            {
                "number": 42,
                "url": "https://github.com/owner/repo/pull/42",
                "head_branch": "outside",
                "base_branch": "v143",
            }
        ]
        payload = self.preflight(
            detection=native_stack_detection(), external=external
        )
        self.assertEqual("stack_external_dependents", payload["result"])
        self.assertEqual(
            "stack_external_dependents", payload["escalation"]["kind"]
        )
        self.assertIn("#42", payload["escalation"]["reason"])
        self.assertIn("v143", payload["escalation"]["reason"])
        self.assertEqual(external, payload["external_dependents"])
        self.assertEqual("escalated", self.saved()["attempt"]["status"])

    def test_an_ad_hoc_base_escalates_and_names_the_conflict(self):
        # base_branch differs from the default branch and there is no native
        # stack, so GitHub measures mergeability against a branch this resolver would
        # not merge in. The escalation names the real conflict.
        detection = {"default_branch": "develop", "stack": None}
        payload = self.preflight(
            metadata=pr_metadata(base_branch="feature-base"), detection=detection
        )
        self.assertEqual("ad_hoc_base", payload["result"])
        self.assertEqual("ad_hoc_base", payload["escalation"]["kind"])
        self.assertIn("docs/list.yaml", payload["escalation"]["reason"])
        self.assertEqual("escalated", self.saved()["attempt"]["status"])

    def test_a_default_branch_base_still_merges_even_when_not_main(self):
        # The declared base is the repository default branch, so the existing
        # merge path is correct even though the default branch is not `main`.
        detection = {"default_branch": "develop", "stack": None}
        payload = self.preflight(
            metadata=pr_metadata(base_branch="develop"), detection=detection
        )
        self.assertEqual("ready", payload["result"])
        self.assertEqual("merge", payload["strategy"])

    def test_the_default_state_path_is_used_when_none_is_given(self):
        args = SimpleNamespace(
            target="owner/repo#7",
            repo_root=None,
            state=None,
            strategy="auto",
            max_attempts=5,
        )
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "resolve_repo_root", return_value=self.directory
        ), mock.patch.object(MODULE, "require_clean_worktree"), mock.patch.object(
            MODULE, "require_no_integration_in_progress"
        ), mock.patch.object(
            MODULE, "live_mergeability", return_value=pr_metadata()
        ), mock.patch.object(
            MODULE, "checkout_pr_branch"
        ), mock.patch.object(
            MODULE, "stack_relations", return_value=dict(NO_RELATIONS)
        ), mock.patch.object(
            MODULE,
            "stack_membership",
            return_value={"default_branch": "main", "stack": None},
        ), mock.patch.object(
            MODULE, "repository_merge_methods", return_value=dict(ALL_MERGE_METHODS)
        ), mock.patch.object(
            MODULE, "default_state_path", return_value=self.directory / "default.json"
        ), mock.patch.object(
            MODULE, "emit"
        ) as emit:
            MODULE.command_preflight(args)
        self.assertEqual(
            str(self.directory / "default.json"), emitted(emit)["state"]
        )


def fake_git(
    head="head1",
    branch="feature",
    merge_base="merge0",
    merge_head="base1",
    heads=None,
):
    remaining = list(heads) if heads else None

    def call(_root, *arguments):
        if arguments[:2] == ("branch", "--show-current"):
            return branch
        if arguments[:2] == ("rev-parse", "HEAD"):
            if remaining:
                return remaining.pop(0)
            return head
        if arguments[:2] == ("rev-parse", "MERGE_HEAD"):
            return merge_head
        if arguments[0] == "merge-base":
            return merge_base
        raise AssertionError(f"unexpected git call: {arguments}")

    return call


class AttemptTest(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory(self)

    def attempt(
        self,
        *,
        conflicts=None,
        process=None,
        merging=True,
        ancestor=False,
        git_call=None,
        **state_overrides,
    ):
        state_path = write_state(self.directory, **state_overrides)
        self.state_path = state_path
        args = SimpleNamespace(state=str(state_path))
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "require_clean_worktree"
        ), mock.patch.object(
            MODULE, "require_no_integration_in_progress"
        ), mock.patch.object(
            MODULE, "git", side_effect=git_call or fake_git()
        ), mock.patch.object(
            MODULE, "find_remote", return_value="origin"
        ), mock.patch.object(
            MODULE, "fetch_reference"
        ) as fetch, mock.patch.object(
            MODULE, "commit_subjects", return_value=["Add a thing"]
        ), mock.patch.object(
            MODULE, "is_ancestor", return_value=ancestor
        ), mock.patch.object(
            MODULE, "start_integration", return_value=process or completed(1, "", "CONFLICT")
        ) as start, mock.patch.object(
            MODULE, "collect_conflicts", return_value=list(conflicts or [])
        ), mock.patch.object(
            MODULE, "merge_in_progress", return_value=merging
        ), mock.patch.object(
            MODULE, "rebase_in_progress", return_value=False
        ), mock.patch.object(
            MODULE, "git_try", return_value=completed(0)
        ), mock.patch.object(
            MODULE, "emit"
        ) as emit:
            MODULE.command_attempt(args)
        self.fetch = fetch
        self.start = start
        return emitted(emit)

    def saved(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def planned(self, **overrides):
        return attempt_record(
            status="planned", conflicts=[], conflict_signature=None, merge_base=None, **overrides
        )

    def test_a_conflicted_attempt_reports_every_file(self):
        conflicts = [conflict_record("a.py"), conflict_record("logo.png", binary=True)]
        payload = self.attempt(conflicts=conflicts, attempt=self.planned())
        self.assertEqual("conflicted", payload["result"])
        self.assertEqual(["a.py", "logo.png"], payload["conflict_paths"])
        self.assertEqual(2, payload["counts"]["conflicts"])
        self.assertEqual(1, payload["counts"]["binary"])
        state = self.saved()
        self.assertEqual("conflicted", state["attempt"]["status"])
        self.assertEqual("merge0", state["attempt"]["merge_base"])
        self.assertEqual(["Add a thing"], state["attempt"]["original_subjects"])
        self.assertEqual(
            MODULE.conflict_signature(["a.py", "logo.png"]),
            state["attempt"]["conflict_signature"],
        )
        self.fetch.assert_called_once()

    def test_the_conflicts_file_carries_the_full_evidence(self):
        payload = self.attempt(conflicts=[conflict_record("a.py")], attempt=self.planned())
        detail = json.loads(Path(payload["conflicts_path"]).read_text(encoding="utf-8"))
        self.assertEqual("conflicted", detail["result"])
        self.assertEqual("a.py", detail["conflicts"][0]["path"])
        self.assertEqual(pr_metadata(), detail["pr"])

    def test_a_repeated_conflict_set_still_reports_the_conflict(self):
        """A run only integrates once, so a repeat is a first attempt here.

        Reading the history as exhaustion would escalate a run that has not
        tried anything yet.
        """
        signature = MODULE.conflict_signature(["a.py"])
        history = [
            {"id": "pr-7-attempt-1", "conflict_signature": signature},
            {"id": "pr-7-attempt-2", "conflict_signature": signature},
        ]
        payload = self.attempt(
            conflicts=[conflict_record("a.py")],
            attempt=self.planned(attempt_number=3),
            history=history,
        )
        self.assertEqual("conflicted", payload["result"])
        self.assertIsNone(payload["escalation"])
        self.assertEqual("conflicted", self.saved()["attempt"]["status"])

    def test_a_clean_merge_moves_on_to_continue(self):
        payload = self.attempt(
            process=completed(0, "Automatic merge went well"), attempt=self.planned()
        )
        self.assertEqual("no_conflicts", payload["result"])
        self.assertEqual("continue", payload["next"])
        self.assertEqual("integrated", self.saved()["attempt"]["status"])

    def test_a_clean_rebase_moves_on_to_publish(self):
        payload = self.attempt(
            process=completed(0),
            attempt=self.planned(strategy="rebase"),
            merging=False,
            git_call=fake_git(heads=["head1", "rebased1"]),
        )
        self.assertEqual("no_conflicts", payload["result"])
        self.assertEqual("publish", payload["next"])
        saved = self.saved()["attempt"]
        self.assertEqual("resolved", saved["status"])
        self.assertEqual("rebased1", saved["integration_head_sha"])

    def test_a_successful_rebase_checkpoints_its_result_atomically(self):
        state_path = write_state(
            self.directory, attempt=self.planned(strategy="rebase")
        )
        real_save_state = MODULE.save_state
        save_count = 0

        def interrupted_save(path, state):
            nonlocal save_count
            save_count += 1
            real_save_state(path, state)
            if save_count == 2:
                raise MODULE.WorkflowError("interrupted after checkpoint")

        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "require_clean_worktree"
        ), mock.patch.object(
            MODULE, "require_no_integration_in_progress"
        ), mock.patch.object(
            MODULE, "git", side_effect=fake_git(heads=["head1", "rebased1"])
        ), mock.patch.object(
            MODULE, "find_remote", return_value="origin"
        ), mock.patch.object(
            MODULE, "fetch_reference"
        ), mock.patch.object(
            MODULE, "commit_subjects", return_value=["Add a thing"]
        ), mock.patch.object(
            MODULE, "is_ancestor", return_value=False
        ), mock.patch.object(
            MODULE, "start_integration", return_value=completed(0)
        ), mock.patch.object(
            MODULE, "save_state", side_effect=interrupted_save
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "interrupted after checkpoint"
            ):
                MODULE.command_attempt(SimpleNamespace(state=str(state_path)))

        saved = json.loads(state_path.read_text(encoding="utf-8"))["attempt"]
        self.assertEqual(0, saved["integration_returncode"])
        self.assertEqual("rebased1", saved["integration_head_sha"])

    def test_a_base_already_in_the_head_escalates_as_a_contradiction(self):
        # is_ancestor is decided before the merge runs, so a merge that would
        # succeed (merging in progress, clean exit) must still escalate: the
        # base tip is already in the head and GitHub's conflict flag is stale.
        payload = self.attempt(
            process=completed(0),
            merging=True,
            ancestor=True,
            attempt=self.planned(),
        )
        self.assertEqual("already_integrated", payload["result"])
        self.assertEqual("contradiction", payload["escalation"]["kind"])
        self.assertIn("already an ancestor", payload["escalation"]["reason"])
        self.assertIn("base1", payload["escalation"]["reason"])
        self.assertIn("head1", payload["escalation"]["reason"])
        state = self.saved()
        self.assertEqual("escalated", state["attempt"]["status"])
        self.assertEqual(1, len(state["history"]))
        self.start.assert_not_called()

    def test_a_base_already_in_a_rebased_head_also_escalates(self):
        payload = self.attempt(
            process=completed(0),
            ancestor=True,
            attempt=self.planned(strategy="rebase"),
            merging=False,
        )
        self.assertEqual("already_integrated", payload["result"])
        self.assertEqual("contradiction", payload["escalation"]["kind"])
        self.assertIn("already an ancestor", payload["escalation"]["reason"])
        self.assertEqual("escalated", self.saved()["attempt"]["status"])
        self.start.assert_not_called()

    def test_a_failure_with_no_conflicted_file_is_reported(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "without leaving a conflicted file"):
            self.attempt(process=completed(128, "", "fatal: bad object"), attempt=self.planned())
        self.assertEqual("planned", self.saved()["attempt"]["status"])

    def test_an_interrupted_status_read_preserves_a_started_merge_for_retry(self):
        state_path = write_state(self.directory, attempt=self.planned())
        args = SimpleNamespace(state=str(state_path))
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "require_clean_worktree"
        ), mock.patch.object(
            MODULE, "require_no_integration_in_progress"
        ), mock.patch.object(
            MODULE, "git", side_effect=fake_git()
        ), mock.patch.object(
            MODULE, "find_remote", return_value="origin"
        ), mock.patch.object(
            MODULE, "fetch_reference"
        ), mock.patch.object(
            MODULE, "commit_subjects", return_value=["Add a thing"]
        ), mock.patch.object(
            MODULE, "is_ancestor", return_value=False
        ), mock.patch.object(
            MODULE, "start_integration", return_value=completed(1, "", "CONFLICT")
        ), mock.patch.object(
            MODULE,
            "collect_conflicts",
            side_effect=MODULE.WorkflowError("could not read status"),
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "could not read status"):
                MODULE.command_attempt(args)

        saved = json.loads(state_path.read_text(encoding="utf-8"))["attempt"]
        self.assertEqual("integrating", saved["status"])
        self.assertEqual(1, saved["integration_returncode"])
        self.assertEqual("CONFLICT", saved["command_output"])

    def test_a_retry_resumes_the_exact_helper_started_merge(self):
        attempt = self.planned()
        attempt.update(
            status="integrating",
            integration_returncode=1,
            command_output="CONFLICT",
        )
        state_path = write_state(self.directory, attempt=attempt)
        args = SimpleNamespace(state=str(state_path))
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "git", side_effect=fake_git()
        ), mock.patch.object(
            MODULE, "merge_in_progress", return_value=True
        ), mock.patch.object(
            MODULE, "rebase_in_progress", return_value=False
        ), mock.patch.object(
            MODULE, "start_integration"
        ) as start, mock.patch.object(
            MODULE, "collect_conflicts", return_value=[conflict_record()]
        ), mock.patch.object(
            MODULE, "emit"
        ) as emit:
            MODULE.command_attempt(args)

        start.assert_not_called()
        self.assertEqual("conflicted", emitted(emit)["result"])
        saved = json.loads(state_path.read_text(encoding="utf-8"))["attempt"]
        self.assertEqual("conflicted", saved["status"])
        self.assertEqual("CONFLICT", saved["command_output"])

    def test_a_retry_rejects_a_different_active_merge(self):
        attempt = self.planned()
        attempt.update(
            status="integrating",
            integration_returncode=1,
            command_output="CONFLICT",
        )
        state_path = write_state(self.directory, attempt=attempt)
        args = SimpleNamespace(state=str(state_path))
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "git", side_effect=fake_git(merge_head="other-base")
        ), mock.patch.object(
            MODULE, "merge_in_progress", return_value=True
        ), mock.patch.object(
            MODULE, "rebase_in_progress", return_value=False
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "MERGE_HEAD base1"):
                MODULE.command_attempt(args)

    def test_a_retry_checks_the_active_rebase_before_resuming_it(self):
        attempt = self.planned(strategy="rebase")
        attempt.update(
            status="integrating",
            integration_returncode=1,
            command_output="CONFLICT",
        )
        state_path = write_state(self.directory, attempt=attempt)
        args = SimpleNamespace(state=str(state_path))
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "git", side_effect=fake_git()
        ), mock.patch.object(
            MODULE, "merge_in_progress", return_value=False
        ), mock.patch.object(
            MODULE, "rebase_in_progress", return_value=True
        ), mock.patch.object(
            MODULE, "require_owned_rebase"
        ) as require_owned, mock.patch.object(
            MODULE, "start_integration"
        ) as start, mock.patch.object(
            MODULE, "collect_conflicts", return_value=[conflict_record()]
        ), mock.patch.object(
            MODULE, "emit"
        ):
            MODULE.command_attempt(args)

        require_owned.assert_called_once()
        self.assertEqual(self.directory, require_owned.call_args.args[0])
        self.assertEqual("head1", require_owned.call_args.args[1]["head_sha"])
        self.assertEqual("base1", require_owned.call_args.args[1]["base_sha"])
        start.assert_not_called()

    def test_a_retry_rejects_an_unrecorded_completed_rebase(self):
        attempt = self.planned(strategy="rebase")
        attempt.update(status="integrating", integration_returncode=0)
        state_path = write_state(self.directory, attempt=attempt)
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "merge_in_progress", return_value=False
        ), mock.patch.object(
            MODULE, "rebase_in_progress", return_value=False
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "no recorded result commit"):
                MODULE.command_attempt(SimpleNamespace(state=str(state_path)))

    def test_a_retry_rejects_a_different_completed_rebase_head(self):
        attempt = self.planned(strategy="rebase")
        attempt.update(
            status="integrating",
            integration_returncode=0,
            integration_head_sha="rebased1",
        )
        state_path = write_state(self.directory, attempt=attempt)
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "require_clean_worktree"
        ), mock.patch.object(
            MODULE, "git", return_value="other-head"
        ), mock.patch.object(
            MODULE, "merge_in_progress", return_value=False
        ), mock.patch.object(
            MODULE, "rebase_in_progress", return_value=False
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "expected HEAD rebased1"):
                MODULE.command_attempt(SimpleNamespace(state=str(state_path)))

    def test_an_attempt_that_already_ran_is_refused(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "already conflicted"):
            self.attempt(attempt=attempt_record(status="conflicted"))

    def test_a_state_with_no_attempt_is_refused(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "run preflight first"):
            self.attempt(attempt=None)

    def test_a_branch_that_moved_away_is_refused(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "branch mismatch"):
            self.attempt(attempt=self.planned(), git_call=fake_git(branch="other"))

    def test_a_detached_worktree_integrates_like_an_attached_one(self):
        payload = self.attempt(
            conflicts=[conflict_record("a.py")],
            attempt=self.planned(),
            git_call=fake_git(branch=""),
        )
        self.assertEqual("conflicted", payload["result"])

    def test_a_head_that_moved_away_is_refused(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "HEAD mismatch"):
            self.attempt(attempt=self.planned(), git_call=fake_git(head="local9"))


class StartIntegrationTest(unittest.TestCase):
    def test_a_merge_stops_before_the_commit(self):
        with mock.patch.object(MODULE, "git_try", return_value=completed(0)) as git_try:
            MODULE.start_integration(Path("."), attempt_record(strategy="merge"), "base1")
        self.assertEqual(
            ("merge", "--no-commit", "--no-ff", "base1"), git_try.call_args[0][1:]
        )

    def test_a_rebase_runs_with_no_interactive_editor(self):
        with mock.patch.object(MODULE, "run", return_value=completed(0)) as runner:
            MODULE.start_integration(Path("."), attempt_record(strategy="rebase"), "base1")
        command = runner.call_args[0][0]
        self.assertEqual(["rebase", "base1"], command[-2:])
        environment = runner.call_args[1]["env"]
        self.assertEqual("true", environment["GIT_EDITOR"])
        self.assertEqual("true", environment["GIT_SEQUENCE_EDITOR"])


class RequireOwnedRebaseTest(unittest.TestCase):
    def test_the_rebase_metadata_must_match_the_frozen_attempt(self):
        directory = temporary_directory(self)
        rebase = directory / "rebase-merge"
        rebase.mkdir()
        (rebase / "orig-head").write_text("other-head\n", encoding="utf-8")
        (rebase / "onto").write_text("base1\n", encoding="utf-8")

        def path_lookup(_repo_root, *_args):
            return completed(0, str(directory / _args[-1]))

        with mock.patch.object(MODULE, "git_try", side_effect=path_lookup):
            with self.assertRaisesRegex(MODULE.WorkflowError, "unrecognized rebase"):
                MODULE.require_owned_rebase(
                    directory, attempt_record(head_sha="head1", base_sha="base1")
                )


class ResolvedTest(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory(self)
        self.blobs = {"ancestor": b"ancestor\n", "head": b"ours\n", "base": b"theirs\n"}

    def write(self, name, content):
        path = self.directory / name
        path.write_bytes(content)
        return path

    def resolve(
        self,
        *,
        paths=("app.py",),
        companion_paths=(),
        rationale="kept the rename and the new call",
        rationale_file=None,
        accept_one_side=False,
        accept_deletion=False,
        accept_line_endings=False,
        add=None,
        **state_overrides,
    ):
        state_path = write_state(self.directory, **state_overrides)
        args = SimpleNamespace(
            state=str(state_path),
            paths=list(paths),
            companion_paths=list(companion_paths),
            rationale=rationale,
            rationale_file=rationale_file,
            accept_one_side=accept_one_side,
            accept_deletion=accept_deletion,
            accept_line_endings=accept_line_endings,
        )
        with mock.patch.object(
            MODULE, "stage_blobs", return_value=dict(self.blobs)
        ), mock.patch.object(
            MODULE, "git_try", return_value=add or completed(0)
        ) as git_try, mock.patch.object(
            MODULE, "require_owned_active_integration"
        ) as require_owned, mock.patch.object(
            MODULE, "emit"
        ) as emit:
            MODULE.command_resolved(args)
        self.state_path = state_path
        self.git_try = git_try
        self.require_owned = require_owned
        return emitted(emit)

    def saved(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_a_combined_resolution_is_recorded_and_staged(self):
        self.write("app.py", b"ours\ntheirs\n")
        payload = self.resolve()
        self.assertEqual("recorded", payload["result"])
        self.assertEqual([], payload["remaining_conflicts"])
        self.assertEqual("continue", payload["next"])
        self.assertEqual(
            [{"path": "app.py", "one_side": None, "deleted": False}], payload["resolved"]
        )
        conflict = self.saved()["attempt"]["conflicts"][0]
        self.assertEqual("resolved", conflict["status"])
        self.assertEqual("kept the rename and the new call", conflict["rationale"])
        self.assertIsNone(conflict["one_side"])
        self.assertEqual(
            ("add", "--all", "--", ":(literal)app.py"),
            self.git_try.call_args[0][1:],
        )
        self.require_owned.assert_called_once()

    def test_resolved_records_files_for_descendant_propagation_state(self):
        self.write("app.py", b"ours\ntheirs\n")
        payload = self.resolve(
            operation="descendant_propagation",
            status="conflicted",
            workspace=str(self.directory),
            conflicts=[conflict_record("app.py")],
            cascade={},
        )
        self.assertEqual("recorded", payload["result"])
        saved = self.saved()
        self.assertEqual("resolved", saved["conflicts"][0]["status"])
        self.assertEqual([], payload["remaining_conflicts"])

    def test_remaining_conflicts_keep_the_run_on_resolved(self):
        self.write("app.py", b"ours\ntheirs\n")
        payload = self.resolve(
            attempt=attempt_record(
                conflicts=[conflict_record("app.py"), conflict_record("other.py")]
            )
        )
        self.assertEqual(["other.py"], payload["remaining_conflicts"])
        self.assertEqual("resolved", payload["next"])

    def test_a_file_that_still_holds_markers_is_refused(self):
        self.write("app.py", b"<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> base\n")
        with self.assertRaisesRegex(MODULE.WorkflowError, "still holds conflict markers"):
            self.resolve()

    def test_a_file_with_a_broken_region_is_refused(self):
        self.write("app.py", b"ours\n>>>>>>> base\n")
        with self.assertRaisesRegex(MODULE.WorkflowError, "closed unopened"):
            self.resolve()

    def test_keeping_only_one_side_is_refused_by_default(self):
        self.write("app.py", b"ours\n")
        with self.assertRaisesRegex(MODULE.WorkflowError, "byte-for-byte the head side"):
            self.resolve()
        self.write("app.py", b"theirs\n")
        with self.assertRaisesRegex(MODULE.WorkflowError, "byte-for-byte the base side"):
            self.resolve()

    def test_line_endings_do_not_disguise_one_side(self):
        self.write("app.py", b"ours\r\n")
        with self.assertRaisesRegex(MODULE.WorkflowError, "byte-for-byte the head side"):
            self.resolve()

    def test_keeping_one_side_on_purpose_is_recorded(self):
        self.write("app.py", b"ours\n")
        payload = self.resolve(
            accept_one_side=True, rationale="the base side reverted a fix"
        )
        self.assertEqual([{"path": "app.py", "one_side": "head", "deleted": False}], payload["resolved"])
        self.assertEqual("head", self.saved()["attempt"]["conflicts"][0]["one_side"])

    def test_a_whole_file_rewritten_as_crlf_is_refused(self):
        self.write("app.py", b"ours\r\nboth\r\n")
        with self.assertRaisesRegex(MODULE.WorkflowError, "now uses CRLF line endings"):
            self.resolve()

    def test_a_deliberate_line_ending_change_is_allowed(self):
        self.write("app.py", b"ours\r\nboth\r\n")
        payload = self.resolve(
            accept_line_endings=True, rationale="the file is a Windows batch script"
        )
        self.assertEqual([{"path": "app.py", "one_side": None, "deleted": False}], payload["resolved"])

    def test_matching_the_line_endings_both_sides_used_is_allowed(self):
        self.blobs = {"ancestor": None, "head": b"ours\r\n", "base": b"theirs\r\n"}
        self.write("app.py", b"ours\r\nboth\r\n")
        payload = self.resolve()
        self.assertEqual([{"path": "app.py", "one_side": None, "deleted": False}], payload["resolved"])

    def test_a_deletion_is_refused_by_default(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "needs --accept-deletion"):
            self.resolve()

    def test_a_deliberate_deletion_is_recorded(self):
        payload = self.resolve(accept_deletion=True, rationale="both sides removed it")
        self.assertEqual([{"path": "app.py", "one_side": None, "deleted": True}], payload["resolved"])
        self.assertTrue(self.saved()["attempt"]["conflicts"][0]["deleted"])

    def test_a_binary_resolution_is_compared_by_bytes(self):
        self.blobs = {"ancestor": None, "head": b"\x89PNG\0a", "base": b"\x89PNG\0b"}
        self.write("logo.png", b"\x89PNG\0c")
        payload = self.resolve(
            paths=("logo.png",),
            attempt=attempt_record(conflicts=[conflict_record("logo.png", binary=True)]),
        )
        self.assertEqual([{"path": "logo.png", "one_side": None, "deleted": False}], payload["resolved"])

    def test_a_binary_file_equal_to_one_side_is_refused(self):
        self.blobs = {"ancestor": None, "head": b"\x89PNG\0a", "base": b"\x89PNG\0b"}
        self.write("logo.png", b"\x89PNG\0b")
        with self.assertRaisesRegex(MODULE.WorkflowError, "byte-for-byte the base side"):
            self.resolve(
                paths=("logo.png",),
                attempt=attempt_record(conflicts=[conflict_record("logo.png", binary=True)]),
            )

    def test_the_rationale_can_come_from_a_file(self):
        self.write("app.py", b"ours\ntheirs\n")
        reason = self.directory / "reason.txt"
        reason.write_text("both sides kept\n", encoding="utf-8")
        self.resolve(rationale=None, rationale_file=str(reason))
        self.assertEqual(
            "both sides kept", self.saved()["attempt"]["conflicts"][0]["rationale"]
        )

    def test_a_path_that_is_not_conflicted_is_refused(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "not conflicted in this attempt"):
            self.resolve(paths=("other.py",))

    def test_a_companion_path_from_the_replayed_commit_is_staged_and_recorded(self):
        self.write("app.py", b"ours\ntheirs\n")
        self.write("moved.py", b"preserved base behavior\n")
        with mock.patch.object(
            MODULE,
            "replayed_commit_paths",
            return_value={"app.py", "moved.py"},
        ), mock.patch.object(MODULE, "path_has_unstaged_changes", return_value=True):
            payload = self.resolve(
                companion_paths=("moved.py",),
                attempt=attempt_record(
                    strategy="rebase",
                    conflicts=[conflict_record("app.py")],
                ),
            )
        self.assertEqual([{"path": "moved.py", "deleted": False}], payload["companions"])
        companion = self.saved()["attempt"]["companion_resolutions"][0]
        self.assertEqual("moved.py", companion["path"])
        self.assertEqual("kept the rename and the new call", companion["rationale"])
        self.assertEqual(
            (
                "add",
                "--all",
                "--",
                ":(literal)app.py",
                ":(literal)moved.py",
            ),
            self.git_try.call_args[0][1:],
        )

    def test_a_base_side_replacement_for_a_deleted_conflict_is_a_companion(self):
        self.write("contract.json", b"")
        self.write("contract.yaml", b"preserved replayed behavior\n")
        conflict = conflict_record(
            "contract.json",
            code="UD",
            deletion=True,
            base_commits=[{"sha": "base-change"}],
        )
        with mock.patch.object(
            MODULE,
            "replayed_commit_paths",
            return_value={"contract.json"},
        ), mock.patch.object(
            MODULE,
            "base_replacement_paths",
            return_value={"contract.yaml"},
        ), mock.patch.object(
            MODULE, "path_has_unstaged_changes", return_value=True
        ):
            payload = self.resolve(
                paths=("contract.json",),
                companion_paths=("contract.yaml",),
                accept_deletion=True,
                attempt=attempt_record(strategy="rebase", conflicts=[conflict]),
            )
        self.assertEqual(
            [{"path": "contract.yaml", "deleted": False}], payload["companions"]
        )

    def test_a_companion_path_outside_the_replayed_commit_is_refused(self):
        self.write("app.py", b"ours\ntheirs\n")
        self.write("unrelated.py", b"unrelated edit\n")
        with mock.patch.object(
            MODULE,
            "replayed_commit_paths",
            return_value={"app.py"},
        ), mock.patch.object(
            MODULE,
            "base_replacement_paths",
            return_value=set(),
        ), self.assertRaisesRegex(
            MODULE.WorkflowError,
            "nor proven base-side replacements",
        ):
            self.resolve(
                companion_paths=("unrelated.py",),
                attempt=attempt_record(
                    strategy="rebase",
                    conflicts=[conflict_record("app.py")],
                ),
            )

    def test_a_renamed_base_path_is_a_proven_replacement(self):
        conflict = conflict_record(
            "old/contract.json",
            code="UD",
            deletion=True,
            base_commits=[{"sha": "base-change"}],
        )
        with mock.patch.object(
            MODULE,
            "commit_path_changes",
            return_value=[
                ("R072", "old/contract.json", "new/contract.yaml"),
                ("A", "new/unrelated.md", None),
            ],
        ), mock.patch.object(
            MODULE, "path_is_file_at_head", return_value=True
        ):
            self.assertEqual(
                {"new/contract.yaml"},
                MODULE.base_replacement_paths(self.directory, [conflict]),
            )

    def test_ambiguous_same_stem_additions_are_not_proven_replacements(self):
        conflict = conflict_record(
            "tools/contract.json",
            code="UD",
            deletion=True,
            base_commits=[{"sha": "base-change"}],
        )
        with mock.patch.object(
            MODULE,
            "commit_path_changes",
            return_value=[
                ("D", "tools/contract.json", None),
                ("A", "tools/contract.yaml", None),
                ("A", "tools/contract.yml", None),
            ],
        ), mock.patch.object(
            MODULE, "path_is_file_at_head", return_value=True
        ):
            self.assertEqual(
                set(), MODULE.base_replacement_paths(self.directory, [conflict])
            )

    def test_a_same_stem_addition_in_the_deleting_commit_is_a_replacement(self):
        conflict = conflict_record(
            "tools/contract.json",
            code="UD",
            deletion=True,
            base_commits=[{"sha": "base-change"}],
        )
        with mock.patch.object(
            MODULE,
            "commit_path_changes",
            return_value=[
                ("D", "tools/contract.json", None),
                ("A", "tools/contract.yaml", None),
                ("A", "tools/policy.yaml", None),
            ],
        ), mock.patch.object(
            MODULE, "path_is_file_at_head", return_value=True
        ):
            self.assertEqual(
                {"tools/contract.yaml"},
                MODULE.base_replacement_paths(self.directory, [conflict]),
            )

    def test_empty_stack_metadata_recovers_a_replacement_from_parent_history(self):
        conflict = conflict_record(
            "tools/contract.json",
            code="DU",
            deletion=True,
            base_commits=[],
        )
        with mock.patch.object(
            MODULE,
            "commits_touching",
            return_value=[{"sha": "parent-merge"}],
        ) as commits, mock.patch.object(
            MODULE,
            "commit_path_changes",
            return_value=[
                ("D", "tools/contract.json", None),
                ("A", "tools/contract.yaml", None),
            ],
        ), mock.patch.object(
            MODULE, "path_is_file_at_head", return_value=True
        ):
            self.assertEqual(
                {"tools/contract.yaml"},
                MODULE.base_replacement_paths(
                    self.directory,
                    [conflict],
                    history_ranges=["base..rewritten-parent"],
                ),
            )
        commits.assert_called_once_with(
            self.directory,
            "base..rewritten-parent",
            "tools/contract.json",
            include_merges=True,
        )

    def test_recorded_base_commits_do_not_expand_into_parent_history(self):
        conflict = conflict_record(
            "tools/contract.json",
            code="UD",
            deletion=True,
            base_commits=[{"sha": "base-change"}],
        )
        with mock.patch.object(
            MODULE, "commits_touching"
        ) as commits, mock.patch.object(
            MODULE,
            "commit_path_changes",
            return_value=[
                ("D", "tools/contract.json", None),
                ("A", "tools/contract.yaml", None),
            ],
        ), mock.patch.object(
            MODULE, "path_is_file_at_head", return_value=True
        ):
            self.assertEqual(
                {"tools/contract.yaml"},
                MODULE.base_replacement_paths(
                    self.directory,
                    [conflict],
                    history_ranges=["base..rewritten-parent"],
                ),
            )
        commits.assert_not_called()

    def test_recorded_base_commits_keep_each_explicit_replacement(self):
        conflict = conflict_record(
            "tools/contract.json",
            code="UD",
            deletion=True,
            base_commits=[
                {"sha": "yaml-replacement"},
                {"sha": "yml-replacement"},
            ],
        )
        with mock.patch.object(
            MODULE,
            "commit_path_changes",
            side_effect=[
                [
                    ("D", "tools/contract.json", None),
                    ("A", "tools/contract.yaml", None),
                ],
                [
                    ("D", "tools/contract.json", None),
                    ("A", "tools/contract.yml", None),
                ],
            ],
        ), mock.patch.object(
            MODULE, "path_is_file_at_head", return_value=True
        ):
            self.assertEqual(
                {"tools/contract.yaml", "tools/contract.yml"},
                MODULE.base_replacement_paths(self.directory, [conflict]),
            )

    def test_parent_history_requires_one_replacement_across_all_commits(self):
        conflict = conflict_record(
            "tools/contract.json",
            code="DU",
            deletion=True,
            base_commits=[],
        )
        with mock.patch.object(
            MODULE,
            "commits_touching",
            return_value=[
                {"sha": "yaml-replacement"},
                {"sha": "yml-replacement"},
            ],
        ), mock.patch.object(
            MODULE,
            "commit_path_changes",
            side_effect=[
                [
                    ("D", "tools/contract.json", None),
                    ("A", "tools/contract.yaml", None),
                ],
                [
                    ("D", "tools/contract.json", None),
                    ("A", "tools/contract.yml", None),
                ],
            ],
        ), mock.patch.object(
            MODULE, "path_is_file_at_head", return_value=True
        ):
            self.assertEqual(
                set(),
                MODULE.base_replacement_paths(
                    self.directory,
                    [conflict],
                    history_ranges=["base..rewritten-parent"],
                ),
            )

    def test_parent_history_rejects_a_distant_rename_destination(self):
        conflict = conflict_record(
            "tools/contract.json",
            code="DU",
            deletion=True,
            base_commits=[],
        )
        with mock.patch.object(
            MODULE,
            "commits_touching",
            return_value=[{"sha": "distant-rename"}],
        ), mock.patch.object(
            MODULE,
            "commit_path_changes",
            return_value=[
                (
                    "R100",
                    "tools/contract.json",
                    "archive/unrelated.json",
                ),
            ],
        ), mock.patch.object(
            MODULE, "path_is_file_at_head", return_value=True
        ):
            self.assertEqual(
                set(),
                MODULE.base_replacement_paths(
                    self.directory,
                    [conflict],
                    history_ranges=["base..rewritten-parent"],
                ),
            )

    def test_parent_history_uses_a_same_stem_addition_after_an_obsolete_rename(self):
        conflict = conflict_record(
            "tools/contract.json",
            code="DU",
            deletion=True,
            base_commits=[],
        )
        with mock.patch.object(
            MODULE,
            "commits_touching",
            return_value=[{"sha": "rename-and-replacement"}],
        ), mock.patch.object(
            MODULE,
            "commit_path_changes",
            return_value=[
                (
                    "R100",
                    "tools/contract.json",
                    "archive/unrelated.json",
                ),
                ("A", "tools/contract.yaml", None),
            ],
        ), mock.patch.object(
            MODULE,
            "path_is_file_at_head",
            side_effect=lambda _repo, path: path == "tools/contract.yaml",
        ):
            self.assertEqual(
                {"tools/contract.yaml"},
                MODULE.base_replacement_paths(
                    self.directory,
                    [conflict],
                    history_ranges=["base..rewritten-parent"],
                ),
            )

    def test_stack_replacement_history_is_bounded_to_parent_lines(self):
        repo = temporary_directory(self)
        GitTestCase.git_in(repo, "init", "--initial-branch", "main", "--quiet")
        GitTestCase.git_in(repo, "config", "user.name", "Resolver Test")
        GitTestCase.git_in(repo, "config", "user.email", "resolver@example.invalid")
        GitTestCase.write_in(repo, "base.txt", "base\n")
        GitTestCase.commit_in(repo, "base")
        parent_base = GitTestCase.git_in(repo, "rev-parse", "HEAD")
        GitTestCase.git_in(repo, "checkout", "-b", "frozen-parent")
        GitTestCase.write_in(repo, "parent.txt", "parent\n")
        GitTestCase.commit_in(repo, "parent")
        frozen_parent = GitTestCase.git_in(repo, "rev-parse", "HEAD")
        GitTestCase.git_in(repo, "checkout", "main")
        GitTestCase.write_in(repo, "trunk.txt", "trunk\n")
        GitTestCase.commit_in(repo, "advance trunk")
        rewritten_trunk = GitTestCase.git_in(repo, "rev-parse", "HEAD")
        GitTestCase.git_in(repo, "checkout", "-b", "rewritten-parent")
        GitTestCase.git_in(repo, "cherry-pick", frozen_parent)
        attempt = attempt_record(
            strategy="stack",
            stack={
                "current_index": 1,
                "plan": [
                    {
                        "old_base": parent_base,
                        "new_base_ref": rewritten_trunk,
                        "head_sha": frozen_parent,
                        "branch_ref": "refs/heads/rewritten-parent",
                    },
                    {
                        "old_base": frozen_parent,
                        "new_base_ref": "refs/heads/rewritten-parent",
                    },
                ],
            },
        )

        self.assertEqual(
            [
                f"{parent_base}..{frozen_parent}",
                f"{parent_base}..{rewritten_trunk}",
                f"{rewritten_trunk}..refs/heads/rewritten-parent",
            ],
            MODULE.stack_replacement_history_ranges(repo, attempt),
        )

    def test_a_same_stem_addition_in_another_directory_is_not_a_replacement(self):
        conflict = conflict_record(
            "tools/contract.json",
            code="UD",
            deletion=True,
            base_commits=[{"sha": "base-change"}],
        )
        with mock.patch.object(
            MODULE,
            "commit_path_changes",
            return_value=[
                ("D", "tools/contract.json", None),
                ("A", "archive/contract.yaml", None),
            ],
        ), mock.patch.object(
            MODULE, "path_is_file_at_head", return_value=True
        ):
            self.assertEqual(
                set(), MODULE.base_replacement_paths(self.directory, [conflict])
            )

    def test_an_obsolete_rename_destination_is_not_a_replacement(self):
        conflict = conflict_record(
            "old/contract.json",
            code="UD",
            deletion=True,
            base_commits=[{"sha": "base-change"}],
        )
        with mock.patch.object(
            MODULE,
            "commit_path_changes",
            return_value=[
                ("R100", "old/contract.json", "intermediate/contract.json"),
            ],
        ), mock.patch.object(
            MODULE, "path_is_file_at_head", return_value=False
        ):
            self.assertEqual(
                set(), MODULE.base_replacement_paths(self.directory, [conflict])
            )

    def test_a_base_commit_that_did_not_delete_the_conflict_proves_nothing(self):
        conflict = conflict_record(
            "tools/contract.json",
            code="UU",
            deletion=False,
            base_commits=[{"sha": "base-change"}],
        )
        with mock.patch.object(MODULE, "commit_path_changes") as inspect:
            self.assertEqual(
                set(), MODULE.base_replacement_paths(self.directory, [conflict])
            )
        inspect.assert_not_called()

    def test_merge_replacement_changes_are_read_against_the_first_parent(self):
        repo = temporary_directory(self)
        self.assertEqual(
            "",
            GitTestCase.git_in(repo, "init", "--initial-branch", "main", "--quiet"),
        )
        GitTestCase.git_in(repo, "config", "user.name", "Resolver Test")
        GitTestCase.git_in(repo, "config", "user.email", "resolver@example.invalid")
        tools = repo / "tools"
        tools.mkdir()
        GitTestCase.write_in(repo, "tools/contract.json", "{}\n")
        GitTestCase.commit_in(repo, "add contract")
        GitTestCase.git_in(repo, "checkout", "-b", "replacement")
        (tools / "contract.json").unlink()
        GitTestCase.write_in(repo, "tools/contract.yaml", "ready: true\n")
        GitTestCase.commit_in(repo, "replace contract")
        GitTestCase.git_in(repo, "checkout", "main")
        GitTestCase.write_in(repo, "main.txt", "main\n")
        GitTestCase.commit_in(repo, "advance main")
        GitTestCase.git_in(
            repo,
            "merge",
            "--no-ff",
            "--no-gpg-sign",
            "replacement",
            "--message",
            "merge replacement",
        )
        merge = GitTestCase.git_in(repo, "rev-parse", "HEAD")

        self.assertEqual(
            [
                ("D", "tools/contract.json", None),
                ("A", "tools/contract.yaml", None),
            ],
            MODULE.commit_path_changes(repo, merge),
        )

    def test_descendant_conflicts_receive_the_preserved_cascade_plan(self):
        self.write("contract.yaml", b"preserved query\n")
        cascade = {
            "current_index": 1,
            "plan": [
                {
                    "old_base": "parent-base",
                    "head_sha": "parent-head",
                    "branch_ref": "refs/heads/parent",
                },
                {
                    "old_base": "parent-head",
                    "new_base_ref": "refs/heads/parent",
                },
            ],
        }
        with mock.patch.object(
            MODULE, "replayed_commit_paths", return_value={"contract.json"}
        ), mock.patch.object(
            MODULE,
            "stack_replacement_history_ranges",
            return_value=["base..parent"],
        ) as history, mock.patch.object(
            MODULE,
            "base_replacement_paths",
            return_value={"contract.yaml"},
        ) as replacements, mock.patch.object(
            MODULE, "path_has_unstaged_changes", return_value=True
        ):
            self.resolve(
                paths=("contract.json",),
                companion_paths=("contract.yaml",),
                accept_deletion=True,
                operation="descendant_propagation",
                status="conflicted",
                workspace=str(self.directory),
                conflicts=[
                    conflict_record(
                        "contract.json",
                        code="DU",
                        deletion=True,
                    )
                ],
                cascade=cascade,
            )
        propagated_attempt = history.call_args.args[1]
        self.assertEqual(cascade, propagated_attempt["stack"])
        replacements.assert_called_once_with(
            self.directory,
            propagated_attempt["conflicts"],
            history_ranges=["base..parent"],
        )

    def test_a_companion_path_is_refused_for_a_merge(self):
        self.write("app.py", b"ours\ntheirs\n")
        self.write("moved.py", b"companion edit\n")
        with self.assertRaisesRegex(
            MODULE.WorkflowError,
            "only while a rebase is replaying a commit",
        ):
            self.resolve(companion_paths=("moved.py",))

    def test_another_conflicted_path_cannot_be_a_companion(self):
        self.write("app.py", b"ours\ntheirs\n")
        self.write("other.py", b"companion edit\n")
        with self.assertRaisesRegex(
            MODULE.WorkflowError,
            "already recorded as conflicted paths",
        ):
            self.resolve(
                companion_paths=("other.py",),
                attempt=attempt_record(
                    strategy="rebase",
                    conflicts=[
                        conflict_record("app.py"),
                        conflict_record("other.py"),
                    ],
                ),
            )

    def test_companion_pathspecs_are_literal(self):
        self.write("app.py", b"ours\ntheirs\n")
        self.write("[ab].py", b"companion edit\n")
        with mock.patch.object(
            MODULE,
            "replayed_commit_paths",
            return_value={"app.py", "[ab].py"},
        ), mock.patch.object(
            MODULE, "path_has_unstaged_changes", return_value=True
        ):
            self.resolve(
                companion_paths=("[ab].py",),
                attempt=attempt_record(
                    strategy="rebase",
                    conflicts=[conflict_record("app.py")],
                ),
            )
        self.assertEqual(
            (
                "add",
                "--all",
                "--",
                ":(literal)app.py",
                ":(literal)[ab].py",
            ),
            self.git_try.call_args[0][1:],
        )

    def test_replayed_commit_paths_preserve_unicode_names(self):
        with mock.patch.object(
            MODULE,
            "git_try",
            return_value=completed(0, "app.py\0caf\u00e9.py\0"),
        ) as git_try:
            paths = MODULE.replayed_commit_paths(self.directory)
        self.assertEqual({"app.py", "caf\u00e9.py"}, paths)
        self.assertIn("-z", git_try.call_args[0])

    def test_an_attempt_with_no_conflicts_recorded_is_refused(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "no conflicted files are recorded"):
            self.resolve(attempt=attempt_record(status="planned"))

    def test_an_escalated_attempt_can_still_be_resolved(self):
        self.write("app.py", b"ours\ntheirs\n")
        payload = self.resolve(attempt=attempt_record(status="escalated"))
        self.assertEqual("recorded", payload["result"])

    def test_a_staging_failure_is_reported(self):
        self.write("app.py", b"ours\ntheirs\n")
        with self.assertRaisesRegex(MODULE.WorkflowError, "could not stage app.py"):
            self.resolve(add=completed(1, "", "fatal: pathspec"))


class CompanionPathIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.repo = temporary_directory(self)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.write("legacy.py", "legacy_behavior = True\n")
        self.git("add", "legacy.py")
        self.git("commit", "-q", "-m", "base")

        self.git("checkout", "-q", "-b", "feature")
        (self.repo / "legacy.py").unlink()
        self.write(
            "moved.py",
            "def extracted_behavior():\n"
            "    return {'feature': True}\n",
        )
        self.git("add", "--all")
        self.git("commit", "-q", "-m", "extract behavior")

        self.git("checkout", "-q", "main")
        self.write(
            "legacy.py",
            "legacy_behavior = True\n"
            "base_behavior = True\n",
        )
        self.git("commit", "-q", "-am", "extend legacy behavior")
        self.git("checkout", "-q", "feature")
        rebase = self.git_try("rebase", "main")
        self.assertNotEqual(0, rebase.returncode)
        self.assertTrue((self.repo / "legacy.py").exists())
        self.assertTrue((self.repo / "moved.py").exists())

    def git(self, *arguments):
        return subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            check=True,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
            },
        ).stdout.strip()

    def git_try(self, *arguments):
        return subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
            },
        )

    def write(self, path, content):
        (self.repo / path).write_text(content, encoding="utf-8", newline="\n")

    def test_move_destination_can_preserve_the_base_change(self):
        (self.repo / "legacy.py").unlink()
        self.write(
            "moved.py",
            "def extracted_behavior():\n"
            "    return {'feature': True, 'base': True}\n",
        )
        state_path = write_state(
            self.repo,
            repo_root=str(self.repo),
            attempt=attempt_record(
                strategy="rebase",
                head_sha=self.git("rev-parse", "REBASE_HEAD"),
                base_sha=self.git("rev-parse", "main"),
                conflicts=[
                    conflict_record(
                        "legacy.py",
                        code="UD",
                        kind="deleted by them",
                        deletion=True,
                    )
                ],
            ),
        )
        args = SimpleNamespace(
            state=str(state_path),
            paths=["legacy.py"],
            companion_paths=["moved.py"],
            rationale=(
                "the feature moved the behavior into moved.py; the destination keeps "
                "the base branch's added behavior"
            ),
            rationale_file=None,
            accept_one_side=False,
            accept_deletion=True,
            accept_line_endings=False,
        )
        with mock.patch.object(MODULE, "emit") as emit:
            MODULE.command_resolved(args)

        payload = emitted(emit)
        self.assertEqual([{"path": "moved.py", "deleted": False}], payload["companions"])
        self.assertEqual([], payload["remaining_conflicts"])
        staged = set(self.git("diff", "--cached", "--name-only").splitlines())
        self.assertEqual({"legacy.py", "moved.py"}, staged)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        companion = state["attempt"]["companion_resolutions"][0]
        self.assertEqual("moved.py", companion["path"])

    def test_commit_path_changes_parses_the_replayed_move(self):
        changes = MODULE.commit_path_changes(
            self.repo, self.git("rev-parse", "REBASE_HEAD")
        )
        changed_paths = {
            path
            for _status, source, destination in changes
            for path in (source, destination)
            if path
        }
        self.assertEqual({"legacy.py", "moved.py"}, changed_paths)


class ContinueTest(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory(self)

    def resolved_attempt(self, **overrides):
        values = {"conflicts": [conflict_record("app.py", status="resolved")]}
        values.update(overrides)
        return attempt_record(**values)

    def run_continue(
        self,
        *,
        merging=True,
        unmerged=(),
        commit=None,
        process=None,
        conflicts=None,
        rebasing=False,
        **state_overrides,
    ):
        state_path = write_state(self.directory, **state_overrides)
        args = SimpleNamespace(state=str(state_path))
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "unmerged_entries", return_value=[{"path": path} for path in unmerged]
        ), mock.patch.object(
            MODULE, "require_owned_active_integration"
        ) as require_owned, mock.patch.object(
            MODULE, "merge_in_progress", return_value=merging
        ), mock.patch.object(
            MODULE, "git_try", return_value=commit or completed(0)
        ) as git_try, mock.patch.object(
            MODULE, "git", return_value="head2"
        ), mock.patch.object(
            MODULE, "run", return_value=process or completed(0)
        ) as runner, mock.patch.object(
            MODULE, "collect_conflicts", return_value=list(conflicts or [])
        ), mock.patch.object(
            MODULE, "rebase_in_progress", return_value=rebasing
        ), mock.patch.object(
            MODULE, "emit"
        ) as emit:
            MODULE.command_continue(args)
        self.state_path = state_path
        self.git_try = git_try
        self.runner = runner
        self.require_owned = require_owned
        return emitted(emit)

    def saved(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_a_merge_commit_finishes_the_attempt(self):
        payload = self.run_continue(attempt=self.resolved_attempt())
        self.assertEqual("resolved", payload["result"])
        self.assertEqual("head2", payload["resolved_head_sha"])
        self.assertEqual("publish", payload["next"])
        self.assertEqual("resolved", self.saved()["attempt"]["status"])
        self.assertEqual("commit", self.git_try.call_args[0][1])
        self.require_owned.assert_called_once()

    def test_the_merge_message_is_written_to_a_temporary_file_that_is_removed(self):
        self.run_continue(
            attempt=self.resolved_attempt(
                conflicts=[conflict_record("app.py", status="resolved", rationale="kept both")]
            )
        )
        message_path = Path(self.git_try.call_args[0][3])
        self.assertFalse(message_path.exists())

    def test_an_unresolved_conflict_blocks_the_commit(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "not resolved yet"):
            self.run_continue(attempt=attempt_record())

    def test_a_path_git_still_calls_unmerged_blocks_the_commit(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "still reports these paths as unmerged"):
            self.run_continue(attempt=self.resolved_attempt(), unmerged=["app.py"])

    def test_a_merge_that_is_no_longer_in_progress_is_refused(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "no merge is in progress"):
            self.run_continue(attempt=self.resolved_attempt(), merging=False)

    def test_a_merge_cannot_be_completed_from_the_wrong_status(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "cannot be completed from status"):
            self.run_continue(attempt=self.resolved_attempt(status="resolved"))

    def test_a_failed_merge_commit_is_reported(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "could not create the merge commit"):
            self.run_continue(
                attempt=self.resolved_attempt(), commit=completed(1, "", "nothing to commit")
            )

    def test_a_rebase_that_replayed_every_commit_finishes(self):
        payload = self.run_continue(attempt=self.resolved_attempt(strategy="rebase"))
        self.assertEqual("resolved", payload["result"])
        self.assertEqual(["rebase", "--continue"], self.runner.call_args[0][0][-2:])

    def test_a_rebase_that_hit_the_next_conflict_reports_it(self):
        payload = self.run_continue(
            attempt=self.resolved_attempt(strategy="rebase"),
            process=completed(1, "", "CONFLICT (content)"),
            conflicts=[conflict_record("other.py")],
            rebasing=True,
        )
        self.assertEqual("conflicted", payload["result"])
        self.assertEqual(["other.py"], payload["conflict_paths"])
        self.assertEqual("conflicted", self.saved()["attempt"]["status"])

    def test_a_commit_the_rebase_emptied_is_reported_for_a_decision(self):
        payload = self.run_continue(
            attempt=self.resolved_attempt(strategy="rebase"),
            process=completed(1, "", "No changes - did you forget to use 'git add'?"),
        )
        self.assertEqual("empty_commit", payload["result"])
        self.assertEqual("skip-empty", payload["next"])

    def test_a_rebase_that_did_not_finish_is_reported(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "did not finish"):
            self.run_continue(
                attempt=self.resolved_attempt(strategy="rebase"),
                process=completed(1, "", "could not apply"),
                rebasing=True,
            )


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory(self)
        self.heads = {
            ("fork", "repo", "feature"): "head1",
            ("owner", "repo", "main"): "base1",
        }
        self.base_after = "base1"
        self.calls = []

    def remote_head(self, owner, repo, branch):
        key = (owner, repo, branch)
        self.calls.append(key)
        if key == ("owner", "repo", "main") and self.calls.count(key) > 1:
            return self.base_after
        return self.heads.get(key)

    def publish(
        self,
        *,
        local_head="head2",
        branch="feature",
        relations_before=None,
        relations_after=None,
        refreshed=None,
        final=None,
        pushed_head=None,
        **state_overrides,
    ):
        overrides = {"attempts": 1, "attempt": attempt_record(status="resolved")}
        overrides.update(state_overrides)
        state_path = write_state(self.directory, **overrides)
        args = SimpleNamespace(state=str(state_path))
        relations_before = dict(relations_before or NO_RELATIONS)
        relations_after = dict(relations_after or relations_before)
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "require_clean_worktree"
        ), mock.patch.object(
            MODULE, "require_no_integration_in_progress"
        ), mock.patch.object(
            MODULE, "git", side_effect=fake_git(head=local_head, branch=branch)
        ), mock.patch.object(
            MODULE, "stack_relations", side_effect=[relations_before, relations_after]
        ), mock.patch.object(
            MODULE, "require_fork_head"
        ), mock.patch.object(
            MODULE, "remote_head", side_effect=self.remote_head
        ), mock.patch.object(
            MODULE, "verify_push_range", return_value={"strategy": "merge", "checks": []}
        ) as verify, mock.patch.object(
            MODULE, "find_remote", return_value="origin"
        ), mock.patch.object(
            MODULE, "run", return_value=completed(0)
        ) as runner, mock.patch.object(
            MODULE,
            "wait_for_remote_head",
            return_value=local_head if pushed_head is None else pushed_head,
        ), mock.patch.object(
            MODULE, "metadata_for", return_value=refreshed or pr_metadata(head_sha=local_head)
        ), mock.patch.object(
            MODULE,
            "live_mergeability",
            return_value=final or pr_metadata(head_sha=local_head, mergeable="MERGEABLE"),
        ) as mergeability, mock.patch.object(
            MODULE, "time"
        ), mock.patch.object(
            MODULE, "emit"
        ) as emit:
            MODULE.command_publish(args)
        self.state_path = state_path
        self.verify = verify
        self.runner = runner
        self.mergeability = mergeability
        return emitted(emit)

    def saved(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_a_resolved_attempt_is_pushed_and_verified(self):
        payload = self.publish()
        self.assertEqual("published", payload["result"])
        self.assertEqual("head2", payload["head_sha"])
        self.assertEqual("head1", payload["previous_head_sha"])
        self.assertEqual("mergeable", payload["mergeability"])
        self.assertEqual("head2", payload["mergeable_at_head_sha"])
        self.assertEqual(1, payload["attempts"])
        self.assertIn("no other branch moved during the push", payload["push_verification"]["checks"])
        state = self.saved()
        self.assertEqual("published", state["attempt"]["status"])
        self.assertEqual(1, len(state["history"]))

    def test_mergeability_is_read_for_the_head_that_was_just_pushed(self):
        self.publish()
        self.assertEqual("head2", self.mergeability.call_args.kwargs["expected_head"])

    def test_an_answer_describing_the_previous_head_is_not_believed(self):
        payload = self.publish(final=pr_metadata(head_sha="head1", mergeable="MERGEABLE"))
        self.assertEqual("unknown", payload["mergeability"])
        self.assertIsNone(payload["mergeable_at_head_sha"])

    def test_the_push_always_names_the_head_branch_explicitly(self):
        self.publish()
        command = self.runner.call_args[0][0]
        self.assertEqual(["origin", "HEAD:refs/heads/feature"], command[-2:])
        self.assertNotIn("--force", " ".join(command))

    def test_a_rebase_is_pushed_with_a_lease_on_the_head_it_read(self):
        self.publish(attempt=attempt_record(status="resolved", strategy="rebase"))
        command = self.runner.call_args[0][0]
        self.assertIn("--force-with-lease=refs/heads/feature:head1", command)

    def test_a_rebase_will_not_force_push_a_branch_that_is_not_there(self):
        self.heads[("fork", "repo", "feature")] = None
        with self.assertRaisesRegex(MODULE.WorkflowError, "does not exist remotely"):
            self.publish(attempt=attempt_record(status="resolved", strategy="rebase"))

    def test_a_rebase_is_refused_while_a_pull_request_stacks_on_this_branch(self):
        payload = self.publish(
            attempt=attempt_record(status="resolved", strategy="rebase"),
            relations_before={"dependents": [dependent()], "stacked_on": None},
        )
        self.assertEqual("unsafe_push", payload["result"])
        self.assertIn("force-push a branch that open pull requests stack on", payload["push_blockers"][0])
        self.assertEqual("unsafe_push", payload["escalation"]["kind"])
        self.runner.assert_not_called()
        self.assertEqual("escalated", self.saved()["attempt"]["status"])

    def test_a_merge_may_still_be_pushed_under_a_dependent_pull_request(self):
        payload = self.publish(
            relations_before={"dependents": [dependent()], "stacked_on": None}
        )
        self.assertEqual("published", payload["result"])

    def test_a_head_branch_that_is_also_the_base_branch_is_refused(self):
        payload = self.publish(
            pr=pr_metadata(head_owner="owner", head_repo="repo", base_branch="feature")
        )
        self.assertEqual("unsafe_push", payload["result"])
        self.runner.assert_not_called()

    def test_a_base_branch_that_moved_during_the_push_is_reported(self):
        self.base_after = "base2"
        with self.assertRaisesRegex(MODULE.WorkflowError, "moved during the push"):
            self.publish()

    def test_a_dependent_branch_that_moved_during_the_push_is_reported(self):
        before = {"dependents": [dependent()], "stacked_on": None}
        after = {"dependents": [dict(dependent(), head_sha="rewritten")], "stacked_on": None}
        with self.assertRaisesRegex(MODULE.WorkflowError, "disturbed open pull requests"):
            self.publish(relations_before=before, relations_after=after)

    def test_a_dependent_that_merged_during_the_push_is_not_a_disturbance(self):
        before = {"dependents": [dependent()], "stacked_on": None}
        after = {"dependents": [], "stacked_on": None}
        payload = self.publish(relations_before=before, relations_after=after)
        self.assertEqual("published", payload["result"])

    def test_a_remote_head_that_does_not_match_is_reported(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "head branch mismatch after push"):
            self.publish(pushed_head="somethingelse")

    def test_a_pull_request_head_that_never_catches_up_is_reported(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "PR head mismatch"):
            self.publish(refreshed=pr_metadata(head_sha="stale"))

    def test_a_still_conflicting_pull_request_records_no_mergeable_head(self):
        payload = self.publish(final=pr_metadata(head_sha="head2", mergeable="CONFLICTING"))
        self.assertEqual("conflicting", payload["mergeability"])
        self.assertIsNone(payload["mergeable_at_head_sha"])
        self.assertEqual(1, payload["attempts"])

    def test_only_a_resolved_attempt_can_be_published(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "only a resolved attempt"):
            self.publish(attempt=attempt_record(status="conflicted"))

    def test_a_head_that_never_moved_has_nothing_to_publish(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "nothing this attempt resolved"):
            self.publish(local_head="head1")

    def test_publishing_from_another_branch_is_refused(self):
        state_path = write_state(self.directory, attempt=attempt_record(status="resolved"))
        args = SimpleNamespace(state=str(state_path))
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "require_clean_worktree"
        ), mock.patch.object(
            MODULE, "require_no_integration_in_progress"
        ), mock.patch.object(MODULE, "git", side_effect=fake_git(branch="main")):
            with self.assertRaisesRegex(MODULE.WorkflowError, "refusing to push from branch"):
                MODULE.command_publish(args)

    def test_a_detached_worktree_publishes_through_the_refspec(self):
        payload = self.publish(branch="")
        self.assertEqual("published", payload["result"])
        command = self.runner.call_args[0][0]
        self.assertEqual(["origin", "HEAD:refs/heads/feature"], command[-2:])

    def test_a_detached_rebase_keeps_the_lease_on_the_head_it_read(self):
        self.publish(branch="", attempt=attempt_record(status="resolved", strategy="rebase"))
        observed = self.heads[("fork", "repo", "feature")]
        branch = pr_metadata()["head_branch"]
        self.assertIn(
            f"--force-with-lease=refs/heads/{branch}:{observed}",
            self.runner.call_args[0][0],
        )

    def test_the_push_is_skipped_when_the_remote_already_holds_the_result(self):
        self.heads[("fork", "repo", "feature")] = "head2"
        payload = self.publish()
        self.assertEqual("published", payload["result"])
        self.runner.assert_not_called()


class AbortTest(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory(self)

    def abort(self, *, in_progress=None, **state_overrides):
        state_path = write_state(self.directory, **state_overrides)
        args = SimpleNamespace(state=str(state_path))
        with mock.patch.object(
            MODULE, "integration_in_progress", return_value=in_progress
        ), mock.patch.object(MODULE, "run") as runner, mock.patch.object(
            MODULE, "require_owned_active_integration"
        ) as require_owned, mock.patch.object(
            MODULE, "git", return_value="head1"
        ), mock.patch.object(MODULE, "emit") as emit:
            MODULE.command_abort(args)
        self.state_path = state_path
        self.runner = runner
        self.require_owned = require_owned
        return emitted(emit)

    def saved(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_an_in_progress_merge_is_undone_and_the_attempt_ends(self):
        payload = self.abort(in_progress="merge")
        self.assertEqual("aborted", payload["result"])
        self.assertEqual("merge", payload["undone"])
        self.assertEqual(["merge", "--abort"], self.runner.call_args[0][0][-2:])
        state = self.saved()
        self.assertIsNone(state["attempt"])
        self.assertEqual("aborted", state["history"][0]["status"])
        self.require_owned.assert_called_once()

    def test_an_in_progress_rebase_is_undone(self):
        payload = self.abort(in_progress="rebase")
        self.assertEqual(["rebase", "--abort"], self.runner.call_args[0][0][-2:])
        self.assertEqual("rebase", payload["undone"])

    def test_aborting_with_nothing_in_progress_still_ends_the_attempt(self):
        payload = self.abort()
        self.assertIsNone(payload["undone"])
        self.runner.assert_not_called()
        self.assertIsNone(self.saved()["attempt"])

    def test_a_published_attempt_is_left_alone(self):
        self.abort(attempt=attempt_record(status="published"))
        self.assertEqual("published", self.saved()["attempt"]["status"])

    def test_aborting_with_no_attempt_is_harmless(self):
        payload = self.abort(attempt=None)
        self.assertEqual("aborted", payload["result"])

    def test_an_unrecognized_active_integration_is_not_aborted(self):
        state_path = write_state(self.directory, attempt=None)
        with mock.patch.object(
            MODULE, "integration_in_progress", return_value="merge"
        ), mock.patch.object(MODULE, "run") as runner:
            with self.assertRaisesRegex(MODULE.WorkflowError, "unrecognized merge"):
                MODULE.command_abort(SimpleNamespace(state=str(state_path)))
        runner.assert_not_called()

    def test_a_planned_attempt_does_not_own_a_matching_active_merge(self):
        state_path = write_state(
            self.directory, attempt=attempt_record(status="planned")
        )
        with mock.patch.object(
            MODULE, "integration_in_progress", return_value="merge"
        ), mock.patch.object(
            MODULE, "require_owned_active_integration"
        ) as require_owned, mock.patch.object(
            MODULE, "run"
        ) as runner:
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "helper-started integration"
            ):
                MODULE.command_abort(SimpleNamespace(state=str(state_path)))
        require_owned.assert_not_called()
        runner.assert_not_called()


class EscalateTest(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory(self)

    def escalate(
        self,
        *,
        kind="contradiction",
        reason="the two sides cannot both hold",
        reason_file=None,
        recommended_action="a person must choose which behaviour to keep",
        **state_overrides,
    ):
        state_path = write_state(self.directory, **state_overrides)
        args = SimpleNamespace(
            state=str(state_path),
            kind=kind,
            reason=reason,
            reason_file=reason_file,
            recommended_action=recommended_action,
        )
        with mock.patch.object(MODULE, "emit") as emit:
            MODULE.command_escalate(args)
        self.state_path = state_path
        return emitted(emit)

    def saved(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_a_contradiction_is_recorded_and_ends_the_attempt(self):
        payload = self.escalate()
        self.assertEqual("escalated", payload["result"])
        self.assertEqual("contradiction", payload["escalation"]["kind"])
        self.assertEqual(1, payload["escalation"]["attempt_number"])
        state = self.saved()
        self.assertEqual("escalated", state["attempt"]["status"])
        self.assertEqual(1, len(state["history"]))
        self.assertTrue(payload["escalation"]["requires_user_decision"])

    def test_an_automation_blocker_does_not_claim_a_user_decision(self):
        payload = self.escalate(
            kind="automation_blocker",
            reason="the helper rejected a proven rename destination",
            recommended_action="upgrade the helper and rerun",
        )
        self.assertFalse(payload["escalation"]["requires_user_decision"])

    def test_an_automation_blocker_requires_a_recovery_action(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "must name the helper change"):
            self.escalate(
                kind="automation_blocker",
                reason="the helper rejected a proven rename destination",
                recommended_action=None,
            )

    def test_the_reason_can_come_from_a_file(self):
        reason = self.directory / "reason.txt"
        reason.write_text("both sides changed the same guard\n", encoding="utf-8")
        payload = self.escalate(reason=None, reason_file=str(reason))
        self.assertEqual("both sides changed the same guard", payload["escalation"]["reason"])

    def test_a_published_attempt_keeps_its_status(self):
        self.escalate(kind="other", attempt=attempt_record(status="published"))
        self.assertEqual("published", self.saved()["attempt"]["status"])

    def test_escalating_with_no_attempt_still_records_the_reason(self):
        payload = self.escalate(attempt=None)
        self.assertIsNone(payload["attempt"])
        self.assertIsNone(payload["escalation"]["attempt_number"])
        self.assertEqual("contradiction", self.saved()["escalation"]["kind"])


class StageOutcomeTest(unittest.TestCase):
    def state(self, **overrides):
        base = {"escalation": None, "attempt": None}
        base.update(overrides)
        return base

    def test_every_outcome_is_in_the_agreed_vocabulary(self):
        self.assertEqual(
            ("cleared", "skipped", "completed", "escalated"),
            MODULE.STAGE_OUTCOMES,
        )

    def test_a_pull_request_already_mergeable_cleared_the_stage(self):
        outcome = MODULE.stage_outcome(
            self.state(
                attempt=attempt_record(
                    status="mergeable", mergeable_at_head_sha="head1"
                )
            )
        )
        self.assertEqual("cleared", outcome)

    def test_a_published_run_that_reached_mergeable_cleared_the_stage(self):
        outcome = MODULE.stage_outcome(
            self.state(
                attempt=attempt_record(
                    status="published",
                    published_head_sha="head2",
                    mergeable_at_head_sha="head2",
                )
            )
        )
        self.assertEqual("cleared", outcome)

    def test_a_published_run_that_did_not_reach_mergeable_is_completed(self):
        """Calling this escalated would send a person after a run that worked.

        The run pushed its resolution and read the answer GitHub gave, which
        is the whole job even when that answer is still conflicting.
        """
        outcome = MODULE.stage_outcome(
            self.state(
                attempt=attempt_record(
                    status="published",
                    published_head_sha="head2",
                    mergeable_at_head_sha=None,
                )
            )
        )
        self.assertEqual("completed", outcome)

    def test_a_completed_run_does_not_clear_the_stage(self):
        state = self.state(
            attempt=attempt_record(
                status="published",
                published_head_sha="head2",
                mergeable_at_head_sha=None,
            )
        )
        self.assertEqual("completed", MODULE.stage_outcome(state))
        self.assertIsNone(MODULE.cleared_head_sha(state))

    def test_a_clearance_recorded_for_another_commit_is_not_believed(self):
        outcome = MODULE.stage_outcome(
            self.state(
                attempt=attempt_record(
                    status="published",
                    published_head_sha="head2",
                    mergeable_at_head_sha="head1",
                )
            )
        )
        self.assertEqual("completed", outcome)

    def test_a_clearance_that_names_no_commit_is_not_a_clearance(self):
        outcome = MODULE.stage_outcome(
            self.state(
                attempt=attempt_record(status="mergeable", mergeable_at_head_sha=None)
            )
        )
        self.assertEqual("escalated", outcome)

    def test_an_automation_blocker_remains_machine_readable_as_escalated(self):
        outcome = MODULE.stage_outcome(
            self.state(
                escalation={
                    "kind": "automation_blocker",
                    "reason": "the helper cannot represent the resolution",
                    "requires_user_decision": False,
                }
            )
        )
        self.assertEqual("escalated", outcome)

    def test_every_escalation_kind_reports_escalated(self):
        for kind in MODULE.ESCALATION_KINDS:
            with self.subTest(kind=kind):
                outcome = MODULE.stage_outcome(
                    self.state(escalation={"kind": kind, "reason": "r"})
                )
                self.assertEqual("escalated", outcome)

    def test_an_escalation_outranks_a_recorded_clearance(self):
        outcome = MODULE.stage_outcome(
            self.state(
                escalation={"kind": "contradiction", "reason": "r"},
                attempt=attempt_record(
                    status="published",
                    published_head_sha="head2",
                    mergeable_at_head_sha="head2",
                ),
            )
        )
        self.assertEqual("escalated", outcome)

    def test_a_run_still_in_flight_says_nothing_at_all(self):
        for status in ("planned", "conflicted", "integrated", "resolved"):
            with self.subTest(status=status):
                outcome = MODULE.stage_outcome(
                    self.state(attempt=attempt_record(status=status))
                )
                self.assertIsNone(outcome)

    def test_a_killed_run_is_indistinguishable_from_a_running_one(self):
        running = self.state(attempt=attempt_record(status="conflicted"))
        killed = json.loads(json.dumps(running))
        self.assertEqual(running, killed)
        self.assertIsNone(MODULE.stage_outcome(killed))

    def test_a_recorded_ending_nobody_recognizes_still_escalates(self):
        for status in ("aborted", "escalated"):
            with self.subTest(status=status):
                outcome = MODULE.stage_outcome(
                    self.state(attempt=attempt_record(status=status))
                )
                self.assertEqual("escalated", outcome)

    def test_only_a_recorded_ending_earns_a_word(self):
        for status in MODULE.RECORDED_ENDINGS:
            with self.subTest(status=status, recorded=True):
                outcome = MODULE.stage_outcome(
                    self.state(attempt=attempt_record(status=status))
                )
                self.assertIn(outcome, MODULE.STAGE_OUTCOMES)
        for status in ("planned", "conflicted", "integrated", "resolved", None):
            with self.subTest(status=status, recorded=False):
                self.assertNotIn(status, MODULE.RECORDED_ENDINGS)
                outcome = MODULE.stage_outcome(
                    self.state(
                        attempt=None if status is None else attempt_record(status=status)
                    )
                )
                self.assertIsNone(outcome)

    def test_a_missing_state_describes_no_run_at_all(self):
        for state in (None, {}, self.state()):
            with self.subTest(state=state):
                self.assertIsNone(MODULE.stage_outcome(state))

    def test_a_payload_with_no_run_to_describe_carries_no_outcome(self):
        payload = MODULE.with_stage_outcome({"result": "no_state"}, None)
        self.assertNotIn("stage_outcome", payload)

    def test_a_payload_describing_a_run_carries_its_outcome(self):
        payload = MODULE.with_stage_outcome(
            {"result": "ready"},
            self.state(
                attempt=attempt_record(status="mergeable", mergeable_at_head_sha="head1")
            ),
        )
        self.assertEqual("cleared", payload["stage_outcome"])

    def test_a_cleared_payload_always_names_the_commit_it_cleared_at(self):
        for attempt in (
            attempt_record(status="mergeable", mergeable_at_head_sha="head1"),
            attempt_record(
                status="published",
                published_head_sha="head2",
                mergeable_at_head_sha="head2",
            ),
        ):
            with self.subTest(status=attempt["status"]):
                payload = MODULE.with_stage_outcome(
                    {"result": "ready"}, self.state(attempt=attempt)
                )
                self.assertEqual("cleared", payload["stage_outcome"])
                self.assertEqual(
                    attempt["mergeable_at_head_sha"], payload["mergeable_at_head_sha"]
                )

    def test_no_commit_means_no_clearance(self):
        for state in (
            None,
            {},
            self.state(attempt=attempt_record(status="conflicted")),
            self.state(
                attempt=attempt_record(status="mergeable", mergeable_at_head_sha=None)
            ),
            self.state(
                escalation={"kind": "contradiction", "reason": "r"},
                attempt=attempt_record(
                    status="published",
                    published_head_sha="head2",
                    mergeable_at_head_sha="head2",
                ),
            ),
        ):
            with self.subTest(state=state):
                self.assertIsNone(MODULE.cleared_head_sha(state))
                self.assertNotEqual("cleared", MODULE.stage_outcome(state))

    def test_the_stage_never_reports_skipped(self):
        for status in ("mergeable", "published", "planned", "conflicted", None):
            with self.subTest(status=status):
                outcome = MODULE.stage_outcome(
                    self.state(
                        attempt=None if status is None else attempt_record(status=status)
                    )
                )
                self.assertIn(outcome, (None,) + MODULE.STAGE_OUTCOMES)
                self.assertNotEqual("skipped", outcome)


class StatusCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory(self)

    def status(self, *, current=False, state=None, **state_overrides):
        path = state if state is not None else write_state(self.directory, **state_overrides)
        args = SimpleNamespace(
            state=None if current else str(path), current=current, repo_root=None
        )
        with mock.patch.object(MODULE, "emit") as emit:
            MODULE.command_status(args)
        self.state_path = Path(path) if path else None
        return emitted(emit)

    def test_the_machine_readable_answer_carries_the_run_outcome(self):
        payload = self.status(
            attempts=2,
            attempt=attempt_record(
                status="published",
                published_head_sha="head2",
                mergeable_at_head_sha="head2",
            ),
            history=[{"id": "pr-7-attempt-1", "status": "published"}],
            relations={"dependents": [dependent()], "stacked_on": None},
        )
        self.assertEqual("ready", payload["result"])
        self.assertEqual("cleared", payload["stage_outcome"])
        self.assertEqual(7, payload["pr"]["number"])
        self.assertEqual("published", payload["attempt"]["status"])
        self.assertEqual("head2", payload["mergeable_at_head_sha"])
        self.assertEqual(2, payload["attempts"])
        self.assertEqual(
            {"conflicts": 1, "dependents": 1, "history": 1}, payload["counts"]
        )

    def test_status_reports_when_the_helper_last_wrote_its_state(self):
        """The only signal a reader has for telling working from wedged.

        Every write stamps it, so a stamp minutes old and a stamp an hour old
        are different answers to the question a person actually asks.
        """
        payload = self.status(updated_at="2026-02-03T04:05:06Z")
        self.assertEqual("2026-02-03T04:05:06Z", payload["last_helper_activity"])
        snapshot = json.loads(
            MODULE.status_path_for(self.state_path).read_text(encoding="utf-8")
        )
        self.assertEqual("2026-02-03T04:05:06Z", snapshot["last_helper_activity"])

    def test_an_escalation_is_reported_verbatim(self):
        escalation = {
            "kind": "contradiction",
            "reason": "both sides changed the same guard",
            "recommended_action": "a person must choose",
            "requires_user_decision": True,
            "attempt_number": 1,
            "recorded_at": "2026-01-01T00:00:00Z",
        }
        payload = self.status(escalation=escalation)
        self.assertEqual(escalation, payload["escalation"])
        self.assertEqual("escalated", payload["stage_outcome"])

    def test_the_status_file_carries_the_full_state(self):
        payload = self.status()
        detail = json.loads(Path(payload["status_path"]).read_text(encoding="utf-8"))
        self.assertEqual(pr_metadata(), detail["pr"])
        self.assertEqual(attempt_record(), detail["attempt"])
        self.assertEqual(ALL_MERGE_METHODS, detail["merge_methods"])

    def test_the_status_file_carries_the_same_outcome_as_the_envelope(self):
        payload = self.status(
            attempt=attempt_record(
                status="published",
                published_head_sha="head2",
                mergeable_at_head_sha="head2",
            )
        )
        detail = json.loads(Path(payload["status_path"]).read_text(encoding="utf-8"))
        self.assertEqual("cleared", payload["stage_outcome"])
        self.assertEqual(payload["stage_outcome"], detail["stage_outcome"])

    def test_a_state_with_no_attempt_still_answers(self):
        payload = self.status(attempt=None)
        self.assertIsNone(payload["attempt"])
        self.assertEqual(0, payload["counts"]["conflicts"])
        self.assertIsNone(payload["mergeable_at_head_sha"])

    def test_a_run_still_in_flight_reports_no_outcome(self):
        for status in ("planned", "conflicted", "integrated", "resolved"):
            with self.subTest(status=status):
                payload = self.status(attempt=attempt_record(status=status))
                self.assertEqual("ready", payload["result"])
                self.assertNotIn("stage_outcome", payload)

    def test_a_cleared_answer_always_names_the_commit_it_cleared_at(self):
        payload = self.status(
            attempt=attempt_record(
                status="published",
                published_head_sha="head2",
                mergeable_at_head_sha="head2",
            )
        )
        detail = json.loads(Path(payload["status_path"]).read_text(encoding="utf-8"))
        for answer in (payload, detail):
            self.assertEqual("cleared", answer["stage_outcome"])
            self.assertEqual("head2", answer["mergeable_at_head_sha"])

    def test_an_outcome_never_travels_without_a_ready_payload(self):
        for overrides in (
            {},
            {"attempt": None},
            {"escalation": {"kind": "contradiction", "reason": "r"}},
            {"attempt": attempt_record(status="mergeable")},
        ):
            with self.subTest(overrides=overrides):
                payload = self.status(**overrides)
                if "stage_outcome" in payload:
                    self.assertEqual("ready", payload["result"])
                    self.assertIn(payload["stage_outcome"], MODULE.STAGE_OUTCOMES)

    def test_the_current_branch_can_be_looked_up_without_a_state_path(self):
        state_path = write_state(self.directory)
        args = SimpleNamespace(state=None, current=True, repo_root=None)
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "resolve_repo_root", return_value=self.directory
        ), mock.patch.object(
            MODULE, "current_pr_target", return_value=MODULE.parse_target("owner/repo#7")
        ), mock.patch.object(
            MODULE, "default_state_path", return_value=state_path
        ), mock.patch.object(MODULE, "emit") as emit:
            MODULE.command_status(args)
        self.assertEqual("ready", emitted(emit)["result"])

    def test_a_pull_request_this_resolver_never_touched_reports_no_state(self):
        args = SimpleNamespace(state=None, current=True, repo_root=None)
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "resolve_repo_root", return_value=self.directory
        ), mock.patch.object(
            MODULE, "current_pr_target", return_value=MODULE.parse_target("owner/repo#7")
        ), mock.patch.object(
            MODULE, "default_state_path", return_value=self.directory / "absent.json"
        ), mock.patch.object(MODULE, "emit") as emit:
            MODULE.command_status(args)
        payload = emitted(emit)
        self.assertEqual("no_state", payload["result"])
        self.assertNotIn("stage_outcome", payload)
        self.assertEqual(7, payload["pr"]["number"])
        self.assertIsNone(payload["attempt"])
        self.assertEqual([], payload["history"])


class CleanupTest(unittest.TestCase):
    def test_the_state_and_every_sidecar_are_removed(self):
        directory = temporary_directory(self)
        state_path = write_state(directory)
        for path in (
            MODULE.preflight_path_for(state_path),
            MODULE.conflicts_path_for(state_path),
            MODULE.status_path_for(state_path),
        ):
            path.write_text("{}", encoding="utf-8")
        with mock.patch.object(MODULE, "emit") as emit:
            MODULE.command_cleanup(SimpleNamespace(state=str(state_path)))
        self.assertEqual("cleaned_up", emitted(emit)["result"])
        self.assertEqual([], list(directory.iterdir()))

    def test_missing_sidecars_are_not_an_error(self):
        directory = temporary_directory(self)
        state_path = write_state(directory)
        with mock.patch.object(MODULE, "emit"):
            MODULE.command_cleanup(SimpleNamespace(state=str(state_path)))
        self.assertFalse(state_path.exists())

    def test_a_state_file_from_another_workflow_is_not_removed(self):
        directory = temporary_directory(self)
        path = directory / "state.json"
        path.write_text(json.dumps({"version": 99}), encoding="utf-8")
        with self.assertRaisesRegex(MODULE.WorkflowError, "unsupported state version"):
            MODULE.command_cleanup(SimpleNamespace(state=str(path)))
        self.assertTrue(path.exists())


class ParserTest(unittest.TestCase):
    def setUp(self):
        self.parser = MODULE.build_parser()

    def test_every_subcommand_is_available(self):
        for command in (
            "preflight",
            "attempt",
            "resolved",
            "continue",
            "abort",
            "escalate",
            "publish",
            "status",
            "cleanup",
        ):
            with self.subTest(command=command):
                arguments = [command]
                if command == "status":
                    arguments.append("--current")
                elif command != "preflight":
                    arguments.extend(["--state", "s.json"])
                if command == "resolved":
                    arguments.extend(["--paths", "a.py", "--rationale", "r"])
                if command == "escalate":
                    arguments.extend(["--kind", "contradiction", "--reason", "r"])
                args = self.parser.parse_args(arguments)
                self.assertTrue(callable(args.function))

    def test_preflight_defaults_match_the_documented_run(self):
        args = self.parser.parse_args(["preflight"])
        self.assertIsNone(args.target)
        self.assertEqual("auto", args.strategy)
        self.assertFalse(args.whole_stack)
        self.assertFalse(hasattr(args, "max_iterations"))

    def test_preflight_rejects_the_arguments_an_outer_loop_used_to_pass(self):
        """A caller that still passes them would otherwise run unbounded.

        Failing loudly beats silently accepting a cap this resolver no longer
        honours.
        """
        for argument in (
            "--max-iterations",
            "--new-invocation",
            "--invocation-run",
            "--pipeline-run",
            "--pipeline-iteration",
            "--pipeline-max-iterations",
        ):
            with self.subTest(argument=argument):
                with self.assertRaises(SystemExit):
                    self.parser.parse_args(["preflight", argument, "1"])

    def test_preflight_accepts_every_argument_shape(self):
        for target in (
            "https://github.com/owner/repo/pull/7",
            "owner/repo#7",
        ):
            with self.subTest(target=target):
                args = self.parser.parse_args(["preflight", target])
                self.assertEqual(target, args.target)

    def test_preflight_rejects_an_unknown_strategy(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["preflight", "--strategy", "squash"])

    def test_resolved_requires_exactly_one_rationale_source(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["resolved", "--state", "s.json", "--paths", "a.py"])
        with self.assertRaises(SystemExit):
            self.parser.parse_args(
                [
                    "resolved",
                    "--state",
                    "s.json",
                    "--paths",
                    "a.py",
                    "--rationale",
                    "r",
                    "--rationale-file",
                    "f",
                ]
            )

    def test_resolved_takes_several_paths_and_every_override_flag(self):
        args = self.parser.parse_args(
            [
                "resolved",
                "--state",
                "s.json",
                "--paths",
                "a.py",
                "b.py",
                "--rationale",
                "r",
                "--accept-one-side",
                "--accept-deletion",
                "--accept-line-endings",
            ]
        )
        self.assertEqual(["a.py", "b.py"], args.paths)
        self.assertTrue(args.accept_one_side)
        self.assertTrue(args.accept_deletion)
        self.assertTrue(args.accept_line_endings)

    def test_the_resolved_override_flags_are_off_by_default(self):
        args = self.parser.parse_args(
            ["resolved", "--state", "s.json", "--paths", "a.py", "--rationale", "r"]
        )
        self.assertFalse(args.accept_one_side)
        self.assertFalse(args.accept_deletion)
        self.assertFalse(args.accept_line_endings)

    def test_escalate_only_accepts_a_declared_kind(self):
        for kind in MODULE.ESCALATION_KINDS:
            with self.subTest(kind=kind):
                args = self.parser.parse_args(
                    ["escalate", "--state", "s.json", "--kind", kind, "--reason", "r"]
                )
                self.assertEqual(kind, args.kind)
        with self.assertRaises(SystemExit):
            self.parser.parse_args(
                ["escalate", "--state", "s.json", "--kind", "whatever", "--reason", "r"]
            )

    def test_status_needs_one_source_and_only_one(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["status"])
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["status", "--state", "s.json", "--current"])

    def test_a_command_is_required(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args([])


class MainTest(unittest.TestCase):
    def test_a_successful_command_exits_zero(self):
        with mock.patch.object(
            MODULE.sys, "argv", ["pr_conflict_resolver.py", "status", "--state", "s.json"]
        ), mock.patch.object(MODULE, "command_status") as command:
            self.assertEqual(0, MODULE.main())
        command.assert_called_once()

    def test_a_workflow_error_is_reported_as_json_and_exits_one(self):
        with mock.patch.object(
            MODULE.sys, "argv", ["pr_conflict_resolver.py", "status", "--state", "s.json"]
        ), mock.patch.object(
            MODULE, "command_status", side_effect=MODULE.WorkflowError("boom")
        ), mock.patch.object(MODULE, "emit") as emit:
            self.assertEqual(1, MODULE.main())
        self.assertEqual({"result": "error", "error": "boom"}, emitted(emit))

    def test_a_broken_state_file_is_reported_the_same_way(self):
        for error in (
            json.JSONDecodeError("bad", "{}", 0),
            OSError("permission denied"),
        ):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(
                    MODULE.sys,
                    "argv",
                    ["pr_conflict_resolver.py", "status", "--state", "s.json"],
                ), mock.patch.object(
                    MODULE, "command_status", side_effect=error
                ), mock.patch.object(MODULE, "emit") as emit:
                    self.assertEqual(1, MODULE.main())
                self.assertEqual("error", emitted(emit)["result"])

    def test_an_unexpected_error_is_not_swallowed(self):
        with mock.patch.object(
            MODULE.sys, "argv", ["pr_conflict_resolver.py", "status", "--state", "s.json"]
        ), mock.patch.object(
            MODULE, "command_status", side_effect=ValueError("unexpected")
        ):
            with self.assertRaises(ValueError):
                MODULE.main()

    def test_the_script_reports_a_missing_tool_through_the_same_channel(self):
        with mock.patch.object(MODULE.shutil, "which", return_value=None):
            with self.assertRaisesRegex(MODULE.WorkflowError, "required tools not found"):
                MODULE.require_tools()


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class RealGitConflictTest(GitTestCase):
    """Drive the git plumbing against a repository that really is conflicted."""

    @classmethod
    def setUpClass(cls):
        cls.template = tempfile.TemporaryDirectory()
        cls.template_repo = Path(cls.template.name).resolve() / "repo"
        cls.template_repo.mkdir()
        cls.git_in(cls.template_repo, "init", "--initial-branch", "main")
        cls.git_in(cls.template_repo, "config", "user.name", "PR Conflict Resolver")
        cls.git_in(
            cls.template_repo,
            "config",
            "user.email",
            "pr-conflict-resolver@example.invalid",
        )
        cls.git_in(cls.template_repo, "config", "core.autocrlf", "false")
        cls.git_in(cls.template_repo, "config", "commit.gpgsign", "false")

        cls.write_in(cls.template_repo, "app.py", "def greet():\n    return 'hello'\n")
        cls.write_in(cls.template_repo, "notes.md", "notes\n")
        cls.commit_in(cls.template_repo, "Add the greeting")
        cls.merge_base = cls.git_in(cls.template_repo, "rev-parse", "HEAD")

        cls.git_in(cls.template_repo, "checkout", "-b", "feature")
        cls.write_in(
            cls.template_repo,
            "app.py",
            "def greet(name):\n    return f'hello {name}'\n",
        )
        cls.commit_in(cls.template_repo, "Take a name")
        cls.head_sha = cls.git_in(cls.template_repo, "rev-parse", "HEAD")

        cls.git_in(cls.template_repo, "checkout", "main")
        cls.write_in(cls.template_repo, "app.py", "def greet():\n    return 'hello!'\n")
        cls.commit_in(cls.template_repo, "Add the exclamation mark")
        cls.base_sha = cls.git_in(cls.template_repo, "rev-parse", "HEAD")
        cls.git_in(cls.template_repo, "tag", "fixture-feature", cls.head_sha)
        cls.git_in(cls.template_repo, "branch", "--delete", "--force", "feature")

    @classmethod
    def tearDownClass(cls):
        cls.template.cleanup()

    def setUp(self):
        root = temporary_directory(self)
        self.repo = root / "repo"
        self.git_in(
            self.template_repo,
            "worktree",
            "add",
            "--quiet",
            "-b",
            "feature",
            str(self.repo),
            "fixture-feature",
        )
        self.addCleanup(self.remove_worktree)

    def remove_worktree(self):
        self.git_in(
            self.template_repo,
            "worktree",
            "remove",
            "--force",
            str(self.repo),
        )
        self.git_in(
            self.template_repo, "branch", "--delete", "--force", "feature"
        )

    def git(self, *arguments):
        return self.git_in(self.repo, *arguments)

    def write(self, name, text):
        self.write_in(self.repo, name, text)

    def commit(self, message):
        self.commit_in(self.repo, message)

    def start_merge(self):
        subprocess.run(
            ["git", "-C", str(self.repo), "merge", "--no-commit", "--no-ff", self.base_sha],
            capture_output=True,
            text=True,
        )

    def test_a_clean_worktree_and_no_integration_are_seen_before_the_merge(self):
        MODULE.require_clean_worktree(self.repo)
        MODULE.require_no_integration_in_progress(self.repo)
        self.assertIsNone(MODULE.integration_in_progress(self.repo))

    def test_the_merge_leaves_one_unmerged_path_with_all_three_stages(self):
        self.start_merge()
        self.assertEqual("merge", MODULE.integration_in_progress(self.repo))
        self.assertTrue(MODULE.merge_in_progress(self.repo))
        self.assertFalse(MODULE.rebase_in_progress(self.repo))

        entries = MODULE.unmerged_entries(self.repo)
        self.assertEqual(
            [{"code": "UU", "path": "app.py", "kind": "both modified"}], entries
        )
        blobs = MODULE.stage_blobs(self.repo, "app.py")
        self.assertEqual(b"def greet():\n    return 'hello'\n", blobs["ancestor"])
        self.assertIn(b"f'hello {name}'", blobs["head"])
        self.assertIn(b"'hello!'", blobs["base"])

    def test_collected_evidence_names_both_sides_of_the_conflict(self):
        self.start_merge()
        conflicts = MODULE.collect_conflicts(
            self.repo,
            head_sha=self.head_sha,
            base_sha=self.base_sha,
            merge_base=self.merge_base,
        )
        self.assertEqual(1, len(conflicts))
        conflict = conflicts[0]
        self.assertEqual("app.py", conflict["path"])
        self.assertFalse(conflict["binary"])
        self.assertEqual(["ancestor", "base", "head"], conflict["present_stages"])
        self.assertEqual(1, len(conflict["marker_regions"]))
        self.assertEqual([], conflict["marker_problems"])
        self.assertEqual(["Take a name"], [item["subject"] for item in conflict["head_commits"]])
        self.assertEqual(
            ["Add the exclamation mark"],
            [item["subject"] for item in conflict["base_commits"]],
        )

    def test_commit_subjects_read_the_branch_the_run_would_republish(self):
        self.assertEqual(
            ["Take a name"],
            MODULE.commit_subjects(self.repo, f"{self.merge_base}..{self.head_sha}"),
        )

    def test_a_resolution_that_keeps_both_sides_is_recorded_and_committed(self):
        self.start_merge()
        conflicts = MODULE.collect_conflicts(
            self.repo,
            head_sha=self.head_sha,
            base_sha=self.base_sha,
            merge_base=self.merge_base,
        )
        state_directory = temporary_directory(self)
        state_path = write_state(
            state_directory,
            repo_root=str(self.repo),
            pr=pr_metadata(head_sha=self.head_sha, base_sha=self.base_sha),
            attempt=attempt_record(
                head_sha=self.head_sha,
                base_sha=self.base_sha,
                merge_base=self.merge_base,
                conflicts=conflicts,
            ),
        )

        self.write("app.py", "def greet(name):\n    return f'hello {name}!'\n")
        with mock.patch.object(MODULE, "emit") as emit:
            MODULE.command_resolved(
                SimpleNamespace(
                    state=str(state_path),
                    paths=["app.py"],
                    rationale="keep the name parameter and the exclamation mark",
                    rationale_file=None,
                    accept_one_side=False,
                    accept_deletion=False,
                    accept_line_endings=False,
                )
            )
        self.assertEqual("recorded", emitted(emit)["result"])
        self.assertEqual([], MODULE.unmerged_entries(self.repo))

        with mock.patch.object(MODULE, "emit") as emit:
            MODULE.command_continue(SimpleNamespace(state=str(state_path)))
        payload = emitted(emit)
        self.assertEqual("resolved", payload["result"])
        self.assertEqual("publish", payload["next"])
        self.assertIsNone(MODULE.integration_in_progress(self.repo))
        self.assertEqual(
            "Merge branch 'main' into feature", self.git("log", "-1", "--format=%s")
        )
        self.assertIn(
            "app.py: keep the name parameter and the exclamation mark",
            self.git("log", "-1", "--format=%B"),
        )
        self.assertEqual(
            "def greet(name):\n    return f'hello {name}!'\n",
            (self.repo / "app.py").read_text(encoding="utf-8"),
        )
        MODULE.require_clean_worktree(self.repo)

    def test_a_resolution_that_keeps_only_one_side_is_refused(self):
        self.start_merge()
        conflicts = MODULE.collect_conflicts(
            self.repo,
            head_sha=self.head_sha,
            base_sha=self.base_sha,
            merge_base=self.merge_base,
        )
        state_directory = temporary_directory(self)
        state_path = write_state(
            state_directory,
            repo_root=str(self.repo),
            attempt=attempt_record(
                head_sha=self.head_sha,
                base_sha=self.base_sha,
                conflicts=conflicts,
            ),
        )
        self.write("app.py", "def greet():\n    return 'hello!'\n")
        with self.assertRaisesRegex(MODULE.WorkflowError, "byte-for-byte the base side"):
            MODULE.command_resolved(
                SimpleNamespace(
                    state=str(state_path),
                    paths=["app.py"],
                    rationale="just take the base side",
                    rationale_file=None,
                    accept_one_side=False,
                    accept_deletion=False,
                    accept_line_endings=False,
                )
            )

    def test_a_file_left_with_markers_is_refused(self):
        self.start_merge()
        conflicts = MODULE.collect_conflicts(
            self.repo,
            head_sha=self.head_sha,
            base_sha=self.base_sha,
            merge_base=self.merge_base,
        )
        state_directory = temporary_directory(self)
        state_path = write_state(
            state_directory,
            repo_root=str(self.repo),
            attempt=attempt_record(
                head_sha=self.head_sha,
                base_sha=self.base_sha,
                conflicts=conflicts,
            ),
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "still holds conflict markers"):
            MODULE.command_resolved(
                SimpleNamespace(
                    state=str(state_path),
                    paths=["app.py"],
                    rationale="not finished",
                    rationale_file=None,
                    accept_one_side=False,
                    accept_deletion=False,
                    accept_line_endings=False,
                )
            )

    def test_aborting_restores_the_branch(self):
        self.start_merge()
        state_directory = temporary_directory(self)
        state_path = write_state(
            state_directory,
            repo_root=str(self.repo),
            attempt=attempt_record(head_sha=self.head_sha, base_sha=self.base_sha),
        )
        with mock.patch.object(MODULE, "emit") as emit:
            MODULE.command_abort(SimpleNamespace(state=str(state_path)))
        payload = emitted(emit)
        self.assertEqual("merge", payload["undone"])
        self.assertEqual(self.head_sha, payload["head_sha"])
        self.assertIsNone(MODULE.integration_in_progress(self.repo))
        MODULE.require_clean_worktree(self.repo)

    def test_a_dirty_worktree_stops_the_run(self):
        self.write("notes.md", "edited\n")
        with self.assertRaisesRegex(MODULE.WorkflowError, "worktree is not clean"):
            MODULE.require_clean_worktree(self.repo)

    def test_the_repository_remote_is_found_by_its_github_url(self):
        self.git("remote", "add", "origin", "https://github.com/owner/repo.git")
        self.addCleanup(self.git, "remote", "remove", "origin")
        self.assertEqual("origin", MODULE.find_remote(self.repo, "Owner/Repo", push=False))
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.find_remote(self.repo, "other/repo", push=False)

    def test_the_repository_root_resolves_from_a_subdirectory(self):
        (self.repo / "nested").mkdir()
        self.assertEqual(self.repo, MODULE.resolve_repo_root(str(self.repo / "nested")))



@unittest.skipUnless(shutil.which("git"), "git is not installed")
class HeadBranchHeldElsewhereTest(unittest.TestCase):
    """The head branch is checked out in a sibling worktree, as in a live run."""

    def setUp(self):
        root = temporary_directory(self)
        self.real_run = MODULE.run
        self.repo = root / "pipeline"
        self.repo.mkdir()
        self.session = root / "session"
        self.git("init", "--initial-branch", "main")
        self.git("config", "user.name", "PR Conflict Resolver")
        self.git("config", "user.email", "pr-conflict-resolver@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write("app.py", "start\n")
        self.commit("Start the app")
        self.branch = pr_metadata()["head_branch"]
        self.git("checkout", "-b", self.branch)
        self.write("app.py", "change\n")
        self.commit("Change the app")
        self.head_sha = self.git("rev-parse", "HEAD")
        self.git("checkout", "main")
        self.git("worktree", "add", str(self.session), self.branch)
        self.metadata = pr_metadata(head_sha=self.head_sha)
        self.target = MODULE.parse_target("owner/repo#7")

    def git(self, *arguments):
        process = subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if process.returncode != 0:
            self.fail(f"git {' '.join(arguments)} failed: {process.stderr}")
        return process.stdout.strip()

    def write(self, name, text):
        (self.repo / name).write_text(text, encoding="utf-8", newline="\n")

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "--no-gpg-sign", "--message", message)

    def gh_checkout(self, command, cwd=None, **keywords):
        """Stand in for `gh pr checkout`, which runs exactly these git checkouts.

        Everything else the module runs is a real git command and is passed
        through, so the checkout meets git's own rules about worktrees.
        """
        if command[0] != "gh":
            return self.real_run(command, cwd=cwd, **keywords)
        self.assertEqual(["gh", "pr", "checkout", self.target["pr_url"]], command[:4])
        if "--detach" in command:
            arguments = ["checkout", "--detach", self.head_sha]
        else:
            arguments = ["checkout", self.branch]
        process = subprocess.run(
            ["git", "-C", str(cwd or self.repo), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if process.returncode != 0:
            raise MODULE.WorkflowError(process.stderr.strip())
        return process

    def test_git_refuses_to_hand_the_branch_to_a_second_worktree(self):
        with self.assertRaises(MODULE.WorkflowError) as refusal:
            self.gh_checkout(["gh", "pr", "checkout", self.target["pr_url"]])
        self.assertIn("already", str(refusal.exception))
        self.assertIn(self.branch, str(refusal.exception))

    def test_the_checkout_reaches_the_head_the_branch_is_held_elsewhere(self):
        with mock.patch.object(MODULE, "run", side_effect=self.gh_checkout):
            attached = MODULE.checkout_pr_branch(
                self.repo, self.target, self.metadata
            )
        self.assertFalse(attached)
        self.assertEqual("", self.git("branch", "--show-current"))
        self.assertEqual(self.head_sha, self.git("rev-parse", "HEAD"))

    def test_a_worktree_already_holding_the_branch_stays_on_it(self):
        self.git("worktree", "remove", str(self.session))
        self.git("checkout", self.branch)
        with mock.patch.object(MODULE, "run", side_effect=self.gh_checkout):
            attached = MODULE.checkout_pr_branch(
                self.repo, self.target, self.metadata
            )
        self.assertTrue(attached)
        self.assertEqual(self.branch, self.git("branch", "--show-current"))


def stack_attempt_record(**overrides):
    attempt = {
        "id": "pr-7-attempt-1",
        "status": "planned",
        "attempt_number": 1,
        "strategy": "stack",
        "strategy_reason": "cascade",
        "strategy_warnings": [],
        "head_sha": "head1",
        "base_sha": "base1",
        "merge_base": None,
        "mergeable": "CONFLICTING",
        "merge_state_status": "DIRTY",
        "started_at": "2026-01-01T00:00:00Z",
        "conflicts": [],
        "conflict_signature": None,
        "published_head_sha": None,
        "mergeable_at_head_sha": None,
        "stack": {
            "number": 19578,
            "size": 2,
            "trunk": "main",
            "invoked_number": 7,
            "members": [
                {
                    "number": 19483,
                    "head_branch": "v143",
                    "base_branch": "main",
                    "mergeable": "MERGEABLE",
                    "head_sha": "aaa",
                    "base_sha": "base1",
                },
                {
                    "number": 7,
                    "head_branch": "feature",
                    "base_branch": "v143",
                    "mergeable": "CONFLICTING",
                    "head_sha": "head1",
                    "base_sha": "old-a",
                },
            ],
            "workspace": None,
            "members_after": None,
            "trunk_sha": "base1",
            "formatting_index": None,
            "formatting_checkpoints": [],
        },
    }
    stack_overrides = overrides.pop("stack", None)
    attempt.update(overrides)
    if stack_overrides:
        attempt["stack"].update(stack_overrides)
    return attempt


def stack_entry(
    position,
    number,
    head,
    base,
    mergeable="MERGEABLE",
    oid=None,
    base_oid=None,
    retargeted_from=None,
    force_pushed=False,
    state="OPEN",
    commit_oids=None,
):
    head_oid = oid or f"oid{number}"
    commit_oids = list(commit_oids or [head_oid])
    entry = {
        "position": position,
        "pullRequest": {
            "number": number,
            "headRefName": head,
            "baseRefName": base,
            "mergeable": mergeable,
            "headRefOid": head_oid,
            "baseRefOid": base_oid or f"baseoid{number}",
            "state": state,
            "commits": {
                "totalCount": len(commit_oids),
                "pageInfo": {"hasNextPage": False},
                "nodes": [
                    {"commit": {"oid": commit_oid}} for commit_oid in commit_oids
                ],
            },
        },
    }
    events = []
    if retargeted_from is not None:
        events.append(
            {
                "__typename": "AutomaticBaseChangeSucceededEvent",
                "oldBase": retargeted_from,
                "newBase": base,
                "createdAt": "2026-08-24T06:06:04Z",
            }
        )
    if force_pushed:
        events.append(
            {
                "__typename": "HeadRefForcePushedEvent",
                "createdAt": "2026-08-24T07:06:04Z",
                "beforeCommit": {"oid": "before"},
                "afterCommit": {"oid": "after"},
            }
        )
    if events:
        entry["pullRequest"]["timelineItems"] = {
            "pageInfo": {"hasNextPage": False},
            "nodes": events,
        }
    return entry


class ParseStackTest(unittest.TestCase):
    def raw(self, nodes, base="main"):
        return {
            "id": "S_1",
            "number": 100,
            "size": len(nodes),
            "baseRefName": base,
            "entries": {"nodes": nodes},
        }

    def test_members_are_ordered_by_position(self):
        stack = MODULE.parse_stack(
            self.raw(
                [
                    stack_entry(1, 7, "feature", "v143", "CONFLICTING"),
                    stack_entry(0, 5, "v143", "main"),
                ]
            )
        )
        self.assertEqual([5, 7], [member["number"] for member in stack["members"]])
        self.assertEqual("main", stack["trunk"])
        self.assertEqual("oid7", stack["members"][1]["head_sha"])

    def test_an_automatic_base_retarget_records_the_previous_branch(self):
        stack = MODULE.parse_stack(
            self.raw(
                [
                    stack_entry(
                        0,
                        7,
                        "feature",
                        "main",
                        base_oid="merged",
                        retargeted_from="lower",
                    )
                ]
            )
        )
        self.assertEqual("lower", stack["members"][0]["retargeted_from"])

    def test_a_force_push_is_recorded_for_lineage_recovery(self):
        stack = MODULE.parse_stack(
            self.raw([stack_entry(0, 7, "feature", "main", force_pushed=True)])
        )
        self.assertTrue(stack["members"][0]["force_pushed"])

    def test_incomplete_branch_history_is_rejected(self):
        entry = stack_entry(0, 7, "feature", "main", force_pushed=True)
        entry["pullRequest"]["timelineItems"]["pageInfo"]["hasNextPage"] = True
        with self.assertRaisesRegex(MODULE.WorkflowError, "incomplete branch history"):
            MODULE.parse_stack(self.raw([entry]))

    def test_paginated_commit_history_is_recorded_as_incomplete(self):
        entry = stack_entry(0, 7, "feature", "main")
        entry["pullRequest"]["commits"]["totalCount"] = 2
        entry["pullRequest"]["commits"]["pageInfo"]["hasNextPage"] = True
        stack = MODULE.parse_stack(self.raw([entry]))
        self.assertFalse(stack["members"][0]["commits_complete"])

    def test_a_commit_count_mismatch_is_rejected(self):
        entry = stack_entry(0, 7, "feature", "main")
        entry["pullRequest"]["commits"]["totalCount"] = 2
        with self.assertRaisesRegex(MODULE.WorkflowError, "incomplete commit history"):
            MODULE.parse_stack(self.raw([entry]))

    def test_an_unreadable_member_is_a_hard_error(self):
        nodes = [
            stack_entry(0, 5, "v143", "main"),
            {"position": 1, "pullRequest": None},
        ]
        with self.assertRaisesRegex(MODULE.WorkflowError, "unreadable member"):
            MODULE.parse_stack(self.raw(nodes))

    def test_a_member_missing_a_required_field_is_a_hard_error(self):
        node = stack_entry(0, 5, "v143", "main")
        del node["pullRequest"]["headRefOid"]
        with self.assertRaisesRegex(MODULE.WorkflowError, "missing a required field"):
            MODULE.parse_stack(self.raw([node]))

    def test_a_stack_with_no_trunk_is_a_hard_error(self):
        raw = self.raw([stack_entry(0, 5, "v143", "main")])
        del raw["baseRefName"]
        with self.assertRaisesRegex(MODULE.WorkflowError, "no trunk branch"):
            MODULE.parse_stack(raw)


class StackMembershipTest(unittest.TestCase):
    def membership(self, *, stack, default="main"):
        payload = {
            "data": {
                "repository": {
                    "defaultBranchRef": None if default is None else {"name": default},
                    "pullRequest": {"stack": stack},
                }
            }
        }
        with mock.patch.object(MODULE, "graphql", return_value=payload) as query:
            result = MODULE.stack_membership(pr_metadata())
        self.query = query
        return result

    def test_a_null_stack_means_no_native_stack(self):
        result = self.membership(stack=None)
        self.assertIsNone(result["stack"])

    def test_the_default_branch_is_read_from_the_api_not_assumed(self):
        result = self.membership(stack=None, default="develop")
        self.assertEqual("develop", result["default_branch"])

    def test_a_present_stack_is_parsed_into_members(self):
        raw = {
            "id": "S_1",
            "number": 100,
            "size": 1,
            "baseRefName": "main",
            "entries": {"nodes": [stack_entry(0, 5, "v143", "main")]},
        }
        result = self.membership(stack=raw)
        self.assertEqual([5], [member["number"] for member in result["stack"]["members"]])

    def test_merged_prefix_is_omitted_from_the_active_stack(self):
        raw = {
            "id": "S_1",
            "number": 100,
            "size": 4,
            "baseRefName": "main",
            "entries": {
                "nodes": [
                    stack_entry(0, 3, "merged-a", "main", state="MERGED"),
                    stack_entry(1, 5, "merged-b", "main", state="MERGED"),
                    stack_entry(2, 7, "feature", "main"),
                    stack_entry(3, 9, "tip", "feature"),
                ]
            },
        }

        result = self.membership(stack=raw)

        stack = result["stack"]
        self.assertEqual([7, 9], [member["number"] for member in stack["members"]])
        self.assertEqual(
            [3, 5], [member["number"] for member in stack["inactive_members"]]
        )
        self.assertEqual(2, stack["size"])
        MODULE.validate_stack_snapshot({**stack, "invoked_number": 7})
        partial = MODULE.propagation_stack(stack, 7, "oid7")
        self.assertEqual([9], [member["number"] for member in partial["members"]])

    def test_inactive_member_that_leaves_a_stack_gap_is_rejected(self):
        raw = {
            "id": "S_1",
            "number": 100,
            "size": 3,
            "baseRefName": "main",
            "entries": {
                "nodes": [
                    stack_entry(0, 3, "lower", "main"),
                    stack_entry(1, 5, "middle", "lower", state="CLOSED"),
                    stack_entry(2, 7, "tip", "middle"),
                ]
            },
        }

        with self.assertRaisesRegex(
            MODULE.WorkflowError,
            "not linear at pull request #7 after omitting inactive pull request #5",
        ):
            self.membership(stack=raw)

    def test_a_missing_default_branch_is_a_hard_error(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "no.*default branch"):
            self.membership(stack=None, default=None)

    def test_the_entries_page_is_bounded_so_the_stack_field_is_not_nulled(self):
        self.membership(stack=None)
        variables = self.query.call_args[0][1]
        self.assertEqual(MODULE.STACK_ENTRIES_PAGE, variables["first"])
        query = self.query.call_args[0][0]
        self.assertIn("HEAD_REF_FORCE_PUSHED_EVENT", query)
        self.assertIn("pageInfo { hasNextPage }", query)

    def test_a_retargeted_member_is_enriched_with_its_merged_predecessor(self):
        raw = {
            "id": "S_1",
            "number": 100,
            "size": 1,
            "baseRefName": "main",
            "entries": {
                "nodes": [
                    stack_entry(
                        0,
                        7,
                        "feature",
                        "main",
                        base_oid="merged",
                        retargeted_from="lower",
                    )
                ]
            },
        }
        predecessor = {
            "number": 5,
            "head_branch": "lower",
            "head_sha": "old-lower",
            "merge_sha": "merged",
        }
        with mock.patch.object(
            MODULE, "merged_predecessor", return_value=predecessor
        ) as resolve:
            result = self.membership(stack=raw)
        self.assertEqual(
            predecessor, result["stack"]["members"][0]["merged_predecessor"]
        )
        resolve.assert_called_once()

    def test_a_merged_predecessor_reuses_its_complete_native_stack_commits(self):
        raw = {
            "id": "S_1",
            "number": 100,
            "size": 2,
            "baseRefName": "main",
            "entries": {
                "nodes": [
                    stack_entry(
                        0,
                        5,
                        "lower",
                        "main",
                        state="MERGED",
                        commit_oids=["parent-a", "oid5"],
                    ),
                    stack_entry(
                        1,
                        7,
                        "feature",
                        "main",
                        base_oid="merged",
                        retargeted_from="lower",
                    ),
                ]
            },
        }
        predecessor = {
            "number": 5,
            "head_branch": "lower",
            "head_sha": "oid5",
            "merge_sha": "merged",
        }
        with mock.patch.object(
            MODULE, "merged_predecessor", return_value=predecessor
        ):
            result = self.membership(stack=raw)

        frozen = result["stack"]["members"][0]["merged_predecessor"]
        self.assertEqual(["parent-a", "oid5"], frozen["commits"])
        self.assertTrue(frozen["commits_complete"])

    def test_only_the_bottom_member_resolves_a_merged_predecessor(self):
        raw = {
            "id": "S_1",
            "number": 100,
            "size": 2,
            "baseRefName": "main",
            "entries": {
                "nodes": [
                    stack_entry(0, 5, "lower", "main"),
                    stack_entry(
                        1,
                        7,
                        "feature",
                        "lower-renamed",
                        retargeted_from="lower",
                    ),
                ]
            },
        }
        with mock.patch.object(
            MODULE, "merged_predecessor", return_value=None
        ) as resolve:
            result = self.membership(stack=raw)
        resolve.assert_called_once_with(
            pr_metadata(), result["stack"]["members"][0]
        )
        self.assertIsNone(
            result["stack"]["members"][1]["merged_predecessor"]
        )


class MergedPredecessorTest(unittest.TestCase):
    def test_matches_the_retarget_event_to_the_exact_merge_result(self):
        member = {
            "retargeted_from": "lower",
            "base_sha": "merged",
        }
        pulls = [
            {
                "number": 5,
                "merged_at": "2026-08-24T06:06:03Z",
                "merge_commit_sha": "merged",
                "head": {"ref": "lower", "sha": "old-lower"},
            }
        ]
        with mock.patch.object(MODULE, "gh_json", return_value=pulls) as gh:
            predecessor = MODULE.merged_predecessor(pr_metadata(), member)
        self.assertEqual(
            {
                "number": 5,
                "head_branch": "lower",
                "head_sha": "old-lower",
                "merge_sha": "merged",
            },
            predecessor,
        )
        self.assertIn("--paginate", gh.call_args.args[0])

    def test_a_different_merge_result_is_not_used_as_the_old_base(self):
        member = {
            "retargeted_from": "lower",
            "base_sha": "expected",
        }
        pulls = [
            {
                "number": 5,
                "merged_at": "2026-08-24T06:06:03Z",
                "merge_commit_sha": "other",
                "head": {"ref": "lower", "sha": "old-lower"},
            }
        ]
        with mock.patch.object(MODULE, "gh_json", return_value=pulls):
            self.assertIsNone(MODULE.merged_predecessor(pr_metadata(), member))


class MergeTreeConflictsTest(unittest.TestCase):
    def test_a_clean_merge_names_no_files(self):
        with mock.patch.object(
            MODULE, "git_try", return_value=completed(0, stdout="treeoid\n")
        ):
            self.assertEqual([], MODULE.merge_tree_conflicts(Path("."), "a", "b"))

    def test_conflicted_files_are_parsed_up_to_the_blank_line(self):
        out = (
            "treeoid\n"
            "docs/list.yaml\n"
            "src/app.py\n"
            "\n"
            "Auto-merging docs/list.yaml\n"
            "CONFLICT (content): Merge conflict in docs/list.yaml\n"
        )
        with mock.patch.object(
            MODULE, "git_try", return_value=completed(1, stdout=out)
        ):
            self.assertEqual(
                ["docs/list.yaml", "src/app.py"],
                MODULE.merge_tree_conflicts(Path("."), "a", "b"),
            )

    def test_a_git_error_is_not_read_as_clean(self):
        with mock.patch.object(
            MODULE, "git_try", return_value=completed(128, stderr="unknown revision")
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "could not test-merge"):
                MODULE.merge_tree_conflicts(Path("."), "a", "b")


class AdHocEscalationTest(unittest.TestCase):
    def escalate(self, base_conflicts, default_conflicts):
        with mock.patch.object(MODULE, "fetch_reference"), mock.patch.object(
            MODULE,
            "merge_tree_conflicts",
            side_effect=[base_conflicts, default_conflicts],
        ):
            return MODULE.ad_hoc_escalation(
                Path("."),
                "origin",
                pr_metadata(base_branch="feature-base"),
                "main",
                "defaultsha",
            )

    def test_a_conflict_with_the_default_branch_names_it(self):
        result = self.escalate([], ["docs/instrumentation-list.yaml"])
        self.assertIn("docs/instrumentation-list.yaml", result["reason"])
        self.assertIn("main", result["reason"])
        self.assertEqual(["docs/instrumentation-list.yaml"], result["default_conflicts"])

    def test_a_conflict_with_the_declared_base_names_it(self):
        result = self.escalate(["src/app.py"], [])
        self.assertIn("src/app.py", result["reason"])
        self.assertIn("feature-base", result["reason"])

    def test_clean_against_both_is_reported_honestly(self):
        result = self.escalate([], [])
        self.assertIn("neither merge", result["reason"])


class AttemptRepoRootTest(unittest.TestCase):
    def test_a_stack_attempt_stages_in_its_workspace(self):
        state = {"repo_root": "/session"}
        attempt = {"strategy": "stack", "stack": {"workspace": "/scratch"}}
        self.assertEqual(Path("/scratch"), MODULE.attempt_repo_root(state, attempt))

    def test_a_single_branch_attempt_stages_in_the_session_worktree(self):
        self.assertEqual(
            Path("/session"),
            MODULE.attempt_repo_root({"repo_root": "/session"}, {"strategy": "merge"}),
        )

    def test_a_stack_attempt_without_a_workspace_is_refused(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "no cascade workspace"):
            MODULE.attempt_repo_root({}, {"strategy": "stack", "stack": {}})


class CreateStackWorkspaceTest(unittest.TestCase):
    def clone_command(self, reference=None):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return completed(0)

        with mock.patch.object(MODULE, "run", side_effect=fake_run):
            workspace = MODULE.create_stack_workspace(
                pr_metadata(), reference=reference
            )
        self.addCleanup(MODULE.force_rmtree, workspace)
        return calls[0], workspace

    def test_borrows_local_objects_when_a_reference_is_given(self):
        reference = Path("/main/repo/.git")
        cmd, workspace = self.clone_command(reference=reference)
        self.assertEqual(
            ["gh", "repo", "clone", "owner/repo", str(workspace), "--"], cmd[:6]
        )
        self.assertIn("--reference-if-able", cmd)
        index = cmd.index("--reference-if-able")
        self.assertEqual(str(reference), cmd[index + 1])
        self.assertIn("--no-single-branch", cmd)

    def test_full_clone_when_no_reference_is_available(self):
        cmd, _ = self.clone_command(reference=None)
        self.assertNotIn("--reference-if-able", cmd)
        self.assertIn("--no-single-branch", cmd)


class LocalObjectSourceTest(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory(self)

    def test_resolves_the_common_object_store(self):
        common = self.directory / "common.git"
        (common / "objects").mkdir(parents=True)
        with mock.patch.object(
            MODULE, "git_try", return_value=completed(0, stdout=f"{common}\n")
        ):
            self.assertEqual(common, MODULE.local_object_source(self.directory))

    def test_resolves_a_relative_common_dir_against_the_repo_root(self):
        (self.directory / ".git" / "objects").mkdir(parents=True)
        with mock.patch.object(
            MODULE, "git_try", return_value=completed(0, stdout=".git")
        ):
            self.assertEqual(
                (self.directory / ".git").resolve(),
                MODULE.local_object_source(self.directory),
            )

    def test_none_when_the_objects_directory_is_absent(self):
        common = self.directory / "empty.git"
        common.mkdir()
        with mock.patch.object(
            MODULE, "git_try", return_value=completed(0, stdout=str(common))
        ):
            self.assertIsNone(MODULE.local_object_source(self.directory))

    def test_none_when_rev_parse_fails(self):
        with mock.patch.object(MODULE, "git_try", return_value=completed(1)):
            self.assertIsNone(MODULE.local_object_source(self.directory))


class DissociateWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.workspace = temporary_directory(self)
        self.alternates = (
            self.workspace / ".git" / "objects" / "info" / "alternates"
        )

    def write_alternates(self, text="/main/repo/.git/objects"):
        self.alternates.parent.mkdir(parents=True, exist_ok=True)
        self.alternates.write_text(text, encoding="utf-8")

    def test_no_alternates_is_a_no_op(self):
        self.assertIsNone(MODULE.dissociate_workspace(self.workspace))

    def test_repacks_and_drops_the_alternates(self):
        self.write_alternates()

        def fake_git_try(_root, *args):
            if args[:1] == ("rev-parse",):
                return completed(0, stdout="HEADSHA")
            return completed(0)

        with mock.patch.object(MODULE, "git_try", side_effect=fake_git_try):
            result = MODULE.dissociate_workspace(self.workspace)
        self.assertIsNone(result)
        self.assertFalse(self.alternates.exists())

    def test_names_the_borrowed_source_when_repack_fails(self):
        self.write_alternates("/gone/.git/objects")
        with mock.patch.object(
            MODULE, "git_try", return_value=completed(1, stderr="boom")
        ):
            result = MODULE.dissociate_workspace(self.workspace)
        self.assertIn("/gone/.git/objects", result)
        self.assertIn("repack", result)
        self.assertTrue(self.alternates.exists())


class CleanupReplacedStackAttemptTest(unittest.TestCase):
    def workspace(self):
        workspace = temporary_directory(self) / "stack"
        workspace.mkdir()
        return workspace

    def test_a_preserved_failed_publish_workspace_is_removed(self):
        workspace = self.workspace()
        state = {
            "attempt": stack_attempt_record(
                status="resolved", stack={"workspace": str(workspace)}
            )
        }
        MODULE.cleanup_replaced_stack_attempt(state)
        self.assertFalse(workspace.exists())
        self.assertIsNone(state["attempt"]["stack"]["workspace"])

    def test_a_published_attempt_never_leaves_its_workspace_owning_preflight(self):
        workspace = self.workspace()
        state = {
            "attempt": stack_attempt_record(
                status="published", stack={"workspace": str(workspace)}
            )
        }

        MODULE.cleanup_replaced_stack_attempt(state)

        self.assertFalse(workspace.exists())
        self.assertIsNone(state["attempt"]["stack"]["workspace"])

    def test_an_active_cascade_must_be_aborted_explicitly(self):
        workspace = self.workspace()
        state = {
            "attempt": stack_attempt_record(
                status="conflicted", stack={"workspace": str(workspace)}
            )
        }
        with self.assertRaisesRegex(MODULE.WorkflowError, "run stack-abort"):
            MODULE.cleanup_replaced_stack_attempt(state)
        self.assertTrue(workspace.exists())

    def test_published_refs_must_be_finalized_not_replaced(self):
        workspace = self.workspace()
        state = {
            "attempt": stack_attempt_record(
                status="published_refs", stack={"workspace": str(workspace)}
            )
        }
        with self.assertRaisesRegex(MODULE.WorkflowError, "re-run stack-publish"):
            MODULE.cleanup_replaced_stack_attempt(state)
        self.assertTrue(workspace.exists())

    def test_published_refs_still_block_preflight_after_cleanup(self):
        state = {
            "attempt": stack_attempt_record(
                status="published_refs", stack={"workspace": None}
            )
        }
        with self.assertRaisesRegex(MODULE.WorkflowError, "re-run stack-publish"):
            MODULE.cleanup_replaced_stack_attempt(state)


class StackCascadePlanTest(unittest.TestCase):
    def setUp(self):
        self.stack = stack_attempt_record()["stack"]

    def test_a_child_uses_the_unique_merge_base_when_its_parent_advanced(self):
        ancestry = {
            ("base1", "aaa"): True,
            ("aaa", "head1"): False,
            ("old-a", "aaa"): True,
            ("old-a", "head1"): True,
        }

        def ancestor(_root, left, right):
            return ancestry.get((left, right), False)

        def git_call(_root, *args):
            tips = {
                "refs/remotes/origin/main": "base1",
                "refs/remotes/origin/v143": "aaa",
                "refs/remotes/origin/feature": "head1",
            }
            if args[0] == "rev-parse" and args[1] in tips:
                return tips[args[1]]
            if args[0] == "merge-base":
                self.assertEqual(
                    ("merge-base", "--all", "aaa", "head1"), args
                )
                return "old-a"
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch.object(MODULE, "git", side_effect=git_call), mock.patch.object(
            MODULE, "is_ancestor", side_effect=ancestor
        ), mock.patch.object(MODULE, "git_try", return_value=completed(0)):
            plan = MODULE.prepare_stack_cascade(Path("/workspace"), self.stack)

        self.assertEqual("old-a", plan[1]["old_base"])
        self.assertEqual("v143", plan[1]["new_base"])

    def test_a_retargeted_bottom_member_uses_the_merged_predecessor_head(self):
        self.stack["members"][0]["base_sha"] = "merged-lower"
        self.stack["members"][0]["merged_predecessor"] = {
            "number": 4,
            "head_branch": "old-lower",
            "head_sha": "old-lower-head",
            "merge_sha": "merged-lower",
        }
        ancestry = {
            ("base1", "aaa"): False,
            ("old-lower-head", "aaa"): True,
            ("aaa", "head1"): True,
        }

        def ancestor(_root, left, right):
            return ancestry.get((left, right), False)

        def git_call(_root, *args):
            tips = {
                "refs/remotes/origin/main": "base1",
                "refs/remotes/origin/v143": "aaa",
                "refs/remotes/origin/feature": "head1",
            }
            if args[0] == "rev-parse" and args[1] in tips:
                return tips[args[1]]
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch.object(MODULE, "git", side_effect=git_call), mock.patch.object(
            MODULE, "is_ancestor", side_effect=ancestor
        ), mock.patch.object(MODULE, "git_try", return_value=completed(0)), mock.patch.object(
            MODULE, "fetch_merged_predecessor"
        ) as fetch:
            plan = MODULE.prepare_stack_cascade(Path("/workspace"), self.stack)

        self.assertEqual("old-lower-head", plan[0]["old_base"])
        self.assertEqual("refs/remotes/origin/main", plan[0]["new_base_ref"])
        fetch.assert_called_once_with(
            Path("/workspace"), self.stack["members"][0]["merged_predecessor"]
        )

    def test_a_retargeted_bottom_member_rejects_an_unrelated_predecessor_head(self):
        self.stack["members"][0]["base_sha"] = "merged-lower"
        self.stack["members"][0]["merged_predecessor"] = {
            "number": 4,
            "head_branch": "old-lower",
            "head_sha": "old-lower-head",
            "merge_sha": "merged-lower",
        }

        def git_call(_root, *args):
            tips = {
                "refs/remotes/origin/main": "base1",
                "refs/remotes/origin/v143": "aaa",
                "refs/remotes/origin/feature": "head1",
            }
            if args[0] == "rev-parse" and args[1] in tips:
                return tips[args[1]]
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch.object(MODULE, "git", side_effect=git_call), mock.patch.object(
            MODULE, "is_ancestor", return_value=False
        ), mock.patch.object(MODULE, "git_try", return_value=completed(0)), mock.patch.object(
            MODULE, "fetch_merged_predecessor"
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "not an ancestor"):
                MODULE.prepare_stack_cascade(Path("/workspace"), self.stack)

    def test_a_retargeted_bottom_member_accepts_an_exact_merged_result_boundary(self):
        member = self.stack["members"][0]
        member["base_sha"] = "merged-lower"
        member["commits"] = ["commit-a", "aaa"]
        member["commits_complete"] = True
        member["merged_predecessor"] = {
            "number": 4,
            "head_branch": "old-lower",
            "head_sha": "old-lower-head",
            "merge_sha": "merged-lower",
        }
        ancestry = {
            ("base1", "aaa"): False,
            ("old-lower-head", "aaa"): False,
            ("merged-lower", "aaa"): True,
            ("aaa", "head1"): True,
        }

        def git_call(_root, *args):
            tips = {
                "refs/remotes/origin/main": "base1",
                "refs/remotes/origin/v143": "aaa",
                "refs/remotes/origin/feature": "head1",
            }
            if args[0] == "rev-parse" and args[1] in tips:
                return tips[args[1]]
            if args[0] == "rev-list":
                self.assertEqual(
                    (
                        "rev-list",
                        "--reverse",
                        "--topo-order",
                        "merged-lower..aaa",
                    ),
                    args,
                )
                return "commit-a\naaa"
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch.object(MODULE, "git", side_effect=git_call), mock.patch.object(
            MODULE,
            "is_ancestor",
            side_effect=lambda _root, left, right: ancestry.get((left, right), False),
        ), mock.patch.object(MODULE, "git_try", return_value=completed(0)), mock.patch.object(
            MODULE, "fetch_merged_predecessor"
        ):
            plan = MODULE.prepare_stack_cascade(Path("/workspace"), self.stack)

        self.assertEqual("merged-lower", plan[0]["old_base"])

    def test_a_merged_result_boundary_requires_the_exact_recorded_commit_range(self):
        member = self.stack["members"][0]
        member["base_sha"] = "merged-lower"
        member["commits"] = ["commit-a", "aaa"]
        member["commits_complete"] = True
        member["merged_predecessor"] = {
            "number": 4,
            "head_branch": "old-lower",
            "head_sha": "old-lower-head",
            "merge_sha": "merged-lower",
        }

        def git_call(_root, *args):
            tips = {
                "refs/remotes/origin/main": "base1",
                "refs/remotes/origin/v143": "aaa",
                "refs/remotes/origin/feature": "head1",
            }
            if args[0] == "rev-parse" and args[1] in tips:
                return tips[args[1]]
            if args[0] == "rev-list":
                return "commit-a\nextra\naaa"
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch.object(MODULE, "git", side_effect=git_call), mock.patch.object(
            MODULE,
            "is_ancestor",
            side_effect=lambda _root, left, right: (left, right)
            == ("merged-lower", "aaa"),
        ), mock.patch.object(MODULE, "git_try", return_value=completed(0)), mock.patch.object(
            MODULE, "fetch_merged_predecessor"
        ):
            with self.assertRaisesRegex(
                MODULE.MergedPredecessorLineageError,
                "complete commit range",
            ):
                MODULE.prepare_stack_cascade(Path("/workspace"), self.stack)

    def test_a_merged_result_boundary_requires_a_complete_github_commit_list(self):
        member = self.stack["members"][0]
        member["base_sha"] = "merged-lower"
        member["commits"] = ["commit-a"]
        member["commits_complete"] = False
        member["merged_predecessor"] = {
            "number": 4,
            "head_branch": "old-lower",
            "head_sha": "old-lower-head",
            "merge_sha": "merged-lower",
        }

        def git_call(_root, *args):
            tips = {
                "refs/remotes/origin/main": "base1",
                "refs/remotes/origin/v143": "aaa",
                "refs/remotes/origin/feature": "head1",
            }
            if args[0] == "rev-parse" and args[1] in tips:
                return tips[args[1]]
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch.object(MODULE, "git", side_effect=git_call), mock.patch.object(
            MODULE,
            "is_ancestor",
            side_effect=lambda _root, left, right: (left, right)
            == ("merged-lower", "aaa"),
        ), mock.patch.object(MODULE, "git_try", return_value=completed(0)), mock.patch.object(
            MODULE, "fetch_merged_predecessor"
        ):
            with self.assertRaisesRegex(
                MODULE.MergedPredecessorLineageError,
                "complete pull request commit list",
            ):
                MODULE.prepare_stack_cascade(Path("/workspace"), self.stack)

    def test_complete_frozen_lists_recover_a_corrected_predecessor_tip(self):
        member = self.stack["members"][0]
        member["base_sha"] = "merged-lower"
        member["commits"] = [
            "parent-a",
            "child-a",
            "parent-boundary",
            "merge-parent",
            "child-b",
            "aaa",
        ]
        member["commits_complete"] = True
        member["merged_predecessor"] = {
            "number": 4,
            "head_branch": "old-lower",
            "head_sha": "parent-final",
            "merge_sha": "merged-lower",
            "commits": ["parent-a", "parent-boundary", "parent-final"],
            "commits_complete": True,
        }
        ancestry = {
            ("base1", "aaa"): False,
            ("parent-final", "aaa"): False,
            ("merged-lower", "aaa"): False,
            ("aaa", "head1"): True,
        }

        def git_call(_root, *args):
            tips = {
                "refs/remotes/origin/main": "base1",
                "refs/remotes/origin/v143": "aaa",
                "refs/remotes/origin/feature": "head1",
            }
            if args[0] == "rev-parse" and args[1] in tips:
                return tips[args[1]]
            if args == ("merge-base", "--all", "parent-final", "aaa"):
                return "parent-boundary"
            if args == (
                "rev-list",
                "--reverse",
                "--topo-order",
                "parent-boundary..aaa",
            ):
                return "child-a\nmerge-parent\nchild-b\naaa"
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch.object(MODULE, "git", side_effect=git_call), mock.patch.object(
            MODULE,
            "is_ancestor",
            side_effect=lambda _root, left, right: ancestry.get((left, right), False),
        ), mock.patch.object(MODULE, "git_try", return_value=completed(0)), mock.patch.object(
            MODULE, "fetch_merged_predecessor"
        ):
            plan = MODULE.prepare_stack_cascade(Path("/workspace"), self.stack)

        self.assertEqual("parent-boundary", plan[0]["old_base"])

    def test_multiple_merge_bases_are_rejected_as_ambiguous(self):
        def git_call(_root, *args):
            tips = {
                "refs/remotes/origin/main": "base1",
                "refs/remotes/origin/v143": "aaa",
                "refs/remotes/origin/feature": "head1",
            }
            if args[0] == "rev-parse" and args[1] in tips:
                return tips[args[1]]
            if args[0] == "merge-base":
                return "old-a\nother-a"
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch.object(MODULE, "git", side_effect=git_call), mock.patch.object(
            MODULE, "is_ancestor", return_value=False
        ), mock.patch.object(MODULE, "git_try", return_value=completed(0)):
            with self.assertRaisesRegex(MODULE.WorkflowError, "ambiguous"):
                MODULE.prepare_stack_cascade(Path("/workspace"), self.stack)

    def test_a_rewritten_parent_uses_its_patch_equivalent_prefix_as_the_boundary(self):
        self.stack["members"][1]["base_sha"] = "rewritten-parent"
        self.stack["members"][0]["force_pushed"] = True
        ancestry = {
            ("base1", "aaa"): True,
            ("aaa", "head1"): False,
            ("rewritten-parent", "aaa"): True,
        }

        def ancestor(_root, left, right):
            return ancestry.get((left, right), False)

        def git_call(_root, *args):
            tips = {
                "refs/remotes/origin/main": "base1",
                "refs/remotes/origin/v143": "aaa",
                "refs/remotes/origin/feature": "head1",
            }
            if args[0] == "rev-parse" and args[1] in tips:
                return tips[args[1]]
            if args[0] == "merge-base":
                return "shared"
            if args[0] == "rev-list":
                return (
                    "old-parent-one shared\n"
                    "old-parent-two old-parent-one\n"
                    "child-one old-parent-two\n"
                    "child-two child-one"
                )
            if args[0] == "cherry":
                return (
                    "- old-parent-one\n"
                    "- old-parent-two\n"
                    "+ child-one\n"
                    "+ child-two"
                )
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch.object(MODULE, "git", side_effect=git_call), mock.patch.object(
            MODULE, "is_ancestor", side_effect=ancestor
        ), mock.patch.object(MODULE, "git_try", return_value=completed(0)):
            plan = MODULE.prepare_stack_cascade(Path("/workspace"), self.stack)

        self.assertEqual("old-parent-two", plan[1]["old_base"])

    def test_a_rewritten_parent_rejects_interleaved_patch_equivalence(self):
        with mock.patch.object(
            MODULE,
            "is_ancestor",
            return_value=True,
        ), mock.patch.object(
            MODULE,
            "git",
            side_effect=[
                "old-parent shared\nchild-one old-parent\nparent-again child-one",
                "- old-parent\n+ child-one\n- parent-again",
            ],
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "interleaved"):
                MODULE.recover_rewritten_parent_boundary(
                    Path("/workspace"),
                    parent_sha="parent",
                    child_sha="child",
                    merge_base="shared",
                    historical_base="recorded",
                    parent_branch="parent-branch",
                    child_branch="child-branch",
                )

    def test_a_rewritten_parent_rejects_nonlinear_child_history(self):
        with mock.patch.object(
            MODULE,
            "is_ancestor",
            return_value=True,
        ), mock.patch.object(
            MODULE,
            "git",
            return_value="merge shared second-parent",
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "not linear"):
                MODULE.recover_rewritten_parent_boundary(
                    Path("/workspace"),
                    parent_sha="parent",
                    child_sha="child",
                    merge_base="shared",
                    historical_base="recorded",
                    parent_branch="parent-branch",
                    child_branch="child-branch",
                )

    def test_a_rewritten_parent_requires_a_parent_prefix_and_child_suffix(self):
        cases = {
            "no equivalent prefix": (
                "child-one shared\nchild-two child-one",
                "+ child-one\n+ child-two",
            ),
            "no child suffix": (
                "old-one shared\nold-two old-one",
                "- old-one\n- old-two",
            ),
        }
        for label, (history, cherry) in cases.items():
            with self.subTest(label=label), mock.patch.object(
                MODULE,
                "is_ancestor",
                return_value=True,
            ), mock.patch.object(
                MODULE,
                "git",
                side_effect=[history, cherry],
            ):
                with self.assertRaisesRegex(
                    MODULE.WorkflowError, "parent-equivalent prefix"
                ):
                    MODULE.recover_rewritten_parent_boundary(
                        Path("/workspace"),
                        parent_sha="parent",
                        child_sha="child",
                        merge_base="shared",
                        historical_base="recorded",
                        parent_branch="parent-branch",
                        child_branch="child-branch",
                    )

    def test_unrelated_parent_and_child_are_rejected(self):
        def git_call(_root, *args):
            tips = {
                "refs/remotes/origin/main": "base1",
                "refs/remotes/origin/v143": "aaa",
                "refs/remotes/origin/feature": "head1",
            }
            if args[0] == "rev-parse" and args[1] in tips:
                return tips[args[1]]
            if args[0] == "merge-base":
                raise MODULE.WorkflowError("no merge base")
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch.object(MODULE, "git", side_effect=git_call), mock.patch.object(
            MODULE, "is_ancestor", return_value=False
        ), mock.patch.object(MODULE, "git_try", return_value=completed(0)):
            with self.assertRaisesRegex(MODULE.WorkflowError, "no safe lineage"):
                MODULE.prepare_stack_cascade(Path("/workspace"), self.stack)

    def test_a_stale_remote_head_is_rejected_before_local_refs_move(self):
        def git_call(_root, *args):
            tips = {
                "refs/remotes/origin/main": "base1",
                "refs/remotes/origin/v143": "someoneelse",
            }
            if args[0] == "rev-parse" and args[1] in tips:
                return tips[args[1]]
            raise AssertionError(f"unexpected git call: {args}")

        with mock.patch.object(MODULE, "git", side_effect=git_call), mock.patch.object(
            MODULE, "git_try", return_value=completed(0)
        ) as git_try:
            with self.assertRaisesRegex(MODULE.WorkflowError, "moved before"):
                MODULE.prepare_stack_cascade(Path("/workspace"), self.stack)
        self.assertFalse(
            any(call.args[1:2] == ("update-ref",) for call in git_try.call_args_list)
        )


class FetchMergedPredecessorTest(unittest.TestCase):
    def test_fetches_the_frozen_pull_ref_and_verifies_its_head(self):
        predecessor = {"number": 4, "head_sha": "old-lower-head"}
        with mock.patch.object(
            MODULE, "git_try", return_value=completed(0)
        ) as fetch, mock.patch.object(
            MODULE, "git", return_value="old-lower-head"
        ):
            MODULE.fetch_merged_predecessor(Path("/workspace"), predecessor)
        fetch.assert_called_once_with(
            Path("/workspace"),
            "fetch",
            "--no-tags",
            "origin",
            "refs/pull/4/head",
        )

    def test_a_changed_pull_ref_is_rejected(self):
        predecessor = {"number": 4, "head_sha": "expected"}
        with mock.patch.object(
            MODULE, "git_try", return_value=completed(0)
        ), mock.patch.object(MODULE, "git", return_value="changed"):
            with self.assertRaisesRegex(MODULE.WorkflowError, "expected"):
                MODULE.fetch_merged_predecessor(
                    Path("/workspace"), predecessor
                )


class AtomicStackPushTest(unittest.TestCase):
    def test_expected_repository_requires_one_matching_origin_push_url(self):
        stack = stack_attempt_record(status="resolved")["stack"]
        with mock.patch.object(
            MODULE,
            "git_try",
            return_value=completed(
                0, stdout="https://github.com/example/expected.git\n"
            ),
        ), mock.patch.object(
            MODULE, "run", return_value=completed(0)
        ) as runner:
            MODULE.atomic_stack_push(
                Path("/workspace"), stack, "example/expected"
            )
        runner.assert_called_once()

    def test_mismatched_origin_blocks_atomic_publication(self):
        stack = stack_attempt_record(status="resolved")["stack"]
        with mock.patch.object(
            MODULE,
            "git_try",
            return_value=completed(
                0, stdout="https://github.com/example/other.git\n"
            ),
        ), mock.patch.object(MODULE, "run") as runner, self.assertRaisesRegex(
            MODULE.WorkflowError, "expected example/expected"
        ):
            MODULE.atomic_stack_push(
                Path("/workspace"), stack, "example/expected"
            )
        runner.assert_not_called()

    def test_every_member_uses_an_exact_lease_in_one_atomic_push(self):
        stack = stack_attempt_record(
            status="resolved",
            stack={
                "members_after": [
                    {"number": 19483, "head_branch": "v143", "head_sha": "newa"},
                    {"number": 7, "head_branch": "feature", "head_sha": "newf"},
                ]
            },
        )["stack"]
        with mock.patch.object(
            MODULE, "run", return_value=completed(0)
        ) as runner:
            result = MODULE.atomic_stack_push(Path("/workspace"), stack)
        self.assertEqual(0, result.returncode)
        command = runner.call_args.args[0]
        self.assertEqual(
            ["git", "-C", str(Path("/workspace")), "push", "--atomic"],
            command[:5],
        )
        self.assertIn("--force-with-lease=refs/heads/v143:aaa", command)
        self.assertIn("--force-with-lease=refs/heads/feature:head1", command)
        self.assertIn("newa:refs/heads/v143", command)
        self.assertIn("newf:refs/heads/feature", command)
        self.assertNotIn("refs/heads/v143:refs/heads/v143", command)
        self.assertNotIn("refs/heads/feature:refs/heads/feature", command)
        self.assertFalse(any("main" in argument for argument in command))


class ValidateRebasedStackTest(unittest.TestCase):
    def test_a_descendant_that_misses_its_rebased_parent_is_rejected(self):
        stack = stack_attempt_record()["stack"]
        tips = [
            {"number": 19483, "head_branch": "v143", "head_sha": "newa"},
            {"number": 7, "head_branch": "feature", "head_sha": "newf"},
        ]
        with mock.patch.object(
            MODULE, "capture_member_tips", return_value=tips
        ), mock.patch.object(
            MODULE, "is_ancestor", side_effect=[True, False]
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "does not contain.*expected parent"
            ):
                MODULE.validate_rebased_stack(Path("/workspace"), stack)


class CurrentStackSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.stack = stack_attempt_record()["stack"]

    def test_an_unchanged_stack_with_no_new_dependents_passes(self):
        with mock.patch.object(
            MODULE,
            "stack_membership",
            return_value={"default_branch": "main", "stack": self.stack},
        ), mock.patch.object(
            MODULE, "external_stack_dependents", return_value=[]
        ):
            self.assertIsNone(
                MODULE.require_current_stack_snapshot(pr_metadata(), self.stack)
            )

    def test_changed_membership_is_rejected_before_publication(self):
        changed = json.loads(json.dumps(self.stack))
        changed["members"][1]["base_branch"] = "other"
        with mock.patch.object(
            MODULE,
            "stack_membership",
            return_value={"default_branch": "main", "stack": changed},
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "stack changed"):
                MODULE.require_current_stack_snapshot(pr_metadata(), self.stack)

    def test_a_new_external_dependent_is_rejected_before_publication(self):
        dependent = {
            "number": 99,
            "url": "https://github.com/owner/repo/pull/99",
            "head_branch": "outside",
            "base_branch": "v143",
        }
        with mock.patch.object(
            MODULE,
            "stack_membership",
            return_value={"default_branch": "main", "stack": self.stack},
        ), mock.patch.object(
            MODULE, "external_stack_dependents", return_value=[dependent]
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "#99.*v143"):
                MODULE.require_current_stack_snapshot(pr_metadata(), self.stack)


class RepairNativeStackTopologyTest(unittest.TestCase):
    def setUp(self):
        self.stack = {
            "number": 100,
            "size": 4,
            "trunk": "main",
            "invoked_number": 2,
            "members": [
                {
                    "number": 1,
                    "head_branch": "lower",
                    "base_branch": "main",
                    "head_sha": "one",
                    "base_sha": "base",
                },
                {
                    "number": 2,
                    "head_branch": "feature",
                    "base_branch": "main",
                    "head_sha": "two",
                    "base_sha": "base",
                },
                {
                    "number": 3,
                    "head_branch": "child",
                    "base_branch": "feature",
                    "head_sha": "three",
                    "base_sha": "two",
                },
                {
                    "number": 4,
                    "head_branch": "grandchild",
                    "base_branch": "child",
                    "head_sha": "four",
                    "base_sha": "three",
                },
            ],
        }
        self.repaired = {
            "number": 101,
            "size": 3,
            "trunk": "main",
            "members": self.stack["members"][1:],
        }
        self.singleton = {
            "number": 102,
            "size": 1,
            "trunk": "main",
            "members": self.stack["members"][:1],
        }

    def test_members_split_into_maximal_direct_base_chains(self):
        self.assertEqual(
            [[1], [2, 3, 4]],
            [
                [member["number"] for member in segment]
                for segment in MODULE.linear_stack_segments(self.stack)
            ],
        )
        self.assertEqual(
            [
                {
                    "number": 2,
                    "head_branch": "feature",
                    "base_branch": "main",
                    "expected_base": "lower",
                }
            ],
            MODULE.stack_base_mismatches(self.stack),
        )

    def test_a_nonadjacent_parent_and_child_remain_in_one_repaired_chain(self):
        self.stack["members"] = [
            self.stack["members"][0],
            self.stack["members"][1],
            {
                "number": 3,
                "head_branch": "child",
                "base_branch": "lower",
                "head_sha": "three",
                "base_sha": "one",
            },
        ]
        self.assertEqual(
            [[1, 3], [2]],
            [
                [member["number"] for member in segment]
                for segment in MODULE.linear_stack_segments(self.stack)
            ],
        )

    def test_a_fork_starts_new_chains_below_the_shared_parent(self):
        self.stack["members"] = [
            self.stack["members"][0],
            {
                "number": 2,
                "head_branch": "left",
                "base_branch": "lower",
                "head_sha": "two",
                "base_sha": "one",
            },
            {
                "number": 3,
                "head_branch": "right",
                "base_branch": "lower",
                "head_sha": "three",
                "base_sha": "one",
            },
            {
                "number": 4,
                "head_branch": "left-child",
                "base_branch": "left",
                "head_sha": "four",
                "base_sha": "two",
            },
        ]
        self.assertEqual(
            [[1], [2, 4], [3]],
            [
                [member["number"] for member in segment]
                for segment in MODULE.linear_stack_segments(self.stack)
            ],
        )

    def test_a_cycle_with_a_fork_is_rejected_before_stack_membership_changes(self):
        self.stack["members"] = [
            {
                "number": 1,
                "head_branch": "a",
                "base_branch": "b",
            },
            {
                "number": 2,
                "head_branch": "b",
                "base_branch": "c",
            },
            {
                "number": 3,
                "head_branch": "c",
                "base_branch": "b",
            },
        ]
        with self.assertRaisesRegex(MODULE.WorkflowError, "direct-base cycle"):
            MODULE.linear_stack_segments(self.stack)

    def test_a_malformed_stack_is_dissolved_and_linear_segments_are_recreated(self):
        observed = [
            self.stack,
            self.stack,
            self.stack,
            self.stack,
            self.singleton,
            None,
            None,
            None,
            self.singleton,
            self.repaired,
        ]
        with mock.patch.object(
            MODULE, "member_stack", side_effect=observed
        ), mock.patch.object(MODULE, "unstack_native_stack") as unstack, mock.patch.object(
            MODULE, "create_native_stack"
        ) as create:
            segments = MODULE.repair_native_stack_topology(
                pr_metadata(), self.stack
            )
        self.assertEqual([[1], [2, 3, 4]], segments)
        unstack.assert_called_once_with(pr_metadata(), 100)
        create.assert_called_once_with(pr_metadata(), [2, 3, 4])

    def test_retry_accepts_an_already_recreated_segment(self):
        observed = [
            self.singleton,
            self.repaired,
            self.repaired,
            self.repaired,
            self.singleton,
            self.repaired,
            self.repaired,
            self.repaired,
            self.singleton,
            self.repaired,
        ]
        with mock.patch.object(
            MODULE, "member_stack", side_effect=observed
        ), mock.patch.object(MODULE, "unstack_native_stack") as unstack, mock.patch.object(
            MODULE, "create_native_stack"
        ) as create:
            segments = MODULE.repair_native_stack_topology(
                pr_metadata(), self.stack
            )
        self.assertEqual([[1], [2, 3, 4]], segments)
        unstack.assert_not_called()
        create.assert_not_called()

    def test_a_one_member_native_stack_is_an_effective_unstacked_singleton(self):
        self.assertIsNone(MODULE.effective_stack_group(self.singleton, 1))
        self.assertEqual(
            (2, 3, 4), MODULE.effective_stack_group(self.repaired, 2)
        )

    def test_a_singleton_wrapper_with_changed_metadata_is_rejected(self):
        changed = json.loads(json.dumps(self.singleton))
        changed["members"][0]["head_sha"] = "changed"
        observed = [
            changed,
            self.repaired,
            self.repaired,
            self.repaired,
            changed,
            self.repaired,
            self.repaired,
            self.repaired,
            changed,
        ]
        with mock.patch.object(MODULE, "member_stack", side_effect=observed):
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "singleton stack wrapper"
            ):
                MODULE.repair_native_stack_topology(pr_metadata(), self.stack)

    def test_unstack_uses_the_versioned_native_stacks_endpoint(self):
        with mock.patch.object(
            MODULE, "run", return_value=completed(0)
        ) as runner:
            MODULE.unstack_native_stack(pr_metadata(), 100)
        command = runner.call_args.args[0]
        self.assertIn("X-GitHub-Api-Version: 2026-03-10", command)
        self.assertIn("repos/owner/repo/stacks/100/unstack", command)

    def test_create_sends_the_ordered_pull_request_numbers_as_json(self):
        with mock.patch.object(
            MODULE, "run", return_value=completed(0)
        ) as runner:
            MODULE.create_native_stack(pr_metadata(), [2, 3, 4])
        self.assertIn("repos/owner/repo/stacks", runner.call_args.args[0])
        self.assertEqual(
            {"pull_requests": [2, 3, 4]},
            json.loads(runner.call_args.kwargs["input_text"]),
        )


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class StackCascadeTopologyTest(GitTestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = tempfile.TemporaryDirectory()
        cls.template_repo = Path(cls.template.name).resolve() / "repo"
        cls.template_repo.mkdir()
        cls.git_in(cls.template_repo, "init", "--initial-branch", "main")
        cls.git_in(cls.template_repo, "config", "user.name", "Stack Test")
        cls.git_in(cls.template_repo, "config", "user.email", "stack@example.invalid")
        cls.git_in(cls.template_repo, "config", "commit.gpgsign", "false")
        cls.write_in(cls.template_repo, "base.txt", "base\n")
        cls.commit_in(cls.template_repo, "base")
        cls.main_head = cls.git_in(cls.template_repo, "rev-parse", "HEAD")

        cls.git_in(cls.template_repo, "checkout", "-b", "lower")
        cls.write_in(cls.template_repo, "lower.txt", "lower one\n")
        cls.commit_in(cls.template_repo, "lower one")
        cls.lower_old = cls.git_in(cls.template_repo, "rev-parse", "HEAD")

        cls.git_in(cls.template_repo, "checkout", "-b", "child")
        cls.write_in(cls.template_repo, "child.txt", "child\n")
        cls.commit_in(cls.template_repo, "child")
        cls.child_head = cls.git_in(cls.template_repo, "rev-parse", "HEAD")

        cls.git_in(cls.template_repo, "checkout", "-b", "grandchild")
        cls.write_in(cls.template_repo, "grandchild.txt", "grandchild\n")
        cls.commit_in(cls.template_repo, "grandchild")
        cls.grandchild_head = cls.git_in(cls.template_repo, "rev-parse", "HEAD")

        cls.git_in(cls.template_repo, "checkout", "lower")
        cls.write_in(cls.template_repo, "lower.txt", "lower one\nlower two\n")
        cls.commit_in(cls.template_repo, "lower two")
        cls.lower_head = cls.git_in(cls.template_repo, "rev-parse", "HEAD")

        cls.git_in(cls.template_repo, "checkout", "main")
        cls.git_in(cls.template_repo, "checkout", "-b", "unrelated")
        cls.write_in(cls.template_repo, "unrelated.txt", "unrelated\n")
        cls.commit_in(cls.template_repo, "unrelated")
        cls.unrelated_head = cls.git_in(cls.template_repo, "rev-parse", "HEAD")
        cls.git_in(cls.template_repo, "checkout", "--detach", "main")

    @classmethod
    def tearDownClass(cls):
        cls.template.cleanup()

    def setUp(self):
        self.root = temporary_directory(self)
        self.remote = self.root / "remote.git"
        self.source = self.root / "source"
        self.workspace = self.root / "workspace"
        self.git(
            self.root,
            "clone",
            "--quiet",
            "--bare",
            "--shared",
            str(self.template_repo),
            str(self.remote),
        )
        (self.remote / "HEAD").write_text(
            "ref: refs/heads/main\n", encoding="utf-8", newline="\n"
        )
        self.git(
            self.template_repo,
            "worktree",
            "add",
            "--quiet",
            str(self.source),
            "main",
        )
        self.addCleanup(self.reset_template)
        self.git(self.source, "remote", "add", "origin", str(self.remote))
        self.clone_workspace()
        self.stack = {
            "number": 10,
            "size": 3,
            "trunk": "main",
            "invoked_number": 3,
            "members": [
                {
                    "number": 1,
                    "head_branch": "lower",
                    "base_branch": "main",
                    "head_sha": self.lower_head,
                    "base_sha": self.main_head,
                },
                {
                    "number": 2,
                    "head_branch": "child",
                    "base_branch": "lower",
                    "head_sha": self.child_head,
                    "base_sha": self.lower_old,
                },
                {
                    "number": 3,
                    "head_branch": "grandchild",
                    "base_branch": "child",
                    "head_sha": self.grandchild_head,
                    "base_sha": self.child_head,
                },
            ],
            "workspace": str(self.workspace),
            "members_after": None,
        }

    def reset_template(self):
        self.git(self.source, "remote", "remove", "origin")
        self.git(
            self.template_repo,
            "worktree",
            "remove",
            "--force",
            str(self.source),
        )
        updates = "".join(
            f"update refs/heads/{branch} {head}\n"
            for branch, head in (
                ("main", self.main_head),
                ("lower", self.lower_head),
                ("child", self.child_head),
                ("grandchild", self.grandchild_head),
                ("unrelated", self.unrelated_head),
            )
        )
        updates += "delete refs/heads/rewritten-lower\n"
        self.git_in(
            self.template_repo,
            "update-ref",
            "--stdin",
            input_bytes=updates.encode("ascii"),
        )

    def git(self, root, *args, check=True):
        return self.git_in(root, *args, check=check)

    def write(self, root, name, content):
        self.write_in(root, name, content)

    def commit(self, root, message):
        self.commit_in(root, message)

    def clone_workspace(self):
        if self.workspace.exists():
            MODULE.force_rmtree(self.workspace)
        self.git(
            self.root,
            "clone",
            "--no-single-branch",
            str(self.remote),
            str(self.workspace),
        )
        self.git(self.workspace, "config", "user.name", "Stack Test")
        self.git(self.workspace, "config", "user.email", "stack@example.invalid")
        self.git(self.workspace, "config", "commit.gpgsign", "false")

    def remote_head(self, branch):
        process = subprocess.run(
            [
                "git",
                f"--git-dir={self.remote}",
                "rev-parse",
                f"refs/heads/{branch}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if process.returncode != 0:
            self.fail(process.stderr or process.stdout)
        return process.stdout.strip()

    def prepare_and_rebase(self):
        plan = MODULE.prepare_stack_cascade(self.workspace, self.stack)
        process = MODULE.run_stack_cascade(self.workspace, self.stack)
        while process.returncode == MODULE.STACK_FORMAT_EXIT:
            index = self.stack["formatting_index"]
            member = self.stack["plan"][index]
            tip = self.git(self.workspace, "rev-parse", member["branch_ref"])
            self.stack.setdefault("formatting_checkpoints", []).append(
                {
                    "index": index,
                    "number": member["number"],
                    "branch": member["branch"],
                    "before_sha": tip,
                    "after_sha": tip,
                    "changed": False,
                    "paths": [],
                    "command": None,
                }
            )
            process = MODULE.run_stack_cascade(
                self.workspace, self.stack, index + 1
            )
        if process.returncode == 0:
            self.stack["members_after"] = MODULE.capture_member_tips(
                self.workspace, self.stack
            )
        return plan, process

    def test_a_moved_lower_branch_cascades_through_every_descendant(self):
        self.git(self.workspace, "tag", "lower", "refs/remotes/origin/main")
        self.git(self.workspace, "tag", "child", "refs/remotes/origin/main")
        plan, process = self.prepare_and_rebase()
        self.assertEqual(0, process.returncode, process.stderr or process.stdout)
        self.assertEqual(self.lower_old, plan[1]["old_base"])
        tips = {
            member["head_branch"]: member["head_sha"]
            for member in self.stack["members_after"]
        }
        self.assertTrue(MODULE.is_ancestor(self.workspace, tips["lower"], tips["child"]))
        self.assertTrue(
            MODULE.is_ancestor(self.workspace, tips["child"], tips["grandchild"])
        )
        self.assertEqual(
            "child",
            self.git(self.workspace, "show", f"{tips['child']}:child.txt"),
        )
        self.assertEqual(
            "grandchild",
            self.git(
                self.workspace,
                "show",
                f"{tips['grandchild']}:grandchild.txt",
            ),
        )
        self.assertEqual(
            "lower one\nlower two",
            self.git(self.workspace, "show", f"{tips['child']}:lower.txt"),
        )
        self.assertEqual(self.lower_head, self.remote_head("lower"))
        self.assertEqual(self.child_head, self.remote_head("child"))
        self.assertEqual(self.grandchild_head, self.remote_head("grandchild"))

        pushed = MODULE.atomic_stack_push(self.workspace, self.stack)
        self.assertEqual(0, pushed.returncode, pushed.stderr or pushed.stdout)
        self.assertEqual(tips["lower"], self.remote_head("lower"))
        self.assertEqual(tips["child"], self.remote_head("child"))
        self.assertEqual(tips["grandchild"], self.remote_head("grandchild"))
        self.assertEqual(self.unrelated_head, self.remote_head("unrelated"))

    def test_a_formatted_parent_is_the_child_rebase_base(self):
        MODULE.prepare_stack_cascade(self.workspace, self.stack)
        first = MODULE.run_stack_cascade(self.workspace, self.stack)
        self.assertEqual(MODULE.STACK_FORMAT_EXIT, first.returncode)
        attempt = stack_attempt_record(
            status="formatting",
            stack=self.stack,
        )
        state_path = write_state(
            self.root,
            repo_root=str(self.workspace),
            attempt=attempt,
        )
        MODULE.format_stack_member(
            self.workspace,
            state_path,
            json.loads(state_path.read_text(encoding="utf-8")),
            attempt,
            format_command=[
                sys.executable,
                "-c",
                "from pathlib import Path; Path('lower.txt').write_text('formatted\\n')",
            ],
            no_format=False,
        )
        next_process = MODULE.run_stack_cascade(
            self.workspace, self.stack, self.stack["formatting_index"] + 1
        )
        self.assertEqual(MODULE.STACK_FORMAT_EXIT, next_process.returncode)
        child = self.git(self.workspace, "rev-parse", "refs/heads/child")
        self.assertEqual(
            "formatted",
            self.git(self.workspace, "show", f"{child}:lower.txt"),
        )

    def test_a_fast_forwarded_parent_uses_the_child_merge_base(self):
        self.stack["members"][1]["base_sha"] = self.lower_head

        plan, process = self.prepare_and_rebase()

        self.assertEqual(0, process.returncode, process.stderr or process.stdout)
        self.assertEqual(self.lower_old, plan[1]["old_base"])
        self.assertEqual(
            "child",
            self.git(self.workspace, "show", "refs/heads/child:child.txt"),
        )

    def test_an_advanced_trunk_uses_the_bottom_member_merge_base(self):
        self.git(self.source, "checkout", "main")
        self.write(self.source, "main.txt", "main advanced\n")
        self.commit(self.source, "advance main")
        advanced_main = self.git(self.source, "rev-parse", "HEAD")
        self.git(self.source, "push", "origin", "main")
        self.stack["members"][0]["base_sha"] = advanced_main
        self.clone_workspace()

        plan, process = self.prepare_and_rebase()

        self.assertEqual(0, process.returncode, process.stderr or process.stdout)
        self.assertEqual(self.main_head, plan[0]["old_base"])
        self.assertEqual(
            "main advanced",
            self.git(self.workspace, "show", "refs/heads/lower:main.txt"),
        )

    def test_a_conflict_stops_with_zero_publication(self):
        self.git(self.source, "checkout", "main")
        self.write(self.source, "lower.txt", "main changed this path\n")
        self.commit(self.source, "main conflict")
        self.git(self.source, "push", "origin", "main")
        self.clone_workspace()

        _plan, process = self.prepare_and_rebase()
        self.assertEqual(
            MODULE.STACK_CONFLICT_EXIT,
            process.returncode,
            process.stderr or process.stdout,
        )
        self.assertTrue(MODULE.rebase_in_progress(self.workspace))
        conflicts = MODULE.collect_stack_conflicts(self.workspace, self.stack)
        self.assertEqual(
            ["lower two", "lower one"],
            [commit["subject"] for commit in conflicts[0]["head_commits"]],
        )
        self.assertEqual(
            ["main conflict"],
            [commit["subject"] for commit in conflicts[0]["base_commits"]],
        )
        self.assertEqual(self.lower_head, self.remote_head("lower"))
        self.assertEqual(self.child_head, self.remote_head("child"))
        self.assertEqual(self.grandchild_head, self.remote_head("grandchild"))

    def test_a_stale_member_lease_rejects_every_update_atomically(self):
        _plan, process = self.prepare_and_rebase()
        self.assertEqual(0, process.returncode, process.stderr or process.stdout)

        self.git(self.source, "checkout", "child")
        self.write(self.source, "actor.txt", "actor\n")
        self.commit(self.source, "actor moves child")
        actor_head = self.git(self.source, "rev-parse", "HEAD")
        self.git(self.source, "push", "origin", "child")

        pushed = MODULE.atomic_stack_push(self.workspace, self.stack)
        self.assertNotEqual(0, pushed.returncode)
        self.assertEqual(self.lower_head, self.remote_head("lower"))
        self.assertEqual(actor_head, self.remote_head("child"))
        self.assertEqual(self.grandchild_head, self.remote_head("grandchild"))
        self.assertEqual(self.unrelated_head, self.remote_head("unrelated"))

    def test_a_force_rewritten_parent_is_rejected_as_unproven_lineage(self):
        self.git(self.source, "checkout", "main")
        self.git(self.source, "checkout", "-b", "rewritten-lower")
        self.write(self.source, "replacement.txt", "replacement lower\n")
        self.commit(self.source, "rewrite lower")
        rewritten = self.git(self.source, "rev-parse", "HEAD")
        self.git(self.source, "push", "--force", "origin", "HEAD:lower")
        self.stack["members"][0]["head_sha"] = rewritten
        self.stack["members"][0]["force_pushed"] = True
        self.clone_workspace()

        with self.assertRaisesRegex(MODULE.WorkflowError, "may have been rewritten"):
            MODULE.prepare_stack_cascade(self.workspace, self.stack)
        self.assertEqual(rewritten, self.remote_head("lower"))
        self.assertEqual(self.child_head, self.remote_head("child"))

    def test_a_patch_equivalent_parent_rewrite_recovers_the_child_boundary(self):
        self.git(self.source, "checkout", "main")
        self.git(self.source, "checkout", "-b", "rewritten-lower")
        self.git(self.source, "cherry-pick", self.lower_old)
        self.git(self.source, "commit", "--amend", "-m", "rewritten lower one")
        rewritten_base = self.git(self.source, "rev-parse", "HEAD")
        self.write(self.source, "lower.txt", "lower one\nlower two\n")
        self.commit(self.source, "rewritten lower two")
        rewritten_head = self.git(self.source, "rev-parse", "HEAD")
        self.git(self.source, "push", "--force", "origin", "HEAD:lower")
        self.stack["members"][0]["head_sha"] = rewritten_head
        self.stack["members"][0]["force_pushed"] = True
        self.stack["members"][1]["base_sha"] = rewritten_base
        self.clone_workspace()

        plan, process = self.prepare_and_rebase()

        self.assertEqual(0, process.returncode, process.stderr or process.stdout)
        self.assertEqual(self.lower_old, plan[1]["old_base"])
        self.assertEqual(
            "lower one\nlower two",
            self.git(self.workspace, "show", "refs/heads/child:lower.txt"),
        )
        self.assertEqual(
            "child",
            self.git(self.workspace, "show", "refs/heads/child:child.txt"),
        )


class ContinueStackCascadeTest(unittest.TestCase):
    def setUp(self):
        self.workspace = Path("/workspace")
        self.stack = {
            "current_index": 0,
            "plan": [
                {
                    "index": 0,
                    "branch": "feature",
                    "branch_ref": "refs/heads/feature",
                    "head_sha": "old",
                }
            ],
        }

    def test_an_empty_resolved_commit_is_skipped_before_the_cascade_resumes(self):
        with mock.patch.object(
            MODULE,
            "run",
            side_effect=[
                completed(1, stderr="No changes - did you forget to use 'git add'?"),
                completed(0),
            ],
        ) as runner, mock.patch.object(
            MODULE, "rebase_in_progress", return_value=True
        ), mock.patch.object(
            MODULE, "unmerged_entries", return_value=[]
        ), mock.patch.object(
            MODULE, "record_rebased_member"
        ) as record, mock.patch.object(
            MODULE, "run_stack_cascade", return_value=completed(0)
        ) as cascade:
            result = MODULE.continue_stack_cascade(self.workspace, self.stack)
        self.assertEqual(MODULE.STACK_FORMAT_EXIT, result.returncode)
        self.assertEqual(
            ["git", "-C", str(self.workspace), "rebase", "--skip"],
            runner.call_args_list[1].args[0],
        )
        record.assert_called_once_with(self.workspace, self.stack["plan"][0])
        cascade.assert_not_called()
        self.assertEqual(0, self.stack["formatting_index"])

    def test_a_skip_that_reaches_another_conflict_reports_that_conflict(self):
        with mock.patch.object(
            MODULE,
            "run",
            side_effect=[
                completed(1, stderr="No changes"),
                completed(1, stderr="CONFLICT"),
            ],
        ), mock.patch.object(
            MODULE, "rebase_in_progress", return_value=True
        ), mock.patch.object(
            MODULE,
            "unmerged_entries",
            side_effect=[[], [{"path": "next.txt"}]],
        ):
            result = MODULE.continue_stack_cascade(self.workspace, self.stack)
        self.assertEqual(MODULE.STACK_CONFLICT_EXIT, result.returncode)
        self.assertIn("CONFLICT", result.stderr)

    def test_a_non_conflict_continue_failure_is_not_an_empty_conflict(self):
        with mock.patch.object(
            MODULE, "run", return_value=completed(1, stderr="signing failed")
        ), mock.patch.object(
            MODULE, "rebase_in_progress", return_value=True
        ), mock.patch.object(
            MODULE, "unmerged_entries", return_value=[]
        ):
            result = MODULE.continue_stack_cascade(self.workspace, self.stack)
        self.assertEqual(1, result.returncode)
        self.assertIn("signing failed", result.stderr)

    def test_a_non_conflict_initial_failure_is_not_an_empty_conflict(self):
        stack = {
            "current_index": None,
            "plan": [
                {
                    "index": 0,
                    "branch": "feature",
                    "branch_ref": "refs/heads/feature",
                    "head_sha": "old",
                    "new_base_ref": "refs/remotes/origin/main",
                    "old_base": "base",
                }
            ],
        }
        with mock.patch.object(
            MODULE, "is_ancestor", return_value=False
        ), mock.patch.object(
            MODULE,
            "run_stack_member_rebase",
            return_value=completed(1, stderr="hook failed"),
        ), mock.patch.object(
            MODULE, "rebase_in_progress", return_value=True
        ), mock.patch.object(
            MODULE, "unmerged_entries", return_value=[]
        ):
            result = MODULE.run_stack_cascade(self.workspace, stack)
        self.assertEqual(1, result.returncode)
        self.assertIsNone(stack["current_index"])


class RefreshSingletonFallbackTest(unittest.TestCase):
    def setUp(self):
        self.stack = {
            "number": 167,
            "size": 1,
            "trunk": "main",
            "invoked_number": 7,
            "members": [
                {
                    "number": 7,
                    "head_branch": "feature",
                    "base_branch": "main",
                    "head_sha": "head1",
                    "base_sha": "base1",
                }
            ],
        }
        self.state = {
            "pr": pr_metadata(),
            "default_branch": "main",
            "relations": dict(NO_RELATIONS),
            "push_blockers": [],
            "external_dependents": [],
        }
        self.attempt = {
            "stack": self.stack,
            "fallback_requested_strategy": "auto",
        }

    def test_a_moved_base_refuses_the_fallback_refresh(self):
        with mock.patch.object(
            MODULE, "metadata_for", return_value=pr_metadata(base_sha="base2")
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "base_sha"):
                MODULE.refresh_singleton_merge_fallback(self.state, self.attempt)

    def test_the_refresh_replaces_every_remote_guard(self):
        relations = {"stacked_on": None, "dependents": [{"number": 8}]}
        refreshed_stack = {
            key: value for key, value in self.stack.items() if key != "invoked_number"
        }
        with mock.patch.object(
            MODULE, "metadata_for", return_value=pr_metadata()
        ), mock.patch.object(
            MODULE,
            "stack_membership",
            return_value={"default_branch": "main", "stack": refreshed_stack},
        ), mock.patch.object(
            MODULE, "stack_relations", return_value=relations
        ), mock.patch.object(
            MODULE,
            "repository_merge_methods",
            return_value=ALL_MERGE_METHODS,
        ), mock.patch.object(
            MODULE, "push_safety_blockers", return_value=["blocked"]
        ), mock.patch.object(
            MODULE, "external_stack_dependents", return_value=[{"number": 9}]
        ):
            MODULE.refresh_singleton_merge_fallback(self.state, self.attempt)

        self.assertEqual(relations, self.state["relations"])
        self.assertEqual(["blocked"], self.state["push_blockers"])
        self.assertEqual([{"number": 9}], self.state["external_dependents"])
        self.assertEqual("merge", self.attempt["fallback_strategy"]["strategy"])
        self.assertEqual(7, self.attempt["stack"]["invoked_number"])

    def test_merge_method_changes_replace_the_fallback_decision(self):
        with mock.patch.object(
            MODULE, "metadata_for", return_value=pr_metadata()
        ), mock.patch.object(
            MODULE,
            "stack_membership",
            return_value={"default_branch": "main", "stack": self.stack},
        ), mock.patch.object(
            MODULE, "stack_relations", return_value=dict(NO_RELATIONS)
        ), mock.patch.object(
            MODULE,
            "repository_merge_methods",
            return_value=REBASE_ONLY_MERGE_METHODS,
        ), mock.patch.object(
            MODULE, "push_safety_blockers", return_value=[]
        ), mock.patch.object(
            MODULE, "external_stack_dependents", return_value=[]
        ):
            MODULE.refresh_singleton_merge_fallback(self.state, self.attempt)

        self.assertEqual("rebase", self.attempt["fallback_strategy"]["strategy"])


class StackRebaseCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory(self)
        self.workspace = temporary_directory(self)

    def run_rebase(
        self,
        *,
        rebase=None,
        members_after=None,
        conflicts=None,
        rebase_in_progress=False,
        attempt=None,
        repair_segments=None,
        prepare_error=None,
        refresh_side_effect=None,
        **state_overrides,
    ):
        attempt = attempt or stack_attempt_record()
        state_path = write_state(
            self.directory,
            attempt=attempt,
            default_branch="main",
            push_blockers=[],
            external_dependents=[],
            **state_overrides,
        )

        args = SimpleNamespace(state=str(state_path))
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "create_stack_workspace", return_value=self.workspace
        ) as create_workspace, mock.patch.object(
            MODULE,
            "repair_native_stack_topology",
            return_value=repair_segments or [],
        ) as repair, mock.patch.object(
            MODULE,
            "prepare_stack_cascade",
            return_value=[],
            side_effect=prepare_error,
        ), mock.patch.object(
            MODULE, "run_stack_cascade", return_value=rebase or completed(0)
        ), mock.patch.object(
            MODULE, "validate_rebased_stack", return_value=members_after or []
        ), mock.patch.object(
            MODULE, "collect_stack_conflicts", return_value=conflicts or []
        ), mock.patch.object(
            MODULE, "rebase_in_progress", return_value=rebase_in_progress
        ), mock.patch.object(
            MODULE, "remove_stack_workspace"
        ) as remove, mock.patch.object(
            MODULE,
            "refresh_singleton_merge_fallback",
            side_effect=refresh_side_effect,
        ) as refresh, mock.patch.object(
            MODULE, "emit"
        ) as emit:
            self.error = None
            try:
                MODULE.command_stack_rebase(args)
            except MODULE.WorkflowError as error:
                self.error = error
        self.state_path = state_path
        self.remove = remove
        self.emit = emit
        self.create_workspace = create_workspace
        self.repair = repair
        self.refresh = refresh
        return emitted(emit) if emit.called else None

    def saved(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_a_clean_cascade_resolves_and_records_the_rebased_tips(self):
        tips = [
            {"number": 19483, "head_branch": "v143", "head_sha": "newa"},
            {"number": 7, "head_branch": "feature", "head_sha": "newf"},
        ]
        payload = self.run_rebase(rebase=completed(0), members_after=tips)
        self.assertEqual("resolved", payload["result"])
        self.assertEqual("stack-publish", payload["next"])
        state = self.saved()
        self.assertEqual("resolved", state["attempt"]["status"])
        self.assertEqual(tips, state["attempt"]["stack"]["members_after"])

    def test_a_malformed_native_stack_is_split_then_returns_to_preflight(self):
        attempt = stack_attempt_record()
        attempt["stack"]["members"][1]["base_branch"] = "main"
        payload = self.run_rebase(
            attempt=attempt, repair_segments=[[19483], [7]]
        )
        self.assertEqual("stack_repaired", payload["result"])
        self.assertEqual("preflight", payload["next"])
        self.assertEqual([[19483], [7]], payload["segments"])
        self.create_workspace.assert_not_called()
        self.repair.assert_called_once()
        state = self.saved()
        self.assertIsNone(state["attempt"])
        self.assertEqual([[19483], [7]], state["last_stack_repair"]["segments"])

    def test_a_rebase_conflict_surfaces_the_conflicted_files(self):
        payload = self.run_rebase(
            rebase=completed(MODULE.STACK_CONFLICT_EXIT, stderr="CONFLICT"),
            conflicts=[conflict_record("docs/list.yaml")],
        )
        self.assertEqual("conflicted", payload["result"])
        self.assertEqual(["docs/list.yaml"], payload["conflict_paths"])
        self.assertEqual("resolved", payload["next"])
        self.assertEqual("conflicted", self.saved()["attempt"]["status"])

    def test_a_completed_member_stops_for_formatting_before_descendants(self):
        attempt = stack_attempt_record()
        attempt["stack"]["plan"] = [
            {
                "index": 0,
                "number": 19483,
                "branch": "v143",
                "branch_ref": "refs/heads/v143",
            }
        ]
        attempt["stack"]["formatting_index"] = 0
        payload = self.run_rebase(
            attempt=attempt,
            rebase=completed(MODULE.STACK_FORMAT_EXIT),
        )
        self.assertEqual("formatting_required", payload["result"])
        self.assertEqual("stack-format", payload["next"])
        self.assertEqual("formatting", self.saved()["attempt"]["status"])

    def test_a_hard_rebase_failure_removes_the_workspace(self):
        self.run_rebase(rebase=completed(1, stderr="bad lineage"))
        self.assertIsNotNone(self.error)
        self.assertIn("bad lineage", str(self.error))
        self.remove.assert_called_once()

    def test_a_rebase_failure_without_unmerged_paths_is_not_a_conflict(self):
        self.run_rebase(
            rebase=completed(MODULE.STACK_CONFLICT_EXIT, stderr="signing failed"),
            conflicts=[],
            rebase_in_progress=True,
        )
        self.assertIsNotNone(self.error)
        self.assertIn("without any unmerged paths", str(self.error))
        self.remove.assert_called_once()

    def test_an_isolated_default_branch_member_falls_back_to_merge(self):
        attempt = stack_attempt_record(
            fallback_strategy={
                "strategy": "merge",
                "reason": "merge is safe",
                "warnings": [],
            },
            stack={
                "size": 1,
                "trunk": "main",
                "invoked_number": 7,
                "members": [
                    {
                        "number": 7,
                        "head_branch": "feature",
                        "base_branch": "main",
                        "head_sha": "head1",
                        "base_sha": "base1",
                    }
                ],
            },
        )
        error = MODULE.MergedPredecessorLineageError("unrecoverable lineage")

        payload = self.run_rebase(attempt=attempt, prepare_error=error)

        self.assertIsNone(self.error)
        self.assertEqual("single_branch_fallback", payload["result"])
        self.assertEqual("attempt", payload["next"])
        saved = self.saved()["attempt"]
        self.assertEqual("planned", saved["status"])
        self.assertEqual("merge", saved["strategy"])
        self.assertNotIn("stack", saved)
        self.assertEqual("unrecoverable lineage", saved["stack_fallback"]["reason"])
        self.remove.assert_called_once()
        self.refresh.assert_called_once()

    def test_refreshed_relations_can_cancel_the_single_branch_fallback(self):
        attempt = stack_attempt_record(
            fallback_strategy={
                "strategy": "merge",
                "reason": "merge is safe",
                "warnings": [],
            },
            stack={
                "size": 1,
                "trunk": "main",
                "invoked_number": 7,
                "members": [
                    {
                        "number": 7,
                        "head_branch": "feature",
                        "base_branch": "main",
                        "head_sha": "head1",
                        "base_sha": "base1",
                    }
                ],
            },
        )
        error = MODULE.MergedPredecessorLineageError("unrecoverable lineage")

        def add_dependent(state, _attempt):
            state["relations"]["dependents"] = [{"number": 8}]

        payload = self.run_rebase(
            attempt=attempt,
            prepare_error=error,
            refresh_side_effect=add_dependent,
        )

        self.assertIsNone(payload)
        self.assertIs(self.error, error)
        self.assertEqual("stack", self.saved()["attempt"]["strategy"])

    def test_a_multi_member_stack_does_not_fall_back_to_merge(self):
        attempt = stack_attempt_record(
            fallback_strategy={
                "strategy": "merge",
                "reason": "merge is safe",
                "warnings": [],
            }
        )
        error = MODULE.MergedPredecessorLineageError("unrecoverable lineage")

        self.run_rebase(attempt=attempt, prepare_error=error)

        self.assertIs(self.error, error)
        self.assertFalse(self.emit.called)
        self.remove.assert_called_once()

    def test_singleton_merge_fallback_rejects_each_unsafe_condition(self):
        def valid():
            state = {
                "default_branch": "main",
                "push_blockers": [],
                "external_dependents": [],
                "relations": dict(NO_RELATIONS),
                "pr": pr_metadata(base_branch="main"),
            }
            attempt = stack_attempt_record(
                fallback_strategy={
                    "strategy": "merge",
                    "reason": "merge is safe",
                    "warnings": [],
                },
                stack={
                    "size": 1,
                    "trunk": "main",
                    "invoked_number": 7,
                    "members": [{"number": 7}],
                },
            )
            return state, attempt

        cases = {
            "multiple members": lambda state, attempt: attempt["stack"].update(
                size=2
            ),
            "different invoked pull request": lambda state, attempt: attempt[
                "stack"
            ].update(invoked_number=8),
            "non-default trunk": lambda state, attempt: attempt["stack"].update(
                trunk="release"
            ),
            "non-default pull request base": lambda state, attempt: state[
                "pr"
            ].update(base_branch="release"),
            "open predecessor": lambda state, attempt: state["relations"].update(
                stacked_on={"number": 4}
            ),
            "dependent": lambda state, attempt: state["relations"].update(
                dependents=[{"number": 8}]
            ),
            "external dependent": lambda state, attempt: state.update(
                external_dependents=[{"number": 9}]
            ),
            "push blocker": lambda state, attempt: state.update(
                push_blockers=["fork"]
            ),
            "rebase-only decision": lambda state, attempt: attempt[
                "fallback_strategy"
            ].update(strategy="rebase"),
        }
        error = MODULE.MergedPredecessorLineageError("unrecoverable lineage")

        for name, mutate in cases.items():
            with self.subTest(name=name):
                state, attempt = valid()
                mutate(state, attempt)
                self.assertFalse(
                    MODULE.configure_singleton_merge_fallback(
                        state, attempt, error
                    )
                )
                self.assertEqual("stack", attempt["strategy"])


class StackFormatCommandTest(GitTestCase):
    def setUp(self):
        self.directory = temporary_directory(self)
        self.workspace = self.directory / "workspace"
        self.workspace.mkdir()
        self.git_in(self.workspace, "init", "--initial-branch", "main")
        self.git_in(self.workspace, "config", "user.name", "Stack Test")
        self.git_in(self.workspace, "config", "user.email", "stack@example.invalid")
        self.write_in(self.workspace, "main.txt", "main\n")
        self.commit_in(self.workspace, "main")
        self.main_sha = self.git_in(self.workspace, "rev-parse", "HEAD")
        self.git_in(self.workspace, "checkout", "-b", "feature")
        self.write_in(self.workspace, "feature.txt", "before\n")
        self.commit_in(self.workspace, "feature")
        self.feature_sha = self.git_in(self.workspace, "rev-parse", "HEAD")
        self.stack = {
            "number": 77,
            "size": 1,
            "trunk": "main",
            "members": [
                {
                    "number": 7,
                    "head_branch": "feature",
                    "base_branch": "main",
                    "head_sha": self.feature_sha,
                    "base_sha": self.main_sha,
                }
            ],
            "workspace": str(self.workspace),
            "plan": [
                {
                    "index": 0,
                    "number": 7,
                    "branch": "feature",
                    "branch_ref": "refs/heads/feature",
                    "head_sha": self.feature_sha,
                    "new_base_ref": "refs/heads/main",
                    "old_base": self.main_sha,
                }
            ],
            "formatting_index": 0,
            "formatting_checkpoints": [],
        }
        attempt = stack_attempt_record(
            status="formatting",
            stack=self.stack,
        )
        self.state_path = write_state(
            self.directory,
            repo_root=str(self.workspace),
            attempt=attempt,
        )

    def run_format(self, *format_arguments):
        args = SimpleNamespace(
            state=str(self.state_path),
            format_command=list(format_arguments) if format_arguments else None,
            no_format=not format_arguments,
        )
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "run_stack_cascade", return_value=completed(0)
        ) as cascade, mock.patch.object(
            MODULE,
            "validate_rebased_stack",
            return_value=[
                {
                    "number": 7,
                    "head_branch": "feature",
                    "head_sha": self.git_in(
                        self.workspace, "rev-parse", "refs/heads/feature"
                    ),
                }
            ],
        ), mock.patch.object(MODULE, "emit") as emit:
            MODULE.command_stack_format(args)
        return cascade, emitted(emit)

    def test_formatter_changes_are_committed_to_the_current_member(self):
        _cascade, payload = self.run_format(
            sys.executable,
            "-c",
            "from pathlib import Path; Path('feature.txt').write_text('after\\n')",
        )
        self.assertEqual("resolved", payload["result"])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        checkpoint = state["attempt"]["stack"]["formatting_checkpoints"][0]
        self.assertTrue(checkpoint["changed"])
        self.assertEqual(["feature.txt"], checkpoint["paths"])
        self.assertEqual("after\n", (self.workspace / "feature.txt").read_text())
        self.assertEqual(
            checkpoint["after_sha"],
            self.git_in(self.workspace, "rev-parse", "refs/heads/feature"),
        )

    def test_a_successful_formatter_with_no_diff_advances_without_a_commit(self):
        original = self.git_in(self.workspace, "rev-parse", "refs/heads/feature")
        _cascade, payload = self.run_format(
            sys.executable,
            "-c",
            "from pathlib import Path; Path('feature.txt').read_text()",
        )
        self.assertEqual("resolved", payload["result"])
        checkpoint = json.loads(
            self.state_path.read_text(encoding="utf-8")
        )["attempt"]["stack"]["formatting_checkpoints"][0]
        self.assertFalse(checkpoint["changed"])
        self.assertEqual(original, checkpoint["after_sha"])

    def test_a_detached_rebase_result_attaches_the_member_before_formatting(self):
        self.git_in(self.workspace, "checkout", "--detach", self.feature_sha)

        _cascade, payload = self.run_format(sys.executable, "-c", "pass")

        self.assertEqual("resolved", payload["result"])
        self.assertEqual(
            "feature", self.git_in(self.workspace, "branch", "--show-current")
        )
        self.assertEqual(
            self.feature_sha, self.git_in(self.workspace, "rev-parse", "HEAD")
        )

    def test_formatter_failure_restores_a_detached_rebase_result(self):
        self.git_in(self.workspace, "checkout", "--detach", self.feature_sha)
        args = SimpleNamespace(
            state=str(self.state_path),
            format_command=[
                sys.executable,
                "-c",
                "from pathlib import Path; Path('feature.txt').write_text('partial\\n'); raise SystemExit(9)",
            ],
            no_format=False,
        )

        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError, "formatter failed"
        ):
            MODULE.command_stack_format(args)

        self.assertEqual("", self.git_in(self.workspace, "branch", "--show-current"))
        self.assertEqual(
            self.feature_sha, self.git_in(self.workspace, "rev-parse", "HEAD")
        )
        self.assertEqual(
            self.feature_sha,
            self.git_in(self.workspace, "rev-parse", "refs/heads/feature"),
        )
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))

    def test_a_detached_unrelated_commit_is_not_adopted_for_formatting(self):
        self.git_in(self.workspace, "checkout", "--detach", self.main_sha)
        args = SimpleNamespace(
            state=str(self.state_path),
            format_command=[sys.executable, "-c", "pass"],
            no_format=False,
        )

        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError, "not at the recorded PR layer"
        ):
            MODULE.command_stack_format(args)

        self.assertEqual("", self.git_in(self.workspace, "branch", "--show-current"))
        self.assertEqual(
            self.main_sha, self.git_in(self.workspace, "rev-parse", "HEAD")
        )
        self.assertEqual(
            self.feature_sha,
            self.git_in(self.workspace, "rev-parse", "refs/heads/feature"),
        )

    def test_formatter_changes_outside_the_current_member_are_refused(self):
        args = SimpleNamespace(
            state=str(self.state_path),
            format_command=[
                sys.executable,
                "-c",
                "from pathlib import Path; Path('main.txt').write_text('bad\\n')",
            ],
            no_format=False,
        )
        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError, "outside the current PR layer"
        ):
            MODULE.command_stack_format(args)
        self.assertEqual(
            self.feature_sha,
            self.git_in(self.workspace, "rev-parse", "refs/heads/feature"),
        )
        self.assertEqual(
            "main\n", (self.workspace / "main.txt").read_text(encoding="utf-8")
        )
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))

    def test_a_failed_formatter_restores_the_workspace_and_can_be_retried(self):
        args = SimpleNamespace(
            state=str(self.state_path),
            format_command=[
                sys.executable,
                "-c",
                "from pathlib import Path; Path('feature.txt').write_text('partial\\n'); raise SystemExit(9)",
            ],
            no_format=False,
        )
        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError, "formatter failed"
        ):
            MODULE.command_stack_format(args)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual("formatting", state["attempt"]["status"])
        self.assertEqual(
            "before\n",
            (self.workspace / "feature.txt").read_text(encoding="utf-8"),
        )
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))
        _cascade, payload = self.run_format(sys.executable, "-c", "pass")
        self.assertEqual("resolved", payload["result"])

    def test_formatter_created_files_are_removed_after_rejection(self):
        args = SimpleNamespace(
            state=str(self.state_path),
            format_command=[
                sys.executable,
                "-c",
                "from pathlib import Path; Path('generated.txt').write_text('bad\\n')",
            ],
            no_format=False,
        )
        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError, "outside the current PR layer"
        ):
            MODULE.command_stack_format(args)
        self.assertFalse((self.workspace / "generated.txt").exists())
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))

    def test_formatter_cleanup_batches_large_path_sets(self):
        paths = [f"generated/{index:04d}-{'x' * 60}.txt" for index in range(1_000)]
        with mock.patch.object(
            MODULE, "git_try", return_value=completed(0)
        ) as git_try:
            MODULE.clean_formatter_paths(self.workspace, paths, ignored=False)
        self.assertGreater(git_try.call_count, 1)
        cleaned = [
            argument
            for call in git_try.call_args_list
            for argument in call.args[1:]
            if argument.startswith(":(literal)")
        ]
        self.assertEqual(
            {MODULE.literal_pathspec(path) for path in paths}, set(cleaned)
        )
        for call in git_try.call_args_list:
            self.assertLessEqual(
                sum(len(str(argument)) + 1 for argument in call.args[1:]),
                8_000,
            )

    def test_formatter_created_ignored_files_are_removed_after_failure(self):
        self.write_in(self.workspace, ".gitignore", "ignored/\n")
        self.commit_in(self.workspace, "ignore formatter output")
        self.feature_sha = self.git_in(self.workspace, "rev-parse", "HEAD")
        self.stack["members"][0]["head_sha"] = self.feature_sha
        self.stack["plan"][0]["head_sha"] = self.feature_sha
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["attempt"]["stack"] = self.stack
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        args = SimpleNamespace(
            state=str(self.state_path),
            format_command=[
                sys.executable,
                "-c",
                "from pathlib import Path; import subprocess; Path('ignored/tool-cache').mkdir(parents=True); subprocess.run(['git', 'init', 'ignored/tool-cache'], check=True, stdout=subprocess.DEVNULL); raise SystemExit(9)",
            ],
            no_format=False,
        )
        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError, "formatter failed"
        ):
            MODULE.command_stack_format(args)
        self.assertFalse((self.workspace / "ignored" / "tool-cache").exists())
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))

    def test_formatter_failure_restores_the_entry_branch(self):
        self.git_in(self.workspace, "checkout", "main")
        args = SimpleNamespace(
            state=str(self.state_path),
            format_command=[
                sys.executable,
                "-c",
                "from pathlib import Path; Path('feature.txt').write_text('partial\\n'); raise SystemExit(9)",
            ],
            no_format=False,
        )
        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError, "formatter failed"
        ):
            MODULE.command_stack_format(args)
        self.assertEqual(
            "main", self.git_in(self.workspace, "branch", "--show-current")
        )
        self.assertEqual(
            self.main_sha, self.git_in(self.workspace, "rev-parse", "HEAD")
        )
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))

    def test_formatter_commit_on_member_branch_is_rolled_back(self):
        self.git_in(self.workspace, "checkout", "main")
        args = SimpleNamespace(
            state=str(self.state_path),
            format_command=[
                sys.executable,
                "-c",
                "from pathlib import Path; import subprocess; Path('feature.txt').write_text('committed\\n'); subprocess.run(['git', 'add', 'feature.txt'], check=True); subprocess.run(['git', 'commit', '-m', 'unexpected formatter commit'], check=True, stdout=subprocess.DEVNULL)",
            ],
            no_format=False,
        )
        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError, "formatter moved"
        ):
            MODULE.command_stack_format(args)
        self.assertEqual(
            self.feature_sha,
            self.git_in(self.workspace, "rev-parse", "refs/heads/feature"),
        )
        self.assertEqual(
            "main", self.git_in(self.workspace, "branch", "--show-current")
        )
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))
        _cascade, payload = self.run_format(sys.executable, "-c", "pass")
        self.assertEqual("resolved", payload["result"])
        self.assertEqual(
            self.feature_sha,
            self.git_in(self.workspace, "rev-parse", "refs/heads/feature"),
        )

    def test_cleaned_blob_matches_ignore_false_windows_dirtiness(self):
        original_git_try = MODULE.git_try

        def false_status_once(workspace, *arguments):
            if arguments == (
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "-z",
            ):
                return completed(0, stdout=" M main.txt\0")
            return original_git_try(workspace, *arguments)

        with mock.patch.object(MODULE, "git_try", side_effect=false_status_once):
            _cascade, payload = self.run_format(sys.executable, "-c", "pass")
        self.assertEqual("resolved", payload["result"])
        checkpoint = json.loads(
            self.state_path.read_text(encoding="utf-8")
        )["attempt"]["stack"]["formatting_checkpoints"][0]
        self.assertFalse(checkpoint["changed"])
        self.assertEqual([], checkpoint["paths"])
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))

    def test_formatter_git_config_changes_are_rejected_and_restored(self):
        self.git_in(self.workspace, "config", "resolver.guard", "original")

        with self.assertRaisesRegex(
            MODULE.WorkflowError, "formatter command changed local Git configuration"
        ):
            self.run_format(
                "git",
                "config",
                "resolver.guard",
                "changed",
            )

        self.assertEqual(
            "original",
            self.git_in(self.workspace, "config", "--get", "resolver.guard"),
        )
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))

    def test_formatter_commit_hook_cannot_move_another_stack_ref(self):
        self.git_in(self.workspace, "branch", "lower", self.main_sha)
        self.stack["plan"].append(
            {
                "index": 1,
                "number": 8,
                "branch": "lower",
                "branch_ref": "refs/heads/lower",
                "head_sha": self.main_sha,
                "new_base_ref": "refs/heads/feature",
                "old_base": self.main_sha,
            }
        )
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["attempt"]["stack"]["plan"] = self.stack["plan"]
        MODULE.save_state(self.state_path, state)
        hooks = self.directory / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text(
            "#!/bin/sh\n"
            "git update-ref refs/heads/lower refs/heads/feature\n",
            encoding="utf-8",
            newline="\n",
        )
        hook.chmod(0o755)
        self.git_in(self.workspace, "config", "core.hooksPath", hooks.as_posix())

        with self.assertRaisesRegex(
            MODULE.WorkflowError,
            "formatting commit moved stack ref 'refs/heads/lower'",
        ):
            self.run_format(
                sys.executable,
                "-c",
                "from pathlib import Path; Path('feature.txt').write_text('after\\n')",
            )

        self.assertEqual(
            self.main_sha,
            self.git_in(self.workspace, "rev-parse", "refs/heads/lower"),
        )
        self.assertEqual(
            self.feature_sha,
            self.git_in(self.workspace, "rev-parse", "refs/heads/feature"),
        )
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))


class StackValidationFixCommandTest(GitTestCase):
    def setUp(self):
        self.directory = temporary_directory(self)
        self.workspace = self.directory / "workspace"
        self.workspace.mkdir()
        self.git_in(self.workspace, "init", "--initial-branch", "main")
        self.git_in(self.workspace, "config", "user.name", "Stack Test")
        self.git_in(self.workspace, "config", "user.email", "stack@example.invalid")
        self.write_in(self.workspace, "base-caller.txt", "old expectation\n")
        self.commit_in(self.workspace, "main")
        self.main_sha = self.git_in(self.workspace, "rev-parse", "HEAD")
        self.git_in(
            self.workspace,
            "update-ref",
            "refs/remotes/origin/main",
            self.main_sha,
        )
        self.git_in(self.workspace, "checkout", "-b", "feature")
        self.write_in(self.workspace, "feature.txt", "resolved behavior\n")
        self.commit_in(self.workspace, "feature")
        self.feature_sha = self.git_in(self.workspace, "rev-parse", "HEAD")
        self.stack = {
            "number": 77,
            "size": 1,
            "trunk": "main",
            "invoked_number": 7,
            "members": [
                {
                    "number": 7,
                    "head_branch": "feature",
                    "base_branch": "main",
                    "head_sha": self.feature_sha,
                    "base_sha": self.main_sha,
                }
            ],
            "workspace": str(self.workspace),
            "plan": [
                {
                    "index": 0,
                    "number": 7,
                    "branch": "feature",
                    "branch_ref": "refs/heads/feature",
                    "head_sha": self.feature_sha,
                    "new_base_ref": "refs/remotes/origin/main",
                    "old_base": self.main_sha,
                }
            ],
            "members_after": [
                {
                    "number": 7,
                    "head_branch": "feature",
                    "head_sha": self.feature_sha,
                }
            ],
        }
        self.state_path = write_state(
            self.directory,
            repo_root=str(self.workspace),
            attempt=stack_attempt_record(status="resolved", stack=self.stack),
        )

    def run_fix(self, command, *, paths=None, rationale="Tests follow the contract."):
        args = SimpleNamespace(
            state=str(self.state_path),
            paths=paths or ["base-caller.txt"],
            rationale=rationale,
            rationale_file=None,
            fix_command=command,
        )
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "emit"
        ) as emit:
            MODULE.command_stack_validation_fix(args)
        return emitted(emit)

    def saved(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def use_descendant_propagation_state(self):
        source_stack = {
            "number": self.stack["number"],
            "size": self.stack["size"],
            "trunk": self.stack["trunk"],
            "invoked_number": self.stack["invoked_number"],
            "members": self.stack["members"],
        }
        self.state_path = write_state(
            self.directory,
            operation="descendant_propagation",
            status="resolved",
            workspace=str(self.workspace),
            stack_number=self.stack["number"],
            fixed_pr=6,
            fixed_head_sha=self.main_sha,
            source_stack=source_stack,
            source_snapshot=MODULE.stack_snapshot_fingerprint(source_stack),
            cascade=self.stack,
            members_after=self.stack["members_after"],
            expected_post_fingerprint=MODULE.propagated_stack_fingerprint(
                source_stack, self.stack["members_after"]
            ),
            attempt=None,
        )

    def test_records_an_exact_base_side_validation_fix_on_the_final_member(self):
        payload = self.run_fix(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('base-caller.txt').write_text('new expectation\\n')"
                ),
            ]
        )

        self.assertEqual("validation_fix_recorded", payload["result"])
        checkpoint = self.saved()["attempt"]["stack"][
            "validation_fix_checkpoints"
        ][0]
        self.assertEqual(["base-caller.txt"], checkpoint["paths"])
        self.assertEqual("Tests follow the contract.", checkpoint["rationale"])
        self.assertNotEqual(self.feature_sha, checkpoint["after_sha"])
        self.assertEqual(
            checkpoint["after_sha"],
            self.saved()["attempt"]["stack"]["members_after"][-1]["head_sha"],
        )
        self.assertEqual(
            "new expectation\n",
            (self.workspace / "base-caller.txt").read_text(encoding="utf-8"),
        )
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))

    def test_records_a_validation_fix_for_a_resolved_descendant_propagation(self):
        self.use_descendant_propagation_state()

        payload = self.run_fix(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('base-caller.txt').write_text('new expectation\\n')"
                ),
            ]
        )

        self.assertEqual("validation_fix_recorded", payload["result"])
        self.assertEqual("validate_then_descendant-propagate", payload["next"])
        state = self.saved()
        checkpoint = state["cascade"]["validation_fix_checkpoints"][0]
        self.assertEqual(checkpoint["after_sha"], state["members_after"][-1]["head_sha"])
        self.assertEqual(
            checkpoint["after_sha"],
            state["cascade"]["members_after"][-1]["head_sha"],
        )
        self.assertEqual(
            MODULE.propagated_stack_fingerprint(
                state["source_stack"], state["members_after"]
            ),
            state["expected_post_fingerprint"],
        )
        self.assertEqual(
            checkpoint["after_sha"],
            self.git_in(self.workspace, "rev-parse", "refs/heads/feature"),
        )

    def test_reuses_a_descendant_validation_fix_after_output_failure(self):
        self.use_descendant_propagation_state()
        command = [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "path = Path('base-caller.txt'); "
                "path.write_text(path.read_text() + 'new expectation\\n')"
            ),
        ]
        args = SimpleNamespace(
            state=str(self.state_path),
            paths=["base-caller.txt"],
            rationale="Tests follow the contract.",
            rationale_file=None,
            fix_command=command,
        )

        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "emit", side_effect=BrokenPipeError
        ), self.assertRaises(BrokenPipeError):
            MODULE.command_stack_validation_fix(args)

        first = self.saved()["cascade"]["validation_fix_checkpoints"][0]
        payload = self.run_fix(command)

        self.assertEqual(first["after_sha"], payload["checkpoint"]["after_sha"])
        self.assertEqual("validate_then_descendant-propagate", payload["next"])
        self.assertEqual(
            1,
            len(self.saved()["cascade"]["validation_fix_checkpoints"]),
        )
        self.assertEqual(
            "old expectation\nnew expectation\n",
            (self.workspace / "base-caller.txt").read_text(encoding="utf-8"),
        )

    def test_multiple_validation_fixes_form_a_linear_final_member_history(self):
        first = self.run_fix(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('base-caller.txt').write_text('new expectation\\n')"
                ),
            ]
        )["checkpoint"]
        second = self.run_fix(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('base-caller.txt').write_text('final expectation\\n')"
                ),
            ],
            rationale="A second validation exposed the final expectation.",
        )["checkpoint"]

        self.assertEqual(first["after_sha"], second["before_sha"])
        self.assertTrue(
            MODULE.is_ancestor(
                self.workspace, self.feature_sha, second["after_sha"]
            )
        )
        self.assertEqual(
            2,
            len(
                self.saved()["attempt"]["stack"]["validation_fix_checkpoints"]
            ),
        )

    def test_undeclared_changes_restore_the_workspace_and_stack_ref(self):
        args = SimpleNamespace(
            state=str(self.state_path),
            paths=["base-caller.txt"],
            rationale="Only the base caller should change.",
            rationale_file=None,
            fix_command=[
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('base-caller.txt').write_text('new expectation\\n'); "
                    "Path('feature.txt').write_text('undeclared\\n')"
                ),
            ],
        )

        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError, "undeclared changes"
        ):
            MODULE.command_stack_validation_fix(args)

        self.assertEqual(
            self.feature_sha,
            self.git_in(self.workspace, "rev-parse", "refs/heads/feature"),
        )
        self.assertEqual(
            "old expectation\n",
            (self.workspace / "base-caller.txt").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "resolved behavior\n",
            (self.workspace / "feature.txt").read_text(encoding="utf-8"),
        )
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))

    def test_failed_fix_command_restores_partial_changes(self):
        args = SimpleNamespace(
            state=str(self.state_path),
            paths=["base-caller.txt"],
            rationale="Tests follow the contract.",
            rationale_file=None,
            fix_command=[
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('base-caller.txt').write_text('partial\\n'); "
                    "raise SystemExit(9)"
                ),
            ],
        )

        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError, "validation fix command failed"
        ):
            MODULE.command_stack_validation_fix(args)

        self.assertEqual(
            "old expectation\n",
            (self.workspace / "base-caller.txt").read_text(encoding="utf-8"),
        )
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))

    def test_new_files_are_rejected_and_removed(self):
        args = SimpleNamespace(
            state=str(self.state_path),
            paths=["base-caller.txt"],
            rationale="Tests follow the contract.",
            rationale_file=None,
            fix_command=[
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('base-caller.txt').write_text('new expectation\\n'); "
                    "Path('generated.txt').write_text('unexpected\\n')"
                ),
            ],
        )

        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError, "undeclared changes"
        ):
            MODULE.command_stack_validation_fix(args)

        self.assertFalse((self.workspace / "generated.txt").exists())
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))

    def test_declared_untracked_paths_are_rejected_before_the_command_runs(self):
        marker = self.directory / "ran"
        args = SimpleNamespace(
            state=str(self.state_path),
            paths=["new-caller.txt"],
            rationale="Tests follow the contract.",
            rationale_file=None,
            fix_command=[
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
            ],
        )

        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError, "must be a tracked file"
        ):
            MODULE.command_stack_validation_fix(args)

        self.assertFalse(marker.exists())

    def test_paths_are_checked_after_attaching_the_final_member(self):
        self.git_in(self.workspace, "rm", "base-caller.txt")
        self.commit_in(self.workspace, "delete base caller")
        deleted_tip = self.git_in(self.workspace, "rev-parse", "HEAD")
        self.stack["members"][0]["head_sha"] = deleted_tip
        self.stack["plan"][0]["head_sha"] = deleted_tip
        self.stack["members_after"][0]["head_sha"] = deleted_tip
        self.state_path = write_state(
            self.directory,
            repo_root=str(self.workspace),
            attempt=stack_attempt_record(status="resolved", stack=self.stack),
        )
        self.git_in(self.workspace, "checkout", "main")
        marker = self.directory / "ran"

        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError, "tracked file at the final stack tip"
        ):
            MODULE.command_stack_validation_fix(
                SimpleNamespace(
                    state=str(self.state_path),
                    paths=["base-caller.txt"],
                    rationale="Tests follow the contract.",
                    rationale_file=None,
                    fix_command=[
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            f"Path({str(marker)!r}).touch(); "
                            "Path('base-caller.txt').write_text('re-added\\n')"
                        ),
                    ],
                )
            )

        self.assertFalse(marker.exists())
        self.assertEqual("main", self.git_in(self.workspace, "branch", "--show-current"))
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))

    def test_only_resolved_stack_attempts_can_record_validation_fixes(self):
        state = self.saved()
        state["attempt"]["status"] = "formatting"
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError, "only after a stack cascade is resolved"
        ):
            MODULE.command_stack_validation_fix(
                SimpleNamespace(
                    state=str(self.state_path),
                    paths=["base-caller.txt"],
                    rationale="Tests follow the contract.",
                    rationale_file=None,
                    fix_command=[sys.executable, "-c", "pass"],
                )
            )

    def test_detached_final_member_is_attached_before_recording_the_fix(self):
        self.git_in(self.workspace, "checkout", "--detach", self.feature_sha)

        payload = self.run_fix(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('base-caller.txt').write_text('new expectation\\n')"
                ),
            ]
        )

        self.assertEqual("validation_fix_recorded", payload["result"])
        self.assertEqual(
            "feature", self.git_in(self.workspace, "branch", "--show-current")
        )

    def test_moving_a_lower_stack_ref_rolls_back_every_member(self):
        self.git_in(self.workspace, "branch", "lower", self.main_sha)
        self.stack["members"].insert(
            0,
            {
                "number": 6,
                "head_branch": "lower",
                "base_branch": "main",
                "head_sha": self.main_sha,
                "base_sha": self.main_sha,
            },
        )
        self.stack["plan"].insert(
            0,
            {
                "index": 0,
                "number": 6,
                "branch": "lower",
                "branch_ref": "refs/heads/lower",
                "head_sha": self.main_sha,
                "new_base_ref": "refs/remotes/origin/main",
                "old_base": self.main_sha,
            },
        )
        self.stack["plan"][1]["index"] = 1
        self.stack["members_after"].insert(
            0,
            {
                "number": 6,
                "head_branch": "lower",
                "head_sha": self.main_sha,
            },
        )
        self.state_path = write_state(
            self.directory,
            repo_root=str(self.workspace),
            attempt=stack_attempt_record(status="resolved", stack=self.stack),
        )
        args = SimpleNamespace(
            state=str(self.state_path),
            paths=["base-caller.txt"],
            rationale="Tests follow the contract.",
            rationale_file=None,
            fix_command=[
                sys.executable,
                "-c",
                (
                    "import subprocess; "
                    "tree = subprocess.check_output(['git', 'rev-parse', "
                    "'refs/heads/lower^{tree}'], text=True).strip(); "
                    "moved = subprocess.check_output(['git', 'commit-tree', tree, "
                    "'-p', 'refs/heads/lower', '-m', 'unexpected'], "
                    "text=True).strip(); "
                    "subprocess.run(['git', 'update-ref', 'refs/heads/lower', "
                    "moved], check=True)"
                ),
            ],
        )

        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError, "moved stack ref 'refs/heads/lower'"
        ):
            MODULE.command_stack_validation_fix(args)

        self.assertEqual(
            self.main_sha,
            self.git_in(self.workspace, "rev-parse", "refs/heads/lower"),
        )
        self.assertEqual(
            self.feature_sha,
            self.git_in(self.workspace, "rev-parse", "refs/heads/feature"),
        )
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))

    def test_git_config_changes_are_rejected_and_restored(self):
        self.git_in(self.workspace, "config", "resolver.guard", "original")
        args = SimpleNamespace(
            state=str(self.state_path),
            paths=["base-caller.txt"],
            rationale="Tests follow the contract.",
            rationale_file=None,
            fix_command=[
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import subprocess; "
                    "Path('base-caller.txt').write_text('new expectation\\n'); "
                    "subprocess.run(['git', 'config', 'resolver.guard', 'changed'], "
                    "check=True)"
                ),
            ],
        )

        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError,
            "validation fix command changed local Git configuration",
        ):
            MODULE.command_stack_validation_fix(args)

        self.assertEqual(
            "original",
            self.git_in(self.workspace, "config", "--get", "resolver.guard"),
        )
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))

    def test_empty_inline_rationale_is_rejected(self):
        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError, "rationale must not be empty"
        ):
            MODULE.command_stack_validation_fix(
                SimpleNamespace(
                    state=str(self.state_path),
                    paths=["base-caller.txt"],
                    rationale="  ",
                    rationale_file=None,
                    fix_command=[sys.executable, "-c", "pass"],
                )
            )

    def test_output_failure_keeps_the_durable_validation_fix_commit(self):
        args = SimpleNamespace(
            state=str(self.state_path),
            paths=["base-caller.txt"],
            rationale="Tests follow the contract.",
            rationale_file=None,
            fix_command=[
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "path = Path('base-caller.txt'); "
                    "path.write_text(path.read_text() + 'new expectation\\n')"
                ),
            ],
        )

        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "emit", side_effect=BrokenPipeError
        ), self.assertRaises(BrokenPipeError):
            MODULE.command_stack_validation_fix(args)

        state = self.saved()
        checkpoint = state["attempt"]["stack"]["validation_fix_checkpoints"][0]
        self.assertEqual(
            checkpoint["after_sha"],
            self.git_in(self.workspace, "rev-parse", "refs/heads/feature"),
        )
        self.assertEqual(
            checkpoint["after_sha"],
            state["attempt"]["stack"]["members_after"][-1]["head_sha"],
        )
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "emit"
        ) as emit:
            MODULE.command_stack_validation_fix(args)
        self.assertEqual(
            checkpoint["after_sha"],
            emitted(emit)["checkpoint"]["after_sha"],
        )
        self.assertEqual(
            1,
            len(
                self.saved()["attempt"]["stack"]["validation_fix_checkpoints"]
            ),
        )
        self.assertEqual(
            "old expectation\nnew expectation\n",
            (self.workspace / "base-caller.txt").read_text(encoding="utf-8"),
        )

    def test_an_unexpected_commit_is_rolled_back(self):
        args = SimpleNamespace(
            state=str(self.state_path),
            paths=["base-caller.txt"],
            rationale="Tests follow the contract.",
            rationale_file=None,
            fix_command=[
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import subprocess; "
                    "Path('base-caller.txt').write_text('committed\\n'); "
                    "subprocess.run(['git', 'add', 'base-caller.txt'], check=True); "
                    "subprocess.run(['git', 'commit', '-m', 'unexpected'], "
                    "check=True, stdout=subprocess.DEVNULL)"
                ),
            ],
        )

        with mock.patch.object(MODULE, "require_tools"), self.assertRaisesRegex(
            MODULE.WorkflowError, "created a commit"
        ):
            MODULE.command_stack_validation_fix(args)

        self.assertEqual(
            self.feature_sha,
            self.git_in(self.workspace, "rev-parse", "refs/heads/feature"),
        )
        self.assertEqual("", self.git_in(self.workspace, "status", "--short"))

    def test_parser_preserves_validation_fix_command_arguments(self):
        args = MODULE.build_parser().parse_args(
            [
                "stack-validation-fix",
                "--state",
                "state.json",
                "--paths",
                "caller.go",
                "caller.rb",
                "--rationale",
                "Callers must follow the resolved contract.",
                "--fix-command",
                "--",
                "python",
                "fix.py",
                "--strict",
            ]
        )

        self.assertIs(args.function, MODULE.command_stack_validation_fix)
        self.assertEqual(
            ["python", "fix.py", "--strict"],
            args.fix_command,
        )


class StackPublishCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory(self)
        self.workspace = temporary_directory(self)
        self.intended = [
            {"number": 19483, "head_branch": "v143", "head_sha": "newa"},
            {"number": 7, "head_branch": "feature", "head_sha": "newf"},
        ]

    def publish(self, *, push=None, landed=None, members_after="default",
                final=None, dissociate=None, trunk="base1", status="resolved"):
        if members_after == "default":
            members_after = self.intended
        attempt = stack_attempt_record(
            status=status,
            published_head_sha="newf" if status == "published_refs" else None,
            stack={
                "workspace": str(self.workspace),
                "members_after": members_after,
                "trunk_sha": "base1",
            },
        )
        state_path = write_state(self.directory, attempt=attempt)
        if landed is None:
            landed = {member["head_branch"]: member["head_sha"]
                      for member in (members_after or [])}

        def fake_wait(owner, repo, branch, expected):
            return landed.get(branch)

        args = SimpleNamespace(state=str(state_path))
        projected_path = self.directory / "19483.json"
        if callable(final):
            final_side_effect = final
            final_return = None
        elif isinstance(final, Exception):
            final_side_effect = final
            final_return = None
        else:
            final_side_effect = None
            final_return = (
                final
                if final is not None
                else pr_metadata(head_sha="newf", mergeable="MERGEABLE")
            )
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "validate_rebased_stack", return_value=members_after or []
        ), mock.patch.object(
            MODULE, "remote_head", return_value=trunk
        ), mock.patch.object(
            MODULE, "require_current_stack_snapshot"
        ), mock.patch.object(
            MODULE, "atomic_stack_push", return_value=push or completed(0)
        ) as atomic, mock.patch.object(
            MODULE, "wait_for_remote_head", side_effect=fake_wait
        ), mock.patch.object(
            MODULE, "dissociate_workspace", return_value=dissociate
        ) as dissociate_mock, mock.patch.object(
            MODULE, "remove_stack_workspace"
        ) as remove, mock.patch.object(
            MODULE, "live_mergeability",
            side_effect=final_side_effect,
            return_value=final_return,
        ), mock.patch.object(
            MODULE,
            "default_state_path",
            side_effect=lambda target: projected_path,
        ), mock.patch.object(
            MODULE, "emit"
        ) as emit:
            self.error = None
            try:
                MODULE.command_stack_publish(args)
            except MODULE.WorkflowError as error:
                self.error = error
        self.state_path = state_path
        self.remove = remove
        self.dissociate = dissociate_mock
        self.atomic = atomic
        self.projected_path = projected_path
        return emitted(emit) if emit.called else None

    def saved(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_every_member_landing_on_its_intended_sha_publishes(self):
        payload = self.publish()
        self.assertEqual("published", payload["result"])
        self.assertEqual("newf", payload["invoked_head_sha"])
        self.assertEqual("mergeable", payload["mergeability"])
        self.remove.assert_called_once()
        self.assertEqual("published", self.saved()["attempt"]["status"])
        self.assertNotIn("push_detail", payload)
        self.assertIsNone(self.saved()["attempt"]["stack_push_detail"])

    def test_publishing_records_clearance_for_each_other_stack_member(self):
        def final(target, *, expected_head=None):
            return pr_metadata(
                number=target["number"],
                head_sha=expected_head,
                base_sha="base1",
                mergeable="MERGEABLE",
            )

        self.publish(final=final)

        projected = json.loads(self.projected_path.read_text(encoding="utf-8"))
        self.assertEqual("newa", projected["attempt"]["mergeable_at_head_sha"])
        self.assertEqual("base1", projected["attempt"]["base_sha"])
        self.assertEqual(7, projected["attempt"]["stack_source_pr"])

    def test_publishing_records_a_completed_nonclearing_stack_member(self):
        def final(target, *, expected_head=None):
            return pr_metadata(
                number=target["number"],
                head_sha=expected_head,
                base_sha="base1",
                mergeable="MERGEABLE" if target["number"] == 7 else "CONFLICTING",
            )

        self.publish(final=final)

        projected = json.loads(self.projected_path.read_text(encoding="utf-8"))
        attempt = projected["attempt"]
        self.assertEqual("pr-19483-attempt-1", attempt["id"])
        self.assertEqual("published", attempt["status"])
        self.assertIsNone(attempt["mergeable_at_head_sha"])
        self.assertEqual("completed", MODULE.stage_outcome(projected))

    def test_a_rejected_atomic_push_preserves_the_workspace_and_publishes_nothing(self):
        self.publish(
            push=completed(1, stderr="stale info"),
            landed={"v143": "aaa", "feature": "head1"},
        )
        self.assertIsNotNone(self.error)
        message = str(self.error)
        self.assertIn("did not land the complete intended stack", message)
        self.assertIn("stale info", message)
        self.assertIn(str(self.workspace), message)
        self.remove.assert_not_called()
        self.dissociate.assert_called_once()
        self.assertEqual("resolved", self.saved()["attempt"]["status"])

    def test_a_stale_remote_member_is_named_after_atomic_rejection(self):
        self.publish(
            push=completed(1, stderr="stale info"),
            landed={"v143": "aaa", "feature": "someoneelse"},
        )
        self.assertIsNotNone(self.error)
        message = str(self.error)
        self.assertIn("#7 feature", message)
        self.assertIn("someoneelse", message)
        self.remove.assert_not_called()

    def test_a_moved_trunk_stops_before_the_atomic_push(self):
        self.publish(trunk="newbase")
        self.assertIsNotNone(self.error)
        self.assertIn("trunk", str(self.error))
        self.assertIn("no branch was published", str(self.error))
        self.atomic.assert_not_called()

    def test_transport_failure_after_every_ref_landed_still_finalizes(self):
        payload = self.publish(push=completed(1, stderr="connection reset"))
        self.assertIsNone(self.error)
        self.assertEqual("published", payload["result"])
        self.assertIn("connection reset", payload["push_detail"])
        self.assertIn(
            "connection reset", self.saved()["attempt"]["stack_push_detail"]
        )

    def test_finalization_failure_keeps_a_durable_published_refs_checkpoint(self):
        self.publish(final=MODULE.WorkflowError("api unavailable"))
        self.assertIsNotNone(self.error)
        self.assertIn("api unavailable", str(self.error))
        self.assertEqual("published_refs", self.saved()["attempt"]["status"])
        self.remove.assert_called_once()

    def test_a_published_refs_checkpoint_finalizes_without_pushing_again(self):
        payload = self.publish(status="published_refs")
        self.assertIsNone(self.error)
        self.assertEqual("published", payload["result"])
        self.atomic.assert_not_called()

    def test_publishing_without_recorded_tips_is_refused(self):
        self.publish(members_after=None)
        self.assertIsNotNone(self.error)
        self.assertIn("no rebased member tips", str(self.error))



class StackAbortCommandTest(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory(self)
        self.workspace = temporary_directory(self)

    def abort(self, *, rebase_in_progress=True, status="conflicted"):
        attempt = stack_attempt_record(
            status=status,
            stack={"workspace": str(self.workspace)},
        )
        state_path = write_state(self.directory, attempt=attempt)
        args = SimpleNamespace(state=str(state_path))
        with mock.patch.object(
            MODULE, "rebase_in_progress", return_value=rebase_in_progress
        ), mock.patch.object(
            MODULE, "git_try", return_value=completed(0)
        ) as git_try, mock.patch.object(
            MODULE, "remove_stack_workspace"
        ) as remove, mock.patch.object(
            MODULE, "emit"
        ) as emit:
            self.error = None
            try:
                MODULE.command_stack_abort(args)
            except MODULE.WorkflowError as error:
                self.error = error
        self.git_try = git_try
        self.remove = remove
        self.state_path = state_path
        return emitted(emit) if emit.called else None

    def saved(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_an_in_progress_cascade_is_aborted_and_the_workspace_removed(self):
        payload = self.abort(rebase_in_progress=True)
        self.assertEqual("aborted", payload["result"])
        self.assertEqual("stack-rebase", payload["undone"])
        self.git_try.assert_called_once_with(self.workspace, "rebase", "--abort")
        self.remove.assert_called_once()
        self.assertIsNone(self.saved()["attempt"])

    def test_a_settled_cascade_still_removes_the_workspace(self):
        payload = self.abort(rebase_in_progress=False)
        self.assertEqual("aborted", payload["result"])
        self.assertIsNone(payload["undone"])
        self.remove.assert_called_once()

    def test_refs_that_already_published_cannot_be_aborted(self):
        self.abort(status="published_refs")
        self.assertIsNotNone(self.error)
        self.assertIn("already published", str(self.error))
        self.remove.assert_not_called()

    def test_a_descendant_propagation_conflict_is_aborted_and_workspace_removed(self):
        state_path = write_state(
            self.directory,
            operation="descendant_propagation",
            status="conflicted",
            workspace=str(self.workspace),
            cascade={"workspace": str(self.workspace)},
        )
        self.state_path = state_path
        args = SimpleNamespace(state=str(state_path))
        with mock.patch.object(
            MODULE, "rebase_in_progress", return_value=True
        ), mock.patch.object(
            MODULE, "git_try", return_value=completed(0)
        ) as git_try, mock.patch.object(MODULE, "force_rmtree") as remove, mock.patch.object(
            MODULE, "emit"
        ) as emit:
            MODULE.command_stack_abort(args)

        payload = emitted(emit)
        self.assertEqual("aborted", payload["result"])
        self.assertEqual("stack-rebase", payload["undone"])
        git_try.assert_called_once_with(self.workspace, "rebase", "--abort")
        remove.assert_called_once_with(self.workspace)
        saved = self.saved()
        self.assertEqual("aborted", saved["status"])
        self.assertIsNone(saved["workspace"])
        self.assertIsNone(saved["cascade"]["workspace"])


class DescendantPropagationTest(unittest.TestCase):
    def stack(self):
        return {
            "id": "S_1",
            "number": 77,
            "size": 3,
            "trunk": "main",
            "members": [
                {
                    "position": 0,
                    "number": 10,
                    "head_branch": "lower",
                    "base_branch": "main",
                    "head_sha": "lower-head",
                    "base_sha": "base",
                },
                {
                    "position": 1,
                    "number": 11,
                    "head_branch": "fixed",
                    "base_branch": "lower",
                    "head_sha": "fixed-head",
                    "base_sha": "lower-head",
                },
                {
                    "position": 2,
                    "number": 12,
                    "head_branch": "tip",
                    "base_branch": "fixed",
                    "head_sha": "tip-head",
                    "base_sha": "fixed-head",
                },
            ],
        }

    def test_selects_only_members_strictly_above_the_fixed_pr(self):
        result = MODULE.propagation_stack(self.stack(), 11, "fixed-head")

        self.assertEqual("fixed", result["trunk"])
        self.assertEqual([12], [member["number"] for member in result["members"]])
        self.assertNotIn(10, [member["number"] for member in result["members"]])
        self.assertNotIn(11, [member["number"] for member in result["members"]])

    def test_rejects_a_stale_fixed_head(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "moved from stale"):
            MODULE.propagation_stack(self.stack(), 11, "stale")

    def test_atomic_push_leases_only_descendants(self):
        partial = MODULE.propagation_stack(self.stack(), 11, "fixed-head")
        partial["members_after"] = [
            {"number": 12, "head_branch": "tip", "head_sha": "new-tip"}
        ]
        with mock.patch.object(
            MODULE, "run", return_value=completed(0)
        ) as runner:
            MODULE.atomic_stack_push(Path("/workspace"), partial)

        command = runner.call_args.args[0]
        self.assertIn("--force-with-lease=refs/heads/tip:tip-head", command)
        self.assertNotIn("refs/heads/fixed", " ".join(command))
        self.assertNotIn("refs/heads/lower", " ".join(command))

    def test_post_propagation_fingerprint_changes_only_descendant_heads(self):
        stack = self.stack()
        expected = {
            **stack,
            "members": [
                *stack["members"][:2],
                {**stack["members"][2], "head_sha": "new-tip"},
            ],
        }
        self.assertEqual(
            MODULE.stack_snapshot_fingerprint(expected),
            MODULE.propagated_stack_fingerprint(
                stack,
                [{"number": 12, "head_branch": "tip", "head_sha": "new-tip"}],
            ),
        )

    def test_parser_exposes_machine_facing_operation(self):
        args = MODULE.build_parser().parse_args(
            [
                "descendant-propagate",
                "owner/repo#11",
                "--stack-number",
                "77",
                "--fixed-pr",
                "11",
                "--expected-head",
                "fixed-head",
            ]
        )
        self.assertIs(args.function, MODULE.command_descendant_propagate)

    def test_parser_exposes_the_formatting_checkpoint(self):
        args = MODULE.build_parser().parse_args(
            ["stack-format", "--state", "state.json", "--no-format"]
        )
        self.assertIs(args.function, MODULE.command_stack_format)
        self.assertTrue(args.no_format)

    def test_parser_preserves_formatter_option_arguments(self):
        args = MODULE.build_parser().parse_args(
            [
                "stack-format",
                "--state",
                "state.json",
                "--format-command",
                "--",
                "cargo",
                "fmt",
                "--manifest-path",
                "tools/http/Cargo.toml",
                "--",
                "--check",
            ]
        )
        self.assertEqual(
            [
                "cargo",
                "fmt",
                "--manifest-path",
                "tools/http/Cargo.toml",
                "--",
                "--check",
            ],
            args.format_command,
        )

    def test_descendant_propagation_stops_for_formatting_before_publishing(self):
        stack = self.stack()
        root = temporary_directory(self)
        args = SimpleNamespace(
            target="owner/repo#11",
            repo=None,
            pull_request=None,
            head_sha=None,
            stack_number=77,
            fixed_pr=11,
            expected_head="fixed-head",
            repo_root=None,
            state=str(root / "propagation.json"),
        )
        workspace = root / "workspace"
        workspace.mkdir()

        def prepare(_workspace, partial):
            partial["plan"] = [
                {
                    "index": 0,
                    "number": 12,
                    "branch": "tip",
                    "branch_ref": "refs/heads/tip",
                }
            ]
            partial["formatting_index"] = 0

        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "resolve_repo_root", return_value=root
        ), mock.patch.object(
            MODULE, "metadata_for", return_value=pr_metadata(number=11)
        ), mock.patch.object(
            MODULE,
            "stack_membership",
            return_value={"default_branch": "main", "stack": stack},
        ), mock.patch.object(
            MODULE, "external_stack_dependents", return_value=[]
        ), mock.patch.object(
            MODULE, "create_stack_workspace", return_value=workspace
        ), mock.patch.object(
            MODULE, "prepare_stack_cascade", side_effect=prepare
        ), mock.patch.object(
            MODULE,
            "run_stack_cascade",
            return_value=completed(MODULE.STACK_FORMAT_EXIT),
        ), mock.patch.object(MODULE, "emit") as emit_mock:
            MODULE.command_descendant_propagate(args)

        payload = emitted(emit_mock)
        self.assertEqual("formatting_required", payload["result"])
        saved = MODULE.load_state(Path(args.state))
        self.assertEqual("formatting", saved["status"])
        self.assertTrue(workspace.exists())

    def test_descendant_propagation_preserves_a_conflict_for_resolution(self):
        stack = self.stack()
        root = temporary_directory(self)
        args = SimpleNamespace(
            target="owner/repo#11",
            repo=None,
            pull_request=None,
            head_sha=None,
            stack_number=77,
            fixed_pr=11,
            expected_head="fixed-head",
            repo_root=None,
            state=str(root / "propagation.json"),
        )
        workspace = root / "workspace"
        workspace.mkdir()

        def prepare(_workspace, partial):
            partial["plan"] = [
                {
                    "index": 0,
                    "number": 12,
                    "branch": "tip",
                    "branch_ref": "refs/heads/tip",
                    "head_sha": "tip-head",
                    "old_base": "fixed-head",
                    "new_base_ref": "refs/remotes/origin/fixed",
                }
            ]
            partial["current_index"] = 0

        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "resolve_repo_root", return_value=root
        ), mock.patch.object(
            MODULE, "metadata_for", return_value=pr_metadata(number=11)
        ), mock.patch.object(
            MODULE,
            "stack_membership",
            return_value={"default_branch": "main", "stack": stack},
        ), mock.patch.object(
            MODULE, "external_stack_dependents", return_value=[]
        ), mock.patch.object(
            MODULE, "create_stack_workspace", return_value=workspace
        ), mock.patch.object(
            MODULE, "prepare_stack_cascade", side_effect=prepare
        ), mock.patch.object(
            MODULE,
            "run_stack_cascade",
            return_value=completed(MODULE.STACK_CONFLICT_EXIT, stderr="CONFLICT"),
        ), mock.patch.object(
            MODULE,
            "collect_stack_conflicts",
            return_value=[conflict_record("tip.txt")],
        ), mock.patch.object(MODULE, "emit") as emit_mock:
            MODULE.command_descendant_propagate(args)

        payload = emitted(emit_mock)
        self.assertEqual("conflicted", payload["result"])
        self.assertEqual("resolved", payload["next"])
        saved = MODULE.load_state(Path(args.state))
        self.assertEqual("conflicted", saved["status"])
        self.assertEqual(["tip.txt"], [item["path"] for item in saved["conflicts"]])
        self.assertEqual(str(workspace), saved["workspace"])
        self.assertTrue(workspace.exists())

    def test_continue_resumes_a_resolved_descendant_propagation_conflict(self):
        root = temporary_directory(self)
        workspace = root / "workspace"
        workspace.mkdir()
        cascade = {
            "current_index": 0,
            "formatting_index": 0,
            "plan": [
                {
                    "index": 0,
                    "number": 12,
                    "branch": "tip",
                    "branch_ref": "refs/heads/tip",
                }
            ],
        }
        state_path = write_state(
            root,
            operation="descendant_propagation",
            status="conflicted",
            workspace=str(workspace),
            stack_number=77,
            fixed_pr=11,
            conflicts=[conflict_record("tip.txt", status="resolved")],
            cascade=cascade,
        )

        with mock.patch.object(MODULE, "unmerged_entries", return_value=[]), mock.patch.object(
            MODULE,
            "continue_stack_cascade",
            side_effect=lambda _workspace, stack: (
                stack.update({"formatting_index": 0})
                or completed(MODULE.STACK_FORMAT_EXIT)
            ),
        ), mock.patch.object(MODULE, "emit") as emit_mock:
            MODULE.command_continue(SimpleNamespace(state=str(state_path)))

        payload = emitted(emit_mock)
        self.assertEqual("formatting_required", payload["result"])
        self.assertEqual("stack-format", payload["next"])
        self.assertEqual("formatting", MODULE.load_state(state_path)["status"])

    def test_descendant_format_preserves_a_later_conflict_for_resolution(self):
        root = temporary_directory(self)
        workspace = root / "workspace"
        workspace.mkdir()
        cascade = {
            "formatting_index": 0,
            "plan": [
                {
                    "index": 0,
                    "number": 12,
                    "branch": "tip",
                    "branch_ref": "refs/heads/tip",
                }
            ],
        }
        state_path = write_state(
            root,
            operation="descendant_propagation",
            status="formatting",
            workspace=str(workspace),
            stack_number=77,
            fixed_pr=11,
            cascade=cascade,
        )
        args = SimpleNamespace(
            state=str(state_path),
            format_command=None,
            no_format=True,
        )

        with mock.patch.object(MODULE, "format_stack_member"), mock.patch.object(
            MODULE,
            "run_stack_cascade",
            return_value=completed(MODULE.STACK_CONFLICT_EXIT, stderr="CONFLICT"),
        ), mock.patch.object(
            MODULE,
            "collect_stack_conflicts",
            return_value=[conflict_record("tip.txt")],
        ), mock.patch.object(MODULE, "emit") as emit_mock:
            MODULE.command_descendant_format(args, state_path, MODULE.load_state(state_path))

        payload = emitted(emit_mock)
        self.assertEqual("conflicted", payload["result"])
        self.assertEqual("resolved", payload["next"])
        saved = MODULE.load_state(state_path)
        self.assertEqual("conflicted", saved["status"])
        self.assertEqual(str(workspace), saved["workspace"])
        self.assertTrue(workspace.exists())

    def test_tip_propagation_creates_no_workspace_and_rewrites_nothing(self):
        args = SimpleNamespace(
            target="owner/repo#12",
            stack_number=77,
            fixed_pr=12,
            expected_head="tip-head",
            repo_root=None,
            state=None,
        )
        with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
            MODULE, "resolve_repo_root", return_value=Path("/repo")
        ), mock.patch.object(
            MODULE, "metadata_for", return_value=pr_metadata(number=12)
        ), mock.patch.object(
            MODULE,
            "stack_membership",
            return_value={"default_branch": "main", "stack": self.stack()},
        ), mock.patch.object(
            MODULE, "external_stack_dependents", return_value=[]
        ), mock.patch.object(
            MODULE, "create_stack_workspace"
        ) as create_workspace, mock.patch.object(
            MODULE, "emit"
        ) as emit_mock:
            MODULE.command_descendant_propagate(args)

        self.assertEqual("no_descendants", emitted(emit_mock)["result"])
        create_workspace.assert_not_called()

    def test_resolved_propagation_rejects_a_changed_source_snapshot(self):
        original = self.stack()
        moved = self.stack()
        moved["members"][-1]["head_sha"] = "tip-moved"
        args = SimpleNamespace(
            target="owner/repo#11",
            repo=None,
            pull_request=None,
            head_sha=None,
            stack_number=77,
            fixed_pr=11,
            expected_head="fixed-head",
            repo_root=None,
            state=None,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            state_path = root / "propagation.json"
            args.state = str(state_path)
            MODULE.save_state(
                state_path,
                {
                    "version": MODULE.STATE_VERSION,
                    "operation": "descendant_propagation",
                    "status": "resolved",
                    "stack_number": 77,
                    "fixed_pr": 11,
                    "fixed_head_sha": "fixed-head",
                    "source_snapshot": MODULE.stack_snapshot_fingerprint(original),
                    "members_before": original["members"][2:],
                    "members_after": [
                        {
                            "number": 12,
                            "head_branch": "tip",
                            "head_sha": "rebased-tip",
                        }
                    ],
                    "expected_post_fingerprint": None,
                    "workspace": str(workspace),
                },
            )
            with mock.patch.object(MODULE, "require_tools"), mock.patch.object(
                MODULE, "resolve_repo_root", return_value=root
            ), mock.patch.object(
                MODULE, "metadata_for", return_value=pr_metadata(number=11)
            ), mock.patch.object(
                MODULE,
                "stack_membership",
                return_value={"default_branch": "main", "stack": moved},
            ), mock.patch.object(
                MODULE, "external_stack_dependents", return_value=[]
            ), mock.patch.object(
                MODULE, "create_stack_workspace"
            ) as create_workspace:
                with self.assertRaisesRegex(
                    MODULE.WorkflowError, "earlier descendant propagation"
                ):
                    MODULE.command_descendant_propagate(args)
            create_workspace.assert_not_called()
