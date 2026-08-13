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
            "call `rename_session` with `Copilot Review Loop: <PR number> - <PR title>` "
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
        self.assertIn("maximum is 5 iterations per invocation", instructions)
        self.assertNotIn("## Approval And Advancement", instructions)
        self.assertNotIn("## Revision, Revert, And Skip", instructions)

    def test_publish_detects_remote_head_divergence_before_push(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "compare the live remote PR head with the preflight pin immediately "
            "before pushing",
            instructions,
        )
        self.assertIn(
            "If `publish` returns `head_changed`, stop without retrying or pushing",
            instructions,
        )

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

    def test_uses_pinned_head_ci_as_review_evidence(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "CI logs and generated report artifacts for the exact pinned PR head "
            "as first-class evidence",
            instructions,
        )
        self.assertIn("never use results from another head", instructions)
        self.assertIn(
            "Pass all paths after one `--paths` flag or repeat the flag; the helper "
            "retains every value",
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
            "Always spell the required `--batch` and `--comments` flags",
            instructions,
        )
        self.assertIn(
            "never pass the batch ID or comment IDs positionally",
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
            "Initialize a run-local iteration counter to 0 before the first preflight",
            instructions,
        )
        self.assertIn(
            "After `published`, increment the run-local iteration counter exactly once",
            instructions,
        )
        self.assertIn(
            "`<n>` is the run-local iteration counter, not the helper's cumulative "
            "persisted iteration count",
            instructions,
        )
        self.assertIn(
            "exits clean during its first preflight reports `0 iterations`",
            instructions,
        )
        self.assertIn(
            "begins with four persisted iterations and publishes once reports `1 iteration`",
            instructions,
        )
        self.assertIn(
            "`preflight --completed-run-iterations <n>`", instructions
        )
        self.assertIn(
            "persisted iterations from earlier invocations never consume the current "
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

    def test_documents_file_based_commit_message_authoring(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "Write the whole commit message to a temporary UTF-8 file outside the "
            "repository and commit it with `git commit -F <path>`",
            instructions,
        )
        self.assertIn(
            "Never assemble the message with `git commit -m` or with shell escape "
            "sequences",
            instructions,
        )
        self.assertIn("read the message back with `git log -1 --pretty=%B`", instructions)

    def test_documents_suppressed_comment_behavior(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("latest Copilot review", instructions)
        self.assertIn("Suppressed comments are never replied to or resolved", instructions)
        self.assertIn("re-derived on every iteration", instructions)

    def test_documents_independent_reply_publication(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "post each thread reply idempotently as its own published comment",
            instructions,
        )
        self.assertIn(
            "Each reply is published on its own rather than bundled into one review",
            instructions,
        )
        self.assertIn(
            "verification fails if any reply is left in an unsubmitted review",
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
            "loop, an unfixable validation stop, `max_iterations_reached`, "
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
            "Report only friction actually encountered in this run", instructions
        )
        self.assertIn(
            "The **Copilot Review Loop Agent Retrospective** is the only content "
            "permitted after the `**Outcome:**` line",
            instructions,
        )
        self.assertIn("The retrospective is advisory and chat-only", instructions)
        self.assertIn(
            "never turn it into a thread reply, commit, or any other GitHub mutation",
            instructions,
        )
        self.assertIn(
            "omit the label entirely when there is nothing to report", instructions
        )
        self.assertIn("Emit exactly one terminal response", instructions)
        self.assertIn("must be the absolute final block", instructions)
        self.assertIn("after its last list item, stop immediately", instructions)
        self.assertIn(
            "never emit a preliminary final response followed by a fuller report",
            instructions,
        )
        self.assertIn("never send a post-retrospective recap", instructions)

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
        self.assertIn("Once it is sent the run is over", instructions)
        self.assertIn(
            "never send another message because a tool result, reminder, or turn "
            "boundary invites one",
            instructions,
        )
        self.assertIn(
            "never open with a narrative recap of what the run did", instructions
        )
        self.assertIn(
            "render the `**Outcome:**` line at most once and never begin a second "
            "report",
            instructions,
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
            args = SimpleNamespace(state=str(state_path), no_comments=True)

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
            args = SimpleNamespace(state=str(state_path), no_comments=True)

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
        completed_run_iterations=0,
        max_iterations=5,
        local_branch="branch",
        checked_out_branch=True,
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
                completed_run_iterations=completed_run_iterations,
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

    def test_preflight_ignores_persisted_iterations_for_run_cap(self):
        payload = self.run_preflight(iterations=5, max_iterations=5)

        self.assertEqual(payload["result"], "review_required")
        self.assertEqual(payload["iteration"], 1)
        self.assertEqual(payload["completed_run_iterations"], 0)
        self.assertEqual(payload["max_iterations"], 5)
        self.assertEqual(payload["total_iterations"], 5)

    def test_preflight_caps_empty_review_required_iteration_for_current_run(self):
        payload = self.run_preflight(
            iterations=12, completed_run_iterations=5, max_iterations=5
        )

        self.assertEqual(payload["result"], "max_iterations_reached")
        self.assertEqual(payload["iteration"], 6)
        self.assertEqual(payload["completed_run_iterations"], 5)
        self.assertEqual(payload["max_iterations"], 5)
        self.assertEqual(payload["total_iterations"], 12)

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


if __name__ == "__main__":
    unittest.main()