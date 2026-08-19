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


SCRIPT = Path(__file__).parents[1] / "scripts" / "conflict_fix_loop.py"
AGENT = Path(__file__).parents[1] / "agents" / "conflict-fix-loop.agent.md"
SPEC = importlib.util.spec_from_file_location("conflict_fix_loop", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


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


def temporary_directory(test: unittest.TestCase) -> Path:
    """Make a temporary directory that survives read-only files on cleanup."""

    directory = Path(tempfile.mkdtemp()).resolve()

    def force_remove(function, path, _info):
        os.chmod(path, stat.S_IWRITE)
        function(path)

    test.addCleanup(shutil.rmtree, directory, ignore_errors=False, onerror=force_remove)
    return directory


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
        "id": "pr-7-iteration-1",
        "status": "conflicted",
        "iteration": 1,
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
        "iterations": 0,
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

    def test_declares_the_frontmatter_keys_the_sibling_loops_use(self):
        self.assertIn("name: Conflict Fix Loop", self.instructions)
        self.assertIn(
            'description: "Use when selected with only a PR URL, PR number, '
            'or owner/repo#number',
            self.instructions,
        )
        self.assertIn(
            "argument-hint: \"PR URL, PR number, or owner/repo#number; omit to use "
            "the current branch's PR\"",
            self.instructions,
        )
        self.assertIn(
            "tools: [read, edit, search, execute, todo, rename_session]",
            self.instructions,
        )
        self.assertIn("user-invocable: true", self.instructions)
        self.assertIn("disable-model-invocation: true", self.instructions)

    def test_does_not_invent_a_model_frontmatter_key(self):
        frontmatter = self.instructions.split("---")[1]
        self.assertNotIn("model:", frontmatter)

    def test_bare_pr_reference_runs_the_full_loop(self):
        self.assertIn(
            "## Activation: Bare PR References Run The Full Loop", self.instructions
        )
        self.assertIn(
            "asks you to run the full Conflict Fix Loop", self.instructions
        )
        self.assertIn("Start the helper's `preflight` workflow at once", self.instructions)
        self.assertIn(
            "Never hand the work to a generic rebase or merge skill", self.instructions
        )

    def test_names_the_session_from_preflight_metadata_idempotently(self):
        self.assertIn("## Session Naming", self.instructions)
        self.assertIn(
            "ensure the session name is `Conflict Fix Loop: <PR number> - <PR title>`",
            self.instructions,
        )
        self.assertIn(
            "If the harness has already supplied a name beginning "
            "`Conflict Fix Loop: <PR number> - `",
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

    def test_states_the_iteration_cap_and_treats_it_as_an_escalation(self):
        self.assertIn("The maximum is 5 iterations", self.instructions)
        self.assertIn(
            "Hitting the cap is an escalation, not a normal completion",
            self.instructions,
        )
        self.assertIn("--max-iterations 5", self.instructions)

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

    def test_documents_every_preflight_result(self):
        for result in (
            "ready",
            "mergeable",
            "unknown_mergeability",
            "max_iterations_reached",
            "unsafe_push",
            "no_safe_strategy",
        ):
            with self.subTest(result=result):
                self.assertIn(f"`{result}`", self.instructions)

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

    def test_closes_every_run_with_a_tagged_retrospective(self):
        self.assertIn("## Conflict Fix Loop Agent Retrospective", self.instructions)
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
        self.assertEqual("conflict-fix-loop", path.parent.name)
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
    def metadata(self, **overrides):
        target = MODULE.parse_target("owner/repo#7")
        with mock.patch.object(MODULE, "gh_json", return_value=gh_metadata(**overrides)):
            return MODULE.metadata_for(target)

    def test_normalizes_the_fields_the_loop_uses(self):
        metadata = self.metadata()
        self.assertEqual("owner/repo", metadata["repo_name"])
        self.assertEqual("owner", metadata["upstream_owner"])
        self.assertEqual("fork", metadata["head_owner"])
        self.assertEqual("repo", metadata["head_repo"])
        self.assertEqual("feature", metadata["head_branch"])
        self.assertEqual("head1", metadata["head_sha"])
        self.assertEqual("main", metadata["base_branch"])
        self.assertEqual("base1", metadata["base_sha"])
        self.assertEqual("CONFLICTING", metadata["mergeable"])
        self.assertEqual("DIRTY", metadata["merge_state_status"])
        self.assertEqual([{"sha": "head1", "message": "Add a thing"}], metadata["commits"])

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
            ({"baseRefOid": None}, "no base commit"),
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


class ConflictEvidenceTest(unittest.TestCase):
    def test_the_signature_ignores_the_order_of_the_paths(self):
        self.assertEqual(
            MODULE.conflict_signature(["b.py", "a.py"]),
            MODULE.conflict_signature(["a.py", "b.py"]),
        )
        self.assertNotEqual(
            MODULE.conflict_signature(["a.py"]), MODULE.conflict_signature(["a.py", "b.py"])
        )

    def test_no_progress_counts_only_the_trailing_run(self):
        history = [
            {"conflict_signature": "x"},
            {"conflict_signature": "y"},
            {"conflict_signature": "x"},
            {"conflict_signature": "x"},
        ]
        self.assertEqual(2, MODULE.detect_no_progress(history, "x"))
        self.assertEqual(0, MODULE.detect_no_progress(history, "y"))
        self.assertEqual(0, MODULE.detect_no_progress([], "x"))

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
        state = {"version": MODULE.STATE_VERSION, "iterations": 1}
        MODULE.save_state(path, state)
        loaded = MODULE.load_state(path)
        self.assertEqual(1, loaded["iterations"])
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
        self.assertTrue(entry["ended_at"].endswith("Z"))

    def test_archiving_ignores_an_unfinished_or_absent_attempt(self):
        state = {"history": [], "attempt": attempt_record(status="conflicted")}
        MODULE.archive_attempt(state)
        self.assertEqual([], state["history"])
        empty = {"history": []}
        MODULE.archive_attempt(empty)
        self.assertEqual([], empty["history"])

    def test_archiving_accepts_every_finished_status(self):
        for status in ("published", "aborted", "escalated"):
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
            iteration=2,
        )
        self.assertEqual(escalation, state["escalation"])
        self.assertEqual("contradiction", escalation["kind"])
        self.assertEqual(2, escalation["iteration"])
        self.assertTrue(escalation["recorded_at"].endswith("Z"))

    def test_every_escalation_kind_is_declared(self):
        self.assertEqual(
            (
                "contradiction",
                "max_iterations",
                "no_progress",
                "unsafe_push",
                "unknown_mergeability",
                "validation",
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
        self.assertEqual("pr-7-iteration-1", summary["id"])
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
    def test_the_pull_request_branch_is_checked_out_and_verified(self):
        with mock.patch.object(MODULE, "run") as runner, mock.patch.object(
            MODULE, "git", side_effect=["feature", "head1"]
        ):
            MODULE.checkout_pr_branch(Path("."), MODULE.parse_target("owner/repo#7"), pr_metadata())
        self.assertEqual(
            ["gh", "pr", "checkout", "https://github.com/owner/repo/pull/7"],
            runner.call_args[0][0],
        )

    def test_a_branch_mismatch_is_refused(self):
        with mock.patch.object(MODULE, "run"), mock.patch.object(
            MODULE, "git", side_effect=["other", "head1"]
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "branch mismatch"):
                MODULE.checkout_pr_branch(
                    Path("."), MODULE.parse_target("owner/repo#7"), pr_metadata()
                )

    def test_local_work_ahead_of_the_pull_request_head_is_refused(self):
        with mock.patch.object(MODULE, "run"), mock.patch.object(
            MODULE, "git", side_effect=["feature", "local9"]
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "HEAD mismatch"):
                MODULE.checkout_pr_branch(
                    Path("."), MODULE.parse_target("owner/repo#7"), pr_metadata()
                )


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
        max_iterations=MODULE.DEFAULT_MAX_ITERATIONS,
        state=None,
    ):
        args = SimpleNamespace(
            target="owner/repo#7",
            repo_root=str(self.directory),
            state=str(state or self.state_path),
            strategy=strategy,
            max_iterations=max_iterations,
        )
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
            "repository_merge_methods",
            return_value=dict(merge_methods or ALL_MERGE_METHODS),
        ), mock.patch.object(
            MODULE, "emit"
        ) as emit:
            MODULE.command_preflight(args)
        self.checkout = checkout
        return emitted(emit)

    def saved(self, path=None):
        return json.loads(Path(path or self.state_path).read_text(encoding="utf-8"))

    def test_a_conflicted_pull_request_is_ready_and_plans_an_attempt(self):
        payload = self.preflight()
        self.assertEqual("ready", payload["result"])
        self.assertEqual("conflicting", payload["mergeability"])
        self.assertEqual("merge", payload["strategy"])
        self.assertEqual(1, payload["iteration"])
        self.assertEqual(MODULE.DEFAULT_MAX_ITERATIONS, payload["max_iterations"])
        self.assertIsNone(payload["escalation"])
        state = self.saved()
        self.assertEqual("planned", state["attempt"]["status"])
        self.assertEqual("pr-7-iteration-1", state["attempt"]["id"])
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
        self.assertIsNone(self.saved()["attempt"])

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
        self.assertIsNone(self.saved()["attempt"])

    def test_no_safe_strategy_escalates_instead_of_guessing(self):
        relations = {"dependents": [dependent()], "stacked_on": None}
        payload = self.preflight(
            relations=relations, merge_methods=REBASE_ONLY_MERGE_METHODS
        )
        self.assertEqual("no_safe_strategy", payload["result"])
        self.assertIn("no safe strategy", payload["strategy_error"])
        self.assertEqual("unsafe_push", payload["escalation"]["kind"])
        self.assertIsNone(payload["strategy"])

    def test_the_iteration_cap_escalates(self):
        write_state(
            self.directory,
            iterations=MODULE.DEFAULT_MAX_ITERATIONS,
            attempt=None,
            history=[],
        )
        payload = self.preflight()
        self.assertEqual("max_iterations_reached", payload["result"])
        self.assertEqual("max_iterations", payload["escalation"]["kind"])
        self.assertEqual(MODULE.DEFAULT_MAX_ITERATIONS + 1, payload["iteration"])

    def test_the_cap_can_be_raised_for_a_single_run(self):
        write_state(self.directory, iterations=5, attempt=None, history=[])
        payload = self.preflight(max_iterations=6)
        self.assertEqual("ready", payload["result"])
        self.assertEqual(6, payload["iteration"])

    def test_a_second_preflight_archives_the_finished_attempt(self):
        write_state(
            self.directory,
            iterations=1,
            attempt=attempt_record(status="published", published_head_sha="head2"),
            history=[],
        )
        payload = self.preflight()
        self.assertEqual(1, payload["counts"]["history"])
        state = self.saved()
        self.assertEqual("published", state["history"][0]["status"])
        self.assertEqual("pr-7-iteration-2", state["attempt"]["id"])

    def test_a_new_run_clears_a_stale_escalation(self):
        write_state(
            self.directory,
            attempt=None,
            escalation={"kind": "no_progress", "reason": "stuck"},
        )
        payload = self.preflight()
        self.assertIsNone(payload["escalation"])
        self.assertIsNone(self.saved()["escalation"])

    def test_an_explicit_rebase_request_is_honoured(self):
        payload = self.preflight(strategy="rebase")
        self.assertEqual("rebase", payload["strategy"])
        self.assertEqual("rebase", self.saved()["attempt"]["strategy"])

    def test_the_default_state_path_is_used_when_none_is_given(self):
        args = SimpleNamespace(
            target="owner/repo#7",
            repo_root=None,
            state=None,
            strategy="auto",
            max_iterations=5,
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


def fake_git(head="head1", branch="feature", merge_base="merge0", heads=None):
    remaining = list(heads) if heads else None

    def call(_root, *arguments):
        if arguments[:2] == ("branch", "--show-current"):
            return branch
        if arguments[:2] == ("rev-parse", "HEAD"):
            if remaining:
                return remaining.pop(0)
            return head
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
        git_call=None,
        **state_overrides,
    ):
        state_path = write_state(self.directory, **state_overrides)
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
            MODULE, "start_integration", return_value=process or completed(1, "", "CONFLICT")
        ) as start, mock.patch.object(
            MODULE, "collect_conflicts", return_value=list(conflicts or [])
        ), mock.patch.object(
            MODULE, "merge_in_progress", return_value=merging
        ), mock.patch.object(
            MODULE, "emit"
        ) as emit:
            MODULE.command_attempt(args)
        self.state_path = state_path
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

    def test_the_same_conflict_set_twice_running_escalates(self):
        signature = MODULE.conflict_signature(["a.py"])
        history = [
            {"id": "pr-7-iteration-1", "conflict_signature": signature},
            {"id": "pr-7-iteration-2", "conflict_signature": signature},
        ]
        payload = self.attempt(
            conflicts=[conflict_record("a.py")],
            attempt=self.planned(iteration=3),
            history=history,
        )
        self.assertEqual("no_progress", payload["result"])
        self.assertEqual("no_progress", payload["escalation"]["kind"])
        self.assertIn("a.py", payload["escalation"]["reason"])
        self.assertEqual("escalated", self.saved()["attempt"]["status"])

    def test_a_different_conflict_set_is_progress(self):
        history = [
            {"conflict_signature": MODULE.conflict_signature(["b.py"])},
            {"conflict_signature": MODULE.conflict_signature(["b.py"])},
        ]
        payload = self.attempt(
            conflicts=[conflict_record("a.py")], attempt=self.planned(), history=history
        )
        self.assertEqual("conflicted", payload["result"])

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
        self.assertEqual("resolved", self.saved()["attempt"]["status"])

    def test_a_rebase_that_changed_nothing_escalates(self):
        payload = self.attempt(
            process=completed(0), attempt=self.planned(strategy="rebase"), merging=False
        )
        self.assertEqual("already_integrated", payload["result"])
        self.assertEqual("other", payload["escalation"]["kind"])
        self.assertIn("rebasing", payload["escalation"]["reason"])
        self.assertIn("changed nothing", payload["escalation"]["reason"])
        self.assertEqual("escalated", self.saved()["attempt"]["status"])

    def test_a_merge_that_changed_nothing_escalates(self):
        payload = self.attempt(process=completed(0), merging=False, attempt=self.planned())
        self.assertEqual("already_integrated", payload["result"])
        self.assertEqual("other", payload["escalation"]["kind"])
        self.assertIn("changed nothing", payload["escalation"]["reason"])
        state = self.saved()
        self.assertEqual("escalated", state["attempt"]["status"])
        self.assertEqual(1, len(state["history"]))

    def test_a_failure_with_no_conflicted_file_is_reported(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "without leaving a conflicted file"):
            self.attempt(process=completed(128, "", "fatal: bad object"), attempt=self.planned())

    def test_an_attempt_that_already_ran_is_refused(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "already conflicted"):
            self.attempt(attempt=attempt_record(status="conflicted"))

    def test_a_state_with_no_attempt_is_refused(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "run preflight first"):
            self.attempt(attempt=None)

    def test_a_branch_that_moved_away_is_refused(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "branch mismatch"):
            self.attempt(attempt=self.planned(), git_call=fake_git(branch="other"))

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
        rationale="kept the rename and the new call",
        rationale_file=None,
        accept_one_side=False,
        accept_deletion=False,
        add=None,
        **state_overrides,
    ):
        state_path = write_state(self.directory, **state_overrides)
        args = SimpleNamespace(
            state=str(state_path),
            paths=list(paths),
            rationale=rationale,
            rationale_file=rationale_file,
            accept_one_side=accept_one_side,
            accept_deletion=accept_deletion,
        )
        with mock.patch.object(
            MODULE, "stage_blobs", return_value=dict(self.blobs)
        ), mock.patch.object(
            MODULE, "git_try", return_value=add or completed(0)
        ) as git_try, mock.patch.object(
            MODULE, "emit"
        ) as emit:
            MODULE.command_resolved(args)
        self.state_path = state_path
        self.git_try = git_try
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
            ("add", "--all", "--", "app.py"), self.git_try.call_args[0][1:]
        )

    def test_remaining_conflicts_keep_the_loop_on_resolved(self):
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
        relations_before=None,
        relations_after=None,
        refreshed=None,
        final=None,
        pushed_head=None,
        **state_overrides,
    ):
        overrides = {"attempt": attempt_record(status="resolved")}
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
            MODULE, "git", side_effect=fake_git(head=local_head)
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
        ), mock.patch.object(
            MODULE, "time"
        ), mock.patch.object(
            MODULE, "emit"
        ) as emit:
            MODULE.command_publish(args)
        self.state_path = state_path
        self.verify = verify
        self.runner = runner
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
        self.assertEqual(1, payload["iterations"])
        self.assertIn("no other branch moved during the push", payload["push_verification"]["checks"])
        state = self.saved()
        self.assertEqual("published", state["attempt"]["status"])
        self.assertEqual(1, len(state["history"]))

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
        self.assertEqual(1, payload["iterations"])

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
            MODULE, "git", return_value="head1"
        ), mock.patch.object(MODULE, "emit") as emit:
            MODULE.command_abort(args)
        self.state_path = state_path
        self.runner = runner
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
        self.assertEqual(1, payload["escalation"]["iteration"])
        state = self.saved()
        self.assertEqual("escalated", state["attempt"]["status"])
        self.assertEqual(1, len(state["history"]))

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
        self.assertIsNone(payload["escalation"]["iteration"])
        self.assertEqual("contradiction", self.saved()["escalation"]["kind"])


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

    def test_the_machine_readable_answer_carries_the_loop_outcome(self):
        payload = self.status(
            iterations=2,
            attempt=attempt_record(
                status="published",
                published_head_sha="head2",
                mergeable_at_head_sha="head2",
            ),
            history=[{"id": "pr-7-iteration-1", "status": "published"}],
            relations={"dependents": [dependent()], "stacked_on": None},
        )
        self.assertEqual("ready", payload["result"])
        self.assertEqual(7, payload["pr"]["number"])
        self.assertEqual("published", payload["attempt"]["status"])
        self.assertEqual("head2", payload["mergeable_at_head_sha"])
        self.assertEqual(2, payload["iterations"])
        self.assertEqual(
            {"conflicts": 1, "dependents": 1, "history": 1}, payload["counts"]
        )

    def test_an_escalation_is_reported_verbatim(self):
        escalation = {
            "kind": "contradiction",
            "reason": "both sides changed the same guard",
            "recommended_action": "a person must choose",
            "iteration": 1,
            "recorded_at": "2026-01-01T00:00:00Z",
        }
        payload = self.status(escalation=escalation)
        self.assertEqual(escalation, payload["escalation"])

    def test_the_status_file_carries_the_full_state(self):
        payload = self.status()
        detail = json.loads(Path(payload["status_path"]).read_text(encoding="utf-8"))
        self.assertEqual(pr_metadata(), detail["pr"])
        self.assertEqual(attempt_record(), detail["attempt"])
        self.assertEqual(ALL_MERGE_METHODS, detail["merge_methods"])

    def test_a_state_with_no_attempt_still_answers(self):
        payload = self.status(attempt=None)
        self.assertIsNone(payload["attempt"])
        self.assertEqual(0, payload["counts"]["conflicts"])
        self.assertIsNone(payload["mergeable_at_head_sha"])

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

    def test_a_pull_request_this_loop_never_touched_reports_no_state(self):
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

    def test_preflight_defaults_match_the_documented_loop(self):
        args = self.parser.parse_args(["preflight"])
        self.assertIsNone(args.target)
        self.assertEqual("auto", args.strategy)
        self.assertEqual(MODULE.DEFAULT_MAX_ITERATIONS, args.max_iterations)
        self.assertEqual(5, MODULE.DEFAULT_MAX_ITERATIONS)

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

    def test_resolved_takes_several_paths_and_both_override_flags(self):
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
            ]
        )
        self.assertEqual(["a.py", "b.py"], args.paths)
        self.assertTrue(args.accept_one_side)
        self.assertTrue(args.accept_deletion)

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
            MODULE.sys, "argv", ["conflict_fix_loop.py", "status", "--state", "s.json"]
        ), mock.patch.object(MODULE, "command_status") as command:
            self.assertEqual(0, MODULE.main())
        command.assert_called_once()

    def test_a_workflow_error_is_reported_as_json_and_exits_one(self):
        with mock.patch.object(
            MODULE.sys, "argv", ["conflict_fix_loop.py", "status", "--state", "s.json"]
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
                    ["conflict_fix_loop.py", "status", "--state", "s.json"],
                ), mock.patch.object(
                    MODULE, "command_status", side_effect=error
                ), mock.patch.object(MODULE, "emit") as emit:
                    self.assertEqual(1, MODULE.main())
                self.assertEqual("error", emitted(emit)["result"])

    def test_an_unexpected_error_is_not_swallowed(self):
        with mock.patch.object(
            MODULE.sys, "argv", ["conflict_fix_loop.py", "status", "--state", "s.json"]
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
class RealGitConflictTest(unittest.TestCase):
    """Drive the git plumbing against a repository that really is conflicted."""

    def setUp(self):
        self.repo = temporary_directory(self)
        self.git("init", "--initial-branch", "main")
        self.git("config", "user.name", "Conflict Fix Loop")
        self.git("config", "user.email", "conflict-fix-loop@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "commit.gpgsign", "false")

        self.write("app.py", "def greet():\n    return 'hello'\n")
        self.write("notes.md", "notes\n")
        self.commit("Add the greeting")
        self.merge_base = self.git("rev-parse", "HEAD")

        self.git("checkout", "-b", "feature")
        self.write("app.py", "def greet(name):\n    return f'hello {name}'\n")
        self.commit("Take a name")
        self.head_sha = self.git("rev-parse", "HEAD")

        self.git("checkout", "main")
        self.write("app.py", "def greet():\n    return 'hello!'\n")
        self.commit("Add the exclamation mark")
        self.base_sha = self.git("rev-parse", "HEAD")

        self.git("checkout", "feature")

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

    def test_commit_subjects_read_the_branch_the_loop_would_republish(self):
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
            attempt=attempt_record(conflicts=conflicts),
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
            attempt=attempt_record(conflicts=conflicts),
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
                )
            )

    def test_aborting_restores_the_branch(self):
        self.start_merge()
        state_directory = temporary_directory(self)
        state_path = write_state(state_directory, repo_root=str(self.repo))
        with mock.patch.object(MODULE, "emit") as emit:
            MODULE.command_abort(SimpleNamespace(state=str(state_path)))
        payload = emitted(emit)
        self.assertEqual("merge", payload["undone"])
        self.assertEqual(self.head_sha, payload["head_sha"])
        self.assertIsNone(MODULE.integration_in_progress(self.repo))
        MODULE.require_clean_worktree(self.repo)

    def test_a_dirty_worktree_stops_the_loop(self):
        self.write("notes.md", "edited\n")
        with self.assertRaisesRegex(MODULE.WorkflowError, "worktree is not clean"):
            MODULE.require_clean_worktree(self.repo)

    def test_the_repository_remote_is_found_by_its_github_url(self):
        self.git("remote", "add", "origin", "https://github.com/owner/repo.git")
        self.assertEqual("origin", MODULE.find_remote(self.repo, "Owner/Repo", push=False))
        with self.assertRaises(MODULE.WorkflowError):
            MODULE.find_remote(self.repo, "other/repo", push=False)

    def test_the_repository_root_resolves_from_a_subdirectory(self):
        (self.repo / "nested").mkdir()
        self.assertEqual(self.repo, MODULE.resolve_repo_root(str(self.repo / "nested")))
