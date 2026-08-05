import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "pr_review_comments.py"
AGENT = Path(__file__).parents[2] / "pr-review-comments.agent.md"
SPEC = importlib.util.spec_from_file_location("pr_review_comments", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AgentInstructionsTest(unittest.TestCase):
    def test_targetless_requests_resolve_the_current_branch_pr(self):
        instructions = AGENT.read_text(encoding="utf-8")

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

    def test_documents_windows_safe_git_bash_helper_path(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn('${USERPROFILE//\\\\//}/.copilot/agents/pr-review-comments', instructions)
        self.assertNotIn("~/.copilot/agents/pr-review-comments", instructions)

    def test_every_watcher_result_ends_with_attention_triggering_final_response(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "present the **Review Comment Result** as the turn's final response",
            instructions,
        )
        self.assertIn(
            "If the result is `review_no_comments`, report the completed review ID as the turn's final response",
            instructions,
        )
        self.assertIn(
            "For every terminal `watch` result, send the user-facing report as the turn's final response",
            instructions,
        )
        self.assertGreaterEqual(
            instructions.count(
                "the final response is required so VS Code raises its normal attention notification"
            ),
            2,
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


class ParseTargetTest(unittest.TestCase):
    def test_parses_review_url(self):
        target = MODULE.parse_target(
            "https://github.com/open-telemetry/opentelemetry-java-instrumentation/"
            "pull/19233#pullrequestreview-4708244602"
        )

        self.assertEqual(target["scope_type"], "review")
        self.assertEqual(target["scope_id"], 4708244602)
        self.assertEqual(target["number"], 19233)

    def test_parses_comment_url(self):
        target = MODULE.parse_target(
            "https://github.com/open-telemetry/opentelemetry-java-instrumentation/"
            "pull/19233#discussion_r3590845592"
        )

        self.assertEqual(target["scope_type"], "comment")
        self.assertEqual(target["scope_id"], 3590845592)

    def test_parses_short_pr_target(self):
        target = MODULE.parse_target("open-telemetry/opentelemetry-java-instrumentation#19233")

        self.assertEqual(target["scope_type"], "pr")
        self.assertIsNone(target["scope_id"])


class CliPathTest(unittest.TestCase):
    def test_converts_git_bash_drive_path_on_windows(self):
        with mock.patch.object(MODULE.os, "name", "nt"):
            path = MODULE.cli_path("/c/src/repo")

        self.assertEqual(path, Path("C:/src/repo").resolve())

    def test_resolve_repo_root_uses_converted_path(self):
        completed = mock.Mock(stdout="C:/src/repo\n")
        with (
            mock.patch.object(MODULE.os, "name", "nt"),
            mock.patch.object(MODULE, "run", return_value=completed) as run,
        ):
            MODULE.resolve_repo_root("/c/src/repo")

        self.assertEqual(run.call_args.args[0][:3], ["git", "-C", "C:\\src\\repo"])


class CurrentPrStatusTest(unittest.TestCase):
    def test_resolves_current_pr_from_checked_out_repository(self):
        completed = mock.Mock(
            stdout='{"url":"https://github.com/open-telemetry/repo/pull/42"}\n'
        )
        repo_root = Path("repo")

        with mock.patch.object(MODULE, "run", return_value=completed) as run:
            target = MODULE.current_pr_target(repo_root)

        self.assertEqual(target["number"], 42)
        run.assert_called_once_with(
            ["gh", "pr", "view", "--json", "url"], cwd=repo_root
        )

    def test_status_current_loads_only_current_pr_state(self):
        target = MODULE.parse_target("https://github.com/open-telemetry/repo/pull/42")
        state = {
            "version": MODULE.STATE_VERSION,
            "pr": {"number": 42, "url": target["pr_url"]},
            "active_queue": "pr-42",
            "queues": {"pr-42": {"id": "pr-42"}},
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
        self.threads = [
            {
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
                            "author": {"login": "reviewer"},
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
            },
            {"id": "thread-2", "isResolved": True, "comments": {"nodes": []}},
        ]

    def test_pr_scope_selects_only_unresolved_thread_roots(self):
        queue = MODULE.select_queue(self.threads, "pr", None)

        self.assertEqual([comment["id"] for comment in queue], [10])

    def test_review_scope_selects_matching_review_comments(self):
        queue = MODULE.select_queue(self.threads, "review", 101)

        self.assertEqual([comment["id"] for comment in queue], [11])

    def test_fetches_only_selected_thread_ids(self):
        payload = {"data": {"t0": self.threads[0]}}
        with mock.patch.object(MODULE, "graphql", return_value=payload) as graphql:
            threads = MODULE.fetch_threads_by_id(["thread-1", "thread-1"])

        self.assertEqual(threads, [self.threads[0]])
        self.assertIn('node(id:"thread-1")', graphql.call_args.args[0])


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
            "summary": "Applied the requested change.",
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
                "body": "Addressed in abc123: Applied the requested change.",
            },
        )

    def test_publishes_empty_follow_up_without_reply_operations(self):
        state = {
            "version": MODULE.STATE_VERSION,
            "repo_root": "repo",
            "pr": {
                "head_owner": "author",
                "head_repo": "repo",
                "head_branch": "branch",
            },
            "active_queue": "review-1",
            "queues": {
                "review-1": {
                    "comments": [],
                    "status": "active",
                }
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            MODULE.save_state(state_path, state)
            args = SimpleNamespace(
                state=str(state_path), all_queues=False, no_comments=True
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
            "queues": {},
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
                mock.patch.object(MODULE, "emit"),
            ):
                MODULE.command_preflight(args)

            saved = MODULE.load_state(path)

        self.assertEqual(saved["monitoring"]["status"], "completed")
        self.assertEqual(
            saved["monitoring"]["result"], {"result": "cancelled_locally"}
        )
        self.assertEqual(saved["active_queue"], "review-2")


if __name__ == "__main__":
    unittest.main()