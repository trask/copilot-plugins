import importlib.util
from pathlib import Path
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


class AgentInstructionsTest(unittest.TestCase):
    def test_renames_the_session_from_preflight_metadata(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "tools: [read, edit, search, execute, todo, rename_session]",
            instructions,
        )
        self.assertIn("## Session Naming", instructions)
        self.assertIn(
            "Call `rename_session` exactly once per run",
            instructions,
        )
        self.assertIn(
            "call `rename_session` with `Review Loop: <PR number> - <PR title>` "
            "from its `pr.number` and `pr.title` fields",
            instructions,
        )
        self.assertIn("Never use an interim number-only name", instructions)
        # rename_session only replaces an auto-generated name; a second call is skipped.
        self.assertNotIn("call `rename_session` again", instructions)
        self.assertNotIn("immediately call `rename_session`", instructions)

    def test_bare_pr_reference_starts_the_full_review_loop(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "description: \"Use when selected with only a PR URL, PR number, or owner/repo#number",
            instructions,
        )
        self.assertIn(
            "## Activation: Bare PR References Run The Full Loop",
            instructions,
        )
        self.assertIn(
            "is an explicit request to run the full Copilot Review Loop",
            instructions,
        )
        self.assertIn(
            "Immediately choose the bundled helper command and start its `preflight` workflow",
            instructions,
        )
        self.assertIn(
            "Never invoke, hand off to, or defer to the generic `github-pr-diff-review` skill",
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
            "the PR attached to the currently checked-out branch",
            instructions,
        )
        self.assertIn(
            "Never enumerate, rank, or select saved state files",
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
        self.assertIn("maximum is 5 iterations", instructions)
        self.assertNotIn("## Approval And Advancement", instructions)
        self.assertNotIn("## Revision, Revert, And Skip", instructions)

    def test_empty_queue_without_clean_head_review_requests_review(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "`review_required`: the queue is empty but the current head has no clean Copilot review",
            instructions,
        )
        self.assertIn("`publish --state <path> --no-comments`", instructions)
        self.assertIn(
            "An empty queue is clean only when `head_review_clean` is true",
            instructions,
        )

    def test_watcher_runs_synchronously_without_terminal_notification_handoff(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "terminal parameter `mode: sync`; omit both `timeout` and `isBackground` entirely",
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

    def test_documents_durable_commit_and_reply_formats(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "Copilot comment:\n\n<original Copilot comment, verbatim>",
            instructions,
        )
        self.assertIn(
            "repeat the label and comment block for each original comment",
            instructions,
        )
        self.assertIn("without adding path attribution", instructions)
        self.assertIn("Analysis: <technical analysis and rationale>", instructions)
        self.assertIn("Upsides: <concrete benefits>", instructions)
        self.assertIn("Downsides: <concrete costs", instructions)
        self.assertIn("Addressed in <sha>.", instructions)
        self.assertIn("No code change.", instructions)
        self.assertIn("minus the `Copilot comment:` section", instructions)

    def test_documents_suppressed_comment_behavior(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("latest Copilot review", instructions)
        self.assertIn("Suppressed comments are never replied to or resolved", instructions)
        self.assertIn("re-derived on every iteration", instructions)


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
            "baseRefOid": "base",
        }

        with mock.patch.object(MODULE, "gh_json", return_value=metadata) as gh_json:
            result = MODULE.metadata_for(target)

        self.assertEqual(result["title"], "Fix the review loop")
        self.assertIn("title", gh_json.call_args.args[0][-1].split(","))

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
            "baseRefOid": "base",
        }

        with (
            mock.patch.object(MODULE, "gh_json", return_value=metadata),
            self.assertRaisesRegex(MODULE.WorkflowError, "head repository is unavailable"),
        ):
            MODULE.metadata_for(target)


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
        queue, skipped = MODULE.select_queue(self.threads)

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

        queue, _ = MODULE.select_queue([*self.threads, second_review_thread])

        self.assertEqual([comment["id"] for comment in queue], [10, 40])
        self.assertEqual([comment["review_id"] for comment in queue], [100, 200])

    def test_reports_skipped_authors_when_no_copilot_comments_remain(self):
        queue, skipped = MODULE.select_queue([self.human_thread])

        self.assertEqual(queue, [])
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
            MODULE.require_fork_head(pr)

    def test_allows_upstream_owned_pr_head_when_branch_exists(self):
        pr = {
            "upstream_owner": "open-telemetry",
            "upstream_repo": "repo",
            "head_owner": "open-telemetry",
            "head_repo": "repo",
            "head_branch": "topic",
        }

        with mock.patch.object(MODULE, "remote_head", return_value="abc123"):
            MODULE.require_fork_head(pr)

    def test_rejects_upstream_owned_pr_head_when_branch_missing(self):
        pr = {
            "upstream_owner": "open-telemetry",
            "upstream_repo": "repo",
            "head_owner": "open-telemetry",
            "head_repo": "repo",
            "head_branch": "topic",
        }

        with mock.patch.object(MODULE, "remote_head", return_value=None):
            with self.assertRaisesRegex(MODULE.WorkflowError, "refusing to push"):
                MODULE.require_fork_head(pr)


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

    def test_posts_reply_to_existing_thread_with_graphql(self):
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
        payload = {
            "data": {
                "addPullRequestReviewThreadReply": {
                    "comment": {"databaseId": 11}
                }
            }
        }

        with (
            mock.patch.object(MODULE, "fetch_review_comments", return_value=[]),
            mock.patch.object(MODULE, "gh_json", return_value={"login": "author"}),
            mock.patch.object(MODULE, "graphql", return_value=payload) as graphql,
        ):
            reply_ids = MODULE.post_missing_replies(state, [comment])

        self.assertEqual(reply_ids, {10: 11})
        self.assertEqual(comment["reply_id"], 11)
        query, variables = graphql.call_args.args
        self.assertIn("addPullRequestReviewThreadReply", query)
        self.assertEqual(
            variables,
            {
                "threadId": "THREAD_1",
                "body": "Addressed in abc123.\n\n"
                "Analysis: Applied the requested change.",
            },
        )

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
            args = SimpleNamespace(state=str(state_path), no_comments=True)

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
        post_replies.assert_not_called()
        resolve_threads.assert_not_called()
        self.assertEqual(emit.call_args.args[0]["reply_ids"], {})

    def test_publishes_a_suppressed_only_queue(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "iterations": 2,
            "repo_root": "repo",
            "pr": {
                "head_owner": "author",
                "head_repo": "repo",
                "head_branch": "branch",
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
            args = SimpleNamespace(state=str(state_path), no_comments=False)

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


class WatcherStateTest(unittest.TestCase):
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
        max_iterations=5,
    ):
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
            if iterations:
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
            )

            with (
                mock.patch.object(MODULE, "require_tools"),
                mock.patch.object(MODULE, "resolve_repo_root", return_value=Path(directory)),
                mock.patch.object(MODULE, "git", side_effect=fake_git),
                mock.patch.object(MODULE, "metadata_for", return_value=metadata),
                mock.patch.object(MODULE, "run"),
                mock.patch.object(MODULE, "fetch_threads", return_value=threads or []),
                mock.patch.object(MODULE, "fetch_reviews", return_value=reviews or []),
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_preflight(args)

        return emit.call_args.args[0]

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
            "body": "No comments.",
            "user": {"login": "copilot-pull-request-reviewer[bot]"},
        }

        payload = self.run_preflight(reviews=[review])

        self.assertEqual(payload["result"], "no_unresolved_comments")
        self.assertEqual(payload["head_review_id"], 10)
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

    def test_preflight_caps_empty_review_required_iteration(self):
        payload = self.run_preflight(iterations=5, max_iterations=5)

        self.assertEqual(payload["result"], "max_iterations_reached")
        self.assertEqual(payload["iteration"], 6)
        self.assertEqual(payload["max_iterations"], 5)

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
        self.assertEqual(payload["max_iterations"], 5)


if __name__ == "__main__":
    unittest.main()