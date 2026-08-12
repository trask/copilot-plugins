from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "pr_description_loop.py"
AGENT = ROOT / "agents" / "pr-description-loop.agent.md"
PLUGIN = ROOT / "plugin.json"
MARKETPLACE = ROOT.parents[1] / ".github" / "plugin" / "marketplace.json"
SPEC = importlib.util.spec_from_file_location("pr_description_loop", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def pr_metadata(**overrides):
    url = "https://github.com/owner/repo/pull/7"
    metadata = {
        "owner": "owner",
        "repo": "repo",
        "number": 7,
        "repo_name": "owner/repo",
        "pr_url": url,
        "url": url,
        "title": "Current title",
        "body": "Current body",
        "head_sha": "head1",
        "is_draft": False,
    }
    metadata.update(overrides)
    return metadata


def write_state(directory: Path, **overrides) -> Path:
    state = {
        "version": MODULE.STATE_VERSION,
        "kind": MODULE.RUN_KIND,
        "created_at": "2026-01-01T00:00:00Z",
        "run_id": "run-1",
        "proposal_count": 0,
        "repo_root": str(directory),
        "pr": pr_metadata(),
        "pinned_at": "2026-01-01T00:00:00Z",
    }
    state.update(overrides)
    path = directory / "state.json"
    MODULE.save_state(path, state)
    return path


class AgentInstructionsTest(unittest.TestCase):
    def setUp(self):
        self.instructions = AGENT.read_text(encoding="utf-8")

    def test_is_manually_selected_and_user_invocable(self):
        self.assertIn("user-invocable: true", self.instructions)
        self.assertIn("disable-model-invocation: true", self.instructions)
        self.assertIn("This agent is manually selected and user-invocable", self.instructions)

    def test_renames_once_after_preflight(self):
        self.assertIn("tools: [read, search, execute, todo, rename_session]", self.instructions)
        self.assertIn("After preflight succeeds, call `rename_session` exactly once", self.instructions)
        self.assertIn("`PR Description Loop: <number> - <title>`", self.instructions)
        self.assertIn("never rename again during this run", self.instructions)

    def test_always_shows_current_text_before_evaluating_or_proposing(self):
        self.assertIn(
            "show the current title and current description before your "
            "evaluation or any proposal",
            self.instructions,
        )
        self.assertIn(
            "present the current title and description first",
            self.instructions,
        )
        self.assertIn("including an empty description", self.instructions)

    def test_renders_title_and_description_without_code_blocks(self):
        self.assertIn("## Displaying Title And Description", self.instructions)
        self.assertIn(
            "Never wrap a displayed title or description in a fenced code block "
            "or inline code span",
            self.instructions,
        )
        self.assertIn(
            "Never use a fenced code block, inline code span, or any other "
            "verbatim wrapper around the title or the description",
            self.instructions,
        )
        self.assertIn(
            "Render the description as ordinary markdown so the interface wraps it",
            self.instructions,
        )
        self.assertIn(
            "Never summarize, normalize, reflow, hard wrap, re-indent, or "
            "silently repair either value",
            self.instructions,
        )
        self.assertIn(
            "a bold label on its own line, then a blank line, then the value "
            "as a blockquote",
            self.instructions,
        )
        self.assertIn(
            "Prefix every line of the value with `> `, including blank lines",
            self.instructions,
        )
        self.assertIn(
            "The `> ` prefix is presentation only and is never part of the "
            "stored value",
            self.instructions,
        )
        self.assertIn(
            "Do not add horizontal rules around it; the blockquote is the boundary",
            self.instructions,
        )
        for label in (
            "`**Current title**`",
            "`**Current description**`",
            "`**Proposed title**`",
            "`**Proposed description**`",
        ):
            self.assertIn(label, self.instructions)
        self.assertIn("Show an empty description as `> _(empty)_`", self.instructions)
        self.assertNotIn("```text", self.instructions)
        self.assertNotIn("\n***\n", self.instructions)

    def test_summarizes_how_a_proposal_differs_from_the_current_text(self):
        self.assertIn("## Summarizing What Changed", self.instructions)
        self.assertIn(
            "Every time you display a proposal, follow it with a "
            "`**What changed**` summary of how that proposal differs from the "
            "current title and description, before asking for approval",
            self.instructions,
        )
        self.assertIn(
            "Describe the differences only. Never restate the full proposed "
            "title or body",
            self.instructions,
        )
        self.assertIn(
            "Repeat the summary for every revision",
            self.instructions,
        )
        self.assertIn(
            "add the `**What changed**` summary from \"Summarizing What "
            "Changed\", then ask for explicit approval",
            self.instructions,
        )

    def test_immediately_evaluates_and_recommends_a_decision(self):
        self.assertIn(
            "Immediately evaluate the current text against the diff for clarity, "
            "concision, consistency, and scope",
            self.instructions,
        )
        self.assertIn(
            'Never insert a neutral "does this look good?" turn',
            self.instructions,
        )
        self.assertIn(
            "If the current text is strong, recommend keeping it and ask only for "
            "explicit approval",
            self.instructions,
        )
        self.assertIn(
            "If it is weak, explain why briefly and immediately show a complete "
            "replacement",
            self.instructions,
        )
        self.assertIn(
            "Do not first ask whether the current text looks good",
            self.instructions,
        )

    def test_requires_explicit_session_approval(self):
        self.assertIn(
            "Never mutate GitHub unless the user explicitly approves the exact title "
            "and exact body in this session",
            self.instructions,
        )
        self.assertIn(
            "Silence, lack of objection, earlier instructions, prior approval of "
            "different text, persistent memory, and inferred intent are not approval",
            self.instructions,
        )
        self.assertIn(
            "Call `propose` and `apply` only after explicit approval",
            self.instructions,
        )

    def test_keeps_strong_text_approval_single_path_and_resolves_optional_concerns(self):
        self.assertIn(
            "ask only for explicit approval of the exact displayed title and body",
            self.instructions,
        )
        self.assertIn(
            "Do not pair that approval request with an offer to make an optional "
            "alternative change",
            self.instructions,
        )
        self.assertIn(
            "If a possible tweak is not important enough to change the "
            "recommendation, omit it",
            self.instructions,
        )
        self.assertIn(
            "A reply that explicitly resolves the sole optional concern you raised "
            "in favor of the displayed current wording also counts",
            self.instructions,
        )
        self.assertIn(
            "`List.of is fine` approves the displayed title and body without another "
            "confirmation turn",
            self.instructions,
        )
        self.assertIn(
            "addresses only one of several open concerns, introduces new feedback, "
            "or is otherwise ambiguous",
            self.instructions,
        )

    def test_validates_approved_current_text_without_mutation(self):
        self.assertIn(
            "`validate --state <path> --expected-head <head_sha> "
            "--expected-run-id <run_id> --no-change`",
            self.instructions,
        )
        self.assertIn("do not run `propose` or `apply`", self.instructions)

    def test_documents_description_style_and_diff_source(self):
        self.assertIn(
            "`gh pr diff <pr.url> --repo <pr.repo_name>`",
            self.instructions,
        )
        for forbidden_header in ("`Summary`", "`Details`", "`Testing`"):
            self.assertIn(forbidden_header, self.instructions)
        self.assertIn("Do not include validation lists", self.instructions)
        self.assertIn(
            "Default to short, single-idea paragraphs", self.instructions
        )
        self.assertIn(
            "Split a paragraph as soon as it starts covering more than one idea",
            self.instructions,
        )
        self.assertIn("Never hard wrap prose", self.instructions)

    def test_prioritizes_user_facing_examples_and_skimmable_structure(self):
        self.assertIn(
            "Open with a short, unheaded paragraph that states the user-visible "
            "outcome",
            self.instructions,
        )
        self.assertIn(
            "When the pull request changes configuration, put concise "
            "before-and-after configuration examples immediately after the opening "
            "paragraph",
            self.instructions,
        )
        self.assertIn(
            "Use actual keys and representative values for each materially distinct "
            "configuration surface",
            self.instructions,
        )
        self.assertIn(
            "When the pull request changes one, show a concrete usage example early "
            "in the body",
            self.instructions,
        )
        self.assertIn(
            "use before-and-after examples when callers must migrate from existing "
            "API usage",
            self.instructions,
        )
        self.assertIn(
            "give distinct substantial ideas descriptive, topic-specific headings",
            self.instructions,
        )
        self.assertIn(
            "Do not add headings to a short or single-idea body",
            self.instructions,
        )
        self.assertIn(
            "Do not turn the body into an exhaustive change log",
            self.instructions,
        )

    def test_restarts_on_head_change_and_uses_external_body_file(self):
        self.assertIn(
            "discard the stale proposal, run preflight again", self.instructions
        )
        self.assertIn("The earlier approval does not carry forward", self.instructions)
        self.assertIn(
            "UTF-8 to a body file outside the repository", self.instructions
        )

    def test_documents_run_capabilities_and_residual_update_race(self):
        self.assertIn(
            "returned `run_id` and `proposal_token` as capabilities",
            self.instructions,
        )
        self.assertIn(
            "does not support conditional unsafe requests", self.instructions
        )
        self.assertIn("Never describe this as an atomic compare-and-swap", self.instructions)
        self.assertIn("twice immediately before a direct REST `PATCH`", self.instructions)

    def test_closes_every_run_with_a_categorized_retrospective(self):
        self.assertIn(
            "## PR Description Loop Agent Retrospective", self.instructions
        )
        self.assertIn(
            "**PR Description Loop Agent Retrospective**", self.instructions
        )
        self.assertIn("Emit exactly one terminal response", self.instructions)
        self.assertIn("must be the absolute final block", self.instructions)
        self.assertIn("after its last list item, stop immediately", self.instructions)
        self.assertIn(
            "never emit a preliminary final response followed by a fuller report",
            self.instructions,
        )
        self.assertIn(
            "Silence is the normal outcome, and a run that went smoothly reports "
            "nothing",
            self.instructions,
        )
        self.assertIn(
            "Produce the retrospective on every terminal outcome, including a "
            "validated unchanged text, an applied proposal, a moved head that "
            "discarded a proposal, a helper error, and a run the user ends without "
            "approving anything",
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
            "Report only friction actually encountered in this run", self.instructions
        )
        self.assertIn("The retrospective is advisory and chat-only", self.instructions)
        self.assertIn(
            "never fold it into a pull request title or description", self.instructions
        )
        self.assertIn(
            "omit the label entirely when there is nothing to report", self.instructions
        )
        self.assertIn(
            "never replaces, reorders, or alters the required final response",
            self.instructions,
        )

    def test_manifest_and_marketplace_versions_match(self):
        plugin = json.loads(PLUGIN.read_text(encoding="utf-8"))
        marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
        entry = next(
            item for item in marketplace["plugins"] if item["name"] == plugin["name"]
        )
        self.assertEqual(plugin["version"], "1.0.10")
        self.assertEqual(entry["version"], plugin["version"])
        self.assertEqual(entry["source"], "./plugins/pr-description-loop")


class TargetParsingTest(unittest.TestCase):
    def test_accepts_urls_short_targets_and_bare_numbers_with_context(self):
        expected = {
            "owner": "owner",
            "repo": "repo",
            "number": 7,
            "repo_name": "owner/repo",
            "pr_url": "https://github.com/owner/repo/pull/7",
        }
        self.assertEqual(
            MODULE.parse_target("https://github.com/owner/repo/pull/7"), expected
        )
        self.assertEqual(MODULE.parse_target("owner/repo#7"), expected)
        self.assertEqual(
            MODULE.parse_target("#7", repo_name="owner/repo"), expected
        )
        self.assertEqual(MODULE.parse_target("7", repo_name="owner/repo"), expected)
        self.assertEqual(
            MODULE.parse_target(
                "https://github.com/owner/repo/pull/7#discussion_r1"
            ),
            expected,
        )

    def test_rejects_invalid_targets_and_context_free_bare_numbers(self):
        for value in (
            "owner/repo",
            "https://github.com/owner/repo/issues/7",
            "not-a-target",
            "7",
        ):
            with self.subTest(value=value):
                with self.assertRaises(MODULE.WorkflowError):
                    MODULE.parse_target(value)

    def test_resolves_a_bare_number_from_current_repository_context(self):
        with mock.patch.object(
            MODULE, "repository_context", return_value="owner/repo"
        ) as repository_context:
            target = MODULE.resolve_target("7", Path("repo"))

        self.assertEqual(target["pr_url"], "https://github.com/owner/repo/pull/7")
        repository_context.assert_called_once_with(Path("repo"))

    def test_resolves_an_omitted_target_from_the_current_branch(self):
        expected = MODULE.parse_target("owner/repo#7")
        with mock.patch.object(
            MODULE, "current_pr_target", return_value=expected
        ) as current_pr_target:
            target = MODULE.resolve_target(None, Path("repo"))

        self.assertEqual(target, expected)
        current_pr_target.assert_called_once_with(Path("repo"))

    def test_reads_repository_context_from_gh(self):
        with mock.patch.object(
            MODULE, "gh_json", return_value={"nameWithOwner": "owner/repo"}
        ) as gh_json:
            self.assertEqual(MODULE.repository_context(Path("repo")), "owner/repo")

        gh_json.assert_called_once_with(
            ["repo", "view", "--json", "nameWithOwner"], cwd=Path("repo")
        )

    def test_reads_pr_metadata_from_the_rest_resource_used_for_updates(self):
        payload = {
            "number": 7,
            "html_url": "https://github.com/owner/repo/pull/7",
            "title": "Title",
            "body": None,
            "head": {"sha": "head1"},
            "draft": False,
        }
        with mock.patch.object(MODULE, "gh_json", return_value=payload) as gh_json:
            metadata = MODULE.metadata_for(MODULE.parse_target("owner/repo#7"))

        self.assertEqual(metadata["body"], "")
        self.assertEqual(metadata["head_sha"], "head1")
        gh_json.assert_called_once_with(["api", "repos/owner/repo/pulls/7"])

    def test_current_branch_without_upstream_uses_direct_gh_resolution(self):
        expected = MODULE.parse_target("owner/repo#7")
        with (
            mock.patch.object(MODULE, "git", return_value="feature"),
            mock.patch.object(MODULE, "configured_upstream", return_value=None),
            mock.patch.object(
                MODULE, "simple_current_pr_target", return_value=expected
            ) as simple,
            mock.patch.object(MODULE, "exact_upstream_pr_targets") as exact,
        ):
            target = MODULE.current_pr_target(Path("repo"))

        self.assertEqual(target, expected)
        simple.assert_called_once_with(Path("repo"), None)
        exact.assert_not_called()

    def test_current_branch_with_upstream_uses_exact_remote_resolution(self):
        expected = MODULE.parse_target("owner/repo#7")
        upstream = {"repo": "fork/repo", "branch": "feature"}
        with (
            mock.patch.object(MODULE, "git", return_value="feature"),
            mock.patch.object(
                MODULE, "configured_upstream", return_value=upstream
            ),
            mock.patch.object(MODULE, "simple_current_pr_target") as simple,
            mock.patch.object(
                MODULE, "exact_upstream_pr_targets", return_value=[expected]
            ) as exact,
        ):
            target = MODULE.current_pr_target(Path("repo"))

        self.assertEqual(target, expected)
        simple.assert_not_called()
        exact.assert_called_once_with(upstream)

    def test_normalizes_git_bash_style_paths_on_windows(self):
        self.assertEqual(
            MODULE.normalize_cli_path("/c/Users/me/state.json", windows=True),
            "C:/Users/me/state.json",
        )
        self.assertEqual(
            MODULE.normalize_cli_path("/c/Users/me/state.json", windows=False),
            "/c/Users/me/state.json",
        )


class StatePersistenceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.emitted = []
        patcher = mock.patch.object(MODULE, "emit", self.emitted.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_atomically_round_trips_state(self):
        path = write_state(self.directory)

        state = MODULE.load_state(path)

        self.assertEqual(state["pr"]["head_sha"], "head1")
        self.assertIn("updated_at", state)
        self.assertTrue(path.read_bytes().endswith(b"\n"))
        self.assertEqual(
            list(self.directory.glob(f".{path.name}.*.tmp")),
            [],
        )

    def test_rejects_an_unsupported_state_version(self):
        path = write_state(self.directory)
        state = json.loads(path.read_text(encoding="utf-8"))
        state["version"] = MODULE.STATE_VERSION + 1
        path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaisesRegex(MODULE.WorkflowError, "unsupported state version"):
            MODULE.load_state(path)

    def test_preflight_initializes_pinned_state(self):
        path = self.directory / "state.json"
        metadata = pr_metadata()
        target = MODULE.parse_target("owner/repo#7")
        args = SimpleNamespace(
            target="owner/repo#7",
            repo_root=str(self.directory),
            state=str(path),
        )
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(
                MODULE, "resolve_repo_root", return_value=self.directory
            ),
            mock.patch.object(MODULE, "resolve_target", return_value=target),
            mock.patch.object(MODULE, "metadata_for", return_value=metadata),
        ):
            MODULE.command_preflight(args)

        state = MODULE.load_state(path)
        self.assertEqual(state["kind"], MODULE.RUN_KIND)
        self.assertEqual(state["run_id"], self.emitted[-1]["run_id"])
        self.assertEqual(state["pr"], metadata)
        self.assertEqual(state["proposal_count"], 0)
        self.assertEqual(self.emitted[-1]["title"], "Current title")
        self.assertEqual(self.emitted[-1]["body"], "Current body")
        self.assertEqual(self.emitted[-1]["head_sha"], "head1")

    def test_two_default_preflights_create_isolated_runs_and_a_stable_index(self):
        index_path = self.directory / "owner--repo--7.json"
        args = SimpleNamespace(
            target="owner/repo#7",
            repo_root=str(self.directory),
            state=None,
        )
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(
                MODULE, "resolve_repo_root", return_value=self.directory
            ),
            mock.patch.object(
                MODULE,
                "resolve_target",
                return_value=MODULE.parse_target("owner/repo#7"),
            ),
            mock.patch.object(MODULE, "metadata_for", return_value=pr_metadata()),
            mock.patch.object(
                MODULE, "default_state_path", return_value=index_path
            ),
            mock.patch.object(
                MODULE, "secrets"
            ) as secrets_module,
        ):
            secrets_module.token_hex.side_effect = [
                "run-a",
                "lock-a",
                "run-b",
                "lock-b",
            ]
            MODULE.command_preflight(args)
            first = self.emitted[-1]
            body_path = self.directory / "body.md"
            body_path.write_text("Approved body", encoding="utf-8")
            MODULE.command_propose(
                SimpleNamespace(
                    state=first["state"],
                    expected_run_id="run-a",
                    title="Approved title",
                    body_file=str(body_path),
                )
            )
            MODULE.command_preflight(args)
            second = self.emitted[-1]

        self.assertNotEqual(first["state"], second["state"])
        self.assertEqual(first["run_id"], "run-a")
        self.assertEqual(second["run_id"], "run-b")
        self.assertTrue(Path(first["state"]).is_file())
        self.assertTrue(Path(second["state"]).is_file())
        self.assertEqual(
            MODULE.load_state(Path(first["state"]))["proposal"]["title"],
            "Approved title",
        )
        index = MODULE.load_state(index_path)
        self.assertEqual(index["kind"], MODULE.INDEX_KIND)
        self.assertEqual(index["latest_run_id"], "run-b")
        self.assertEqual(
            [item["run_id"] for item in index["runs"]], ["run-a", "run-b"]
        )

    def test_preflight_replaces_null_current_timestamp_in_stable_index(self):
        index_path = self.directory / "owner--repo--7.json"
        MODULE.save_state(
            index_path,
            {
                "version": MODULE.STATE_VERSION,
                "kind": MODULE.INDEX_KIND,
                "created_at": "2026-01-01T00:00:00Z",
                "pr": pr_metadata(),
                "runs": [],
                "latest_run_id": None,
                "latest_state": None,
                "current_updated_at": None,
            },
        )
        target = MODULE.parse_target("owner/repo#7")
        args = SimpleNamespace(
            target="owner/repo#7",
            repo_root=str(self.directory),
            state=None,
        )
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(
                MODULE, "resolve_repo_root", return_value=self.directory
            ),
            mock.patch.object(MODULE, "resolve_target", return_value=target),
            mock.patch.object(MODULE, "current_pr_target", return_value=target),
            mock.patch.object(MODULE, "metadata_for", return_value=pr_metadata()),
            mock.patch.object(
                MODULE, "default_state_path", return_value=index_path
            ),
            mock.patch.object(MODULE, "secrets") as secrets_module,
        ):
            secrets_module.token_hex.side_effect = ["run-null", "lock-null"]
            MODULE.command_preflight(args)
            preflight = self.emitted[-1]
            MODULE.command_status(
                SimpleNamespace(state=None, current=True, repo_root=None)
            )

        index = MODULE.load_state(index_path)
        self.assertEqual(index["latest_run_id"], "run-null")
        self.assertEqual(index["latest_state"], preflight["state"])
        self.assertIsInstance(index["current_updated_at"], str)
        self.assertEqual(self.emitted[-1]["kind"], MODULE.INDEX_KIND)
        self.assertEqual(self.emitted[-1]["latest_run_id"], "run-null")

    def test_propose_increments_a_durable_counter_and_preserves_body(self):
        path = write_state(self.directory)
        body_path = self.directory / "body.md"
        body_path.write_text("First paragraph.\n\n- One\n- Two", encoding="utf-8")

        MODULE.command_propose(
            SimpleNamespace(
                state=str(path),
                expected_run_id="run-1",
                title="First title",
                body_file=str(body_path),
            )
        )
        MODULE.command_propose(
            SimpleNamespace(
                state=str(path),
                expected_run_id="run-1",
                title="Second title",
                body_file=str(body_path),
            )
        )

        state = MODULE.load_state(path)
        self.assertEqual(state["proposal_count"], 2)
        self.assertEqual(state["proposal"]["number"], 2)
        self.assertEqual(state["proposal"]["title"], "Second title")
        self.assertEqual(state["proposal"]["run_id"], "run-1")
        self.assertEqual(
            state["proposal"]["base"],
            {
                "head_sha": "head1",
                "title": "Current title",
                "body": "Current body",
            },
        )
        self.assertEqual(
            state["proposal"]["token"],
            MODULE.proposal_token_for(state["proposal"]),
        )
        self.assertEqual(self.emitted[-1]["proposal_token"], state["proposal"]["token"])
        self.assertEqual(
            state["proposal"]["body"], "First paragraph.\n\n- One\n- Two"
        )

    def test_propose_rejects_a_blank_title_without_changing_state(self):
        path = write_state(self.directory)
        body_path = self.directory / "body.md"
        body_path.write_text("Body", encoding="utf-8")

        with self.assertRaisesRegex(MODULE.WorkflowError, "must not be blank"):
            MODULE.command_propose(
                SimpleNamespace(
                    state=str(path),
                    expected_run_id="run-1",
                    title=" \t",
                    body_file=str(body_path),
                )
            )

        self.assertEqual(MODULE.load_state(path)["proposal_count"], 0)

    def test_propose_rejects_a_missing_body_file(self):
        path = write_state(self.directory)

        with self.assertRaisesRegex(MODULE.WorkflowError, "could not read body file"):
            MODULE.command_propose(
                SimpleNamespace(
                    state=str(path),
                    expected_run_id="run-1",
                    title="Title",
                    body_file=str(self.directory / "missing.md"),
                )
            )


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.emitted = []
        patcher = mock.patch.object(MODULE, "emit", self.emitted.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def state_with_proposal(self, **proposal_overrides):
        proposal = {
            "number": 1,
            "run_id": "run-1",
            "base": {
                "head_sha": "head1",
                "title": "Current title",
                "body": "Current body",
            },
            "title": "Proposed title",
            "body": "Proposed body",
            "proposed_at": "2026-01-01T00:00:00Z",
        }
        proposal.update(proposal_overrides)
        proposal["token"] = MODULE.proposal_token_for(proposal)
        return write_state(
            self.directory, proposal_count=1, proposal=proposal
        )

    def apply(
        self,
        path,
        expected_head="head1",
        expected_run_id="run-1",
        expected_proposal_token=None,
    ):
        state = MODULE.load_state(path)
        if expected_proposal_token is None:
            expected_proposal_token = (state.get("proposal") or {}).get("token", "")
        MODULE.command_apply(
            SimpleNamespace(
                state=str(path),
                expected_head=expected_head,
                expected_run_id=expected_run_id,
                expected_proposal_token=expected_proposal_token,
            )
        )

    def test_rejects_a_missing_proposal_before_reading_live_metadata(self):
        path = write_state(self.directory)

        with (
            mock.patch.object(MODULE, "metadata_for") as metadata_for,
            mock.patch.object(MODULE, "update_pr") as update_pr,
            self.assertRaisesRegex(MODULE.WorkflowError, "no stored proposal"),
        ):
            self.apply(path)

        metadata_for.assert_not_called()
        update_pr.assert_not_called()

    def test_rejects_a_blank_stored_proposal_before_mutation(self):
        path = self.state_with_proposal(title=" ")

        with (
            mock.patch.object(MODULE, "metadata_for") as metadata_for,
            mock.patch.object(MODULE, "update_pr") as update_pr,
            self.assertRaisesRegex(MODULE.WorkflowError, "stored proposal is invalid"),
        ):
            self.apply(path)

        metadata_for.assert_not_called()
        update_pr.assert_not_called()

    def test_rejects_an_expected_head_that_differs_from_the_pin(self):
        path = self.state_with_proposal()

        with (
            mock.patch.object(MODULE, "metadata_for") as metadata_for,
            mock.patch.object(MODULE, "update_pr") as update_pr,
            self.assertRaisesRegex(MODULE.WorkflowError, "pinned head"),
        ):
            self.apply(path, expected_head="other")

        metadata_for.assert_not_called()
        update_pr.assert_not_called()

    def test_rejects_cross_run_ids_and_proposal_tokens(self):
        path = self.state_with_proposal()
        other = {
            "number": 1,
            "run_id": "run-2",
            "base": {
                "head_sha": "head1",
                "title": "Current title",
                "body": "Current body",
            },
            "title": "Other title",
            "body": "Other body",
        }
        other["token"] = MODULE.proposal_token_for(other)
        other_directory = self.directory / "other-run"
        other_directory.mkdir()
        other_path = write_state(
            other_directory,
            run_id="run-2",
            proposal_count=1,
            proposal=other,
        )
        other_token = MODULE.load_state(other_path)["proposal"]["token"]

        with (
            mock.patch.object(MODULE, "metadata_for") as metadata_for,
            mock.patch.object(MODULE, "update_pr") as update_pr,
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "run ID mismatch"):
                self.apply(
                    path,
                    expected_run_id="run-2",
                    expected_proposal_token=other_token,
                )
            with self.assertRaisesRegex(MODULE.WorkflowError, "proposal token mismatch"):
                self.apply(path, expected_proposal_token=other_token)

        metadata_for.assert_not_called()
        update_pr.assert_not_called()

    def test_rejects_a_moved_live_head_before_mutation(self):
        path = self.state_with_proposal()

        with (
            mock.patch.object(
                MODULE, "metadata_for", return_value=pr_metadata(head_sha="head2")
            ),
            mock.patch.object(MODULE, "update_pr") as update_pr,
            self.assertRaisesRegex(MODULE.WorkflowError, "PR head moved"),
        ):
            self.apply(path)

        update_pr.assert_not_called()
        self.assertNotIn("validated_head_sha", MODULE.load_state(path))

    def test_rejects_live_text_that_changed_after_preflight(self):
        path = self.state_with_proposal()

        with (
            mock.patch.object(
                MODULE,
                "metadata_for",
                return_value=pr_metadata(body="Externally changed"),
            ),
            mock.patch.object(MODULE, "update_pr") as update_pr,
            self.assertRaisesRegex(MODULE.WorkflowError, "no longer matches"),
        ):
            self.apply(path)

        update_pr.assert_not_called()

    def test_rechecks_exact_snapshot_immediately_before_mutation(self):
        path = self.state_with_proposal()
        changed = pr_metadata(title="Concurrent edit")

        with (
            mock.patch.object(
                MODULE, "metadata_for", side_effect=[pr_metadata(), changed]
            ) as metadata_for,
            mock.patch.object(MODULE, "update_pr") as update_pr,
            self.assertRaisesRegex(MODULE.WorkflowError, "no longer matches"),
        ):
            self.apply(path)

        self.assertEqual(metadata_for.call_count, 2)
        update_pr.assert_not_called()

    def test_rejects_a_verification_mismatch_after_update(self):
        path = self.state_with_proposal()
        verified = pr_metadata(title="Proposed title", body="Wrong body")

        with (
            mock.patch.object(
                MODULE,
                "metadata_for",
                side_effect=[pr_metadata(), pr_metadata(), verified],
            ),
            mock.patch.object(MODULE, "update_pr") as update_pr,
            self.assertRaisesRegex(MODULE.WorkflowError, "did not exactly match"),
        ):
            self.apply(path)

        update_pr.assert_called_once()
        state = MODULE.load_state(path)
        self.assertNotIn("validated_head_sha", state)
        self.assertNotIn("validation", state)

    def test_rejects_a_head_move_during_verification(self):
        path = self.state_with_proposal()
        verified = pr_metadata(
            title="Proposed title", body="Proposed body", head_sha="head2"
        )

        with (
            mock.patch.object(
                MODULE,
                "metadata_for",
                side_effect=[pr_metadata(), pr_metadata(), verified],
            ),
            mock.patch.object(MODULE, "update_pr"),
            self.assertRaisesRegex(MODULE.WorkflowError, "while applying"),
        ):
            self.apply(path)

        self.assertNotIn("validated_head_sha", MODULE.load_state(path))

    def test_applies_and_records_validation_only_after_exact_verification(self):
        path = self.state_with_proposal()
        verified = pr_metadata(title="Proposed title", body="Proposed body")

        with (
            mock.patch.object(
                MODULE,
                "metadata_for",
                side_effect=[pr_metadata(), pr_metadata(), verified],
            ),
            mock.patch.object(MODULE, "update_pr") as update_pr,
        ):
            self.apply(path)

        update_pr.assert_called_once()
        state = MODULE.load_state(path)
        self.assertEqual(state["pr"], verified)
        self.assertEqual(state["validated_head_sha"], "head1")
        self.assertEqual(state["validation"]["mode"], "applied")
        self.assertEqual(state["validation"]["proposal_number"], 1)
        self.assertEqual(state["validation"]["title"], "Proposed title")
        self.assertFalse(state["validation"]["conditional_update"])
        self.assertEqual(
            state["validation"]["precondition_strategy"],
            "two_exact_reads_immediately_before_patch",
        )
        self.assertIn("does not support conditional", state["validation"]["residual_race"])
        self.assertEqual(self.emitted[-1]["result"], "applied")

    def test_update_uses_direct_rest_patch_with_temporary_utf8_json(self):
        state = {
            "pr": pr_metadata(),
        }
        proposal = {
            "title": "Literal title",
            "body": "Literal body\n\n- item",
        }
        state_path = self.directory / "state.json"
        observed = {}

        def capture(command, **_kwargs):
            payload_path = Path(command[command.index("--input") + 1])
            observed["payload"] = json.loads(payload_path.read_text(encoding="utf-8"))
            observed["command"] = command
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(MODULE, "run", side_effect=capture):
            MODULE.update_pr(state_path, state, proposal)

        command = observed["command"]
        self.assertEqual(command[:4], ["gh", "api", "--method", "PATCH"])
        self.assertEqual(command[4], "repos/owner/repo/pulls/7")
        self.assertEqual(
            observed["payload"],
            {"title": "Literal title", "body": "Literal body\n\n- item"},
        )
        self.assertFalse(Path(command[command.index("--input") + 1]).exists())

    def test_rest_update_failure_propagates_without_validation(self):
        path = self.state_with_proposal()
        with (
            mock.patch.object(
                MODULE,
                "metadata_for",
                side_effect=[pr_metadata(), pr_metadata()],
            ),
            mock.patch.object(
                MODULE,
                "update_pr",
                side_effect=MODULE.WorkflowError("PATCH failed (422): validation"),
            ),
            self.assertRaisesRegex(MODULE.WorkflowError, "PATCH failed"),
        ):
            self.apply(path)

        state = MODULE.load_state(path)
        self.assertNotIn("validated_head_sha", state)
        self.assertNotIn("validation", state)


class NoChangeValidationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.emitted = []
        patcher = mock.patch.object(MODULE, "emit", self.emitted.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def validate(self, path, expected_head="head1", no_change=True):
        MODULE.command_validate(
            SimpleNamespace(
                state=str(path),
                expected_head=expected_head,
                expected_run_id="run-1",
                no_change=no_change,
            )
        )

    def test_requires_the_no_change_flag(self):
        path = write_state(self.directory)

        with self.assertRaisesRegex(MODULE.WorkflowError, "requires --no-change"):
            self.validate(path, no_change=False)

    def test_rejects_expected_and_live_head_mismatches(self):
        path = write_state(self.directory)
        with (
            mock.patch.object(MODULE, "metadata_for") as metadata_for,
            self.assertRaisesRegex(MODULE.WorkflowError, "pinned head"),
        ):
            self.validate(path, expected_head="other")
        metadata_for.assert_not_called()

        with (
            mock.patch.object(
                MODULE, "metadata_for", return_value=pr_metadata(head_sha="head2")
            ),
            self.assertRaisesRegex(MODULE.WorkflowError, "PR head moved"),
        ):
            self.validate(path)

    def test_requires_exact_live_title_and_body(self):
        for changes in (
            {"title": "Different title"},
            {"body": "Different body"},
        ):
            with self.subTest(changes=changes):
                path = write_state(self.directory)
                with (
                    mock.patch.object(
                        MODULE,
                        "metadata_for",
                        return_value=pr_metadata(**changes),
                    ),
                    self.assertRaisesRegex(
                        MODULE.WorkflowError, "no longer matches"
                    ),
                ):
                    self.validate(path)
                self.assertNotIn("validated_head_sha", MODULE.load_state(path))

    def test_records_validated_head_without_mutation(self):
        index_path = self.directory / "index.json"
        path = write_state(self.directory, index_path=str(index_path))

        with mock.patch.object(
            MODULE, "metadata_for", return_value=pr_metadata()
        ):
            self.validate(path)

        state = MODULE.load_state(path)
        self.assertEqual(state["validated_head_sha"], "head1")
        self.assertEqual(state["validation"]["mode"], "no_change")
        self.assertEqual(state["validation"]["title"], "Current title")
        self.assertEqual(state["validation"]["body"], "Current body")
        index = MODULE.load_state(index_path)
        self.assertEqual(index["kind"], MODULE.INDEX_KIND)
        self.assertEqual(index["validated_head_sha"], "head1")
        self.assertEqual(index["validation"]["run_id"], "run-1")
        self.assertEqual(self.emitted[-1]["result"], "validated")


class IndexLockTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.index_path = self.directory / "owner--repo--7.json"

    def state_for(self, run_id, updated_at, *, validation=None):
        state = {
            "version": MODULE.STATE_VERSION,
            "kind": MODULE.RUN_KIND,
            "created_at": updated_at,
            "updated_at": updated_at,
            "run_id": run_id,
            "pr": pr_metadata(title=f"Title {run_id}"),
        }
        if validation is not None:
            state["validated_head_sha"] = "head1"
            state["validation"] = {
                "mode": "no_change",
                "run_id": run_id,
                "validated_at": validation,
            }
        return state

    def write_lock(self, owner):
        path = MODULE.index_lock_path(self.index_path)
        path.write_text(json.dumps(owner), encoding="utf-8")
        return path

    def test_process_liveness_check_is_safe_for_the_current_process(self):
        self.assertTrue(MODULE.process_is_alive(os.getpid()))

    def test_concurrent_index_writers_preserve_every_run(self):
        states = [
            self.state_for(
                f"run-{index}",
                f"2026-01-01T00:00:{index:02d}Z",
            )
            for index in range(20)
        ]

        def update(state):
            MODULE.update_run_index(
                self.index_path,
                self.directory / f"{state['run_id']}.json",
                state,
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(update, states))

        index = MODULE.load_state(self.index_path)
        self.assertEqual(
            {item["run_id"] for item in index["runs"]},
            {state["run_id"] for state in states},
        )
        self.assertEqual(index["latest_run_id"], "run-19")
        self.assertEqual(index["pr"]["title"], "Title run-19")

    def test_older_writer_cannot_revert_newer_validation_or_current_state(self):
        newer = self.state_for(
            "newer",
            "2026-01-01T00:00:02Z",
            validation="2026-01-01T00:00:03Z",
        )
        older = self.state_for(
            "older",
            "2026-01-01T00:00:01Z",
            validation="2026-01-01T00:00:01Z",
        )

        MODULE.update_run_index(
            self.index_path, self.directory / "newer.json", newer
        )
        MODULE.update_run_index(
            self.index_path, self.directory / "older.json", older
        )

        index = MODULE.load_state(self.index_path)
        self.assertEqual(
            {item["run_id"] for item in index["runs"]}, {"newer", "older"}
        )
        self.assertEqual(index["latest_run_id"], "newer")
        self.assertEqual(index["pr"]["title"], "Title newer")
        self.assertEqual(index["validation"]["run_id"], "newer")
        self.assertEqual(index["validated_head_sha"], "head1")

    def test_reclaims_an_old_lock_only_after_owner_is_dead(self):
        stale = {
            "pid": 99999999,
            "created_at": time.time() - 100,
            "nonce": "stale",
        }
        path = self.write_lock(stale)

        with (
            mock.patch.object(MODULE, "process_is_alive", return_value=False),
            MODULE.index_lock(
                self.index_path,
                timeout_seconds=0.2,
                stale_seconds=0.01,
                poll_seconds=0.001,
            ),
        ):
            owner = MODULE.read_lock_owner(path)
            self.assertIsNotNone(owner)
            self.assertNotEqual(owner["nonce"], "stale")

        self.assertFalse(path.exists())

    def test_fresh_empty_or_malformed_lock_is_not_reclaimed(self):
        path = MODULE.index_lock_path(self.index_path)
        for content in ("", "{not-json"):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")

                with self.assertRaisesRegex(MODULE.WorkflowError, "timed out"):
                    with MODULE.index_lock(
                        self.index_path,
                        timeout_seconds=0.02,
                        stale_seconds=60,
                        poll_seconds=0.002,
                    ):
                        self.fail("fresh malformed lock should not be reclaimed")

                self.assertEqual(path.read_text(encoding="utf-8"), content)
                path.unlink()

    def test_aged_empty_or_malformed_lock_is_reclaimed(self):
        path = MODULE.index_lock_path(self.index_path)
        for content in ("", "{not-json"):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                old = time.time() - 100
                os.utime(path, (old, old))

                with MODULE.index_lock(
                    self.index_path,
                    timeout_seconds=0.2,
                    stale_seconds=0.01,
                    poll_seconds=0.001,
                ):
                    owner = MODULE.read_lock_owner(path)
                    self.assertIsNotNone(owner)
                    self.assertNotEqual(owner["nonce"], content)

                self.assertFalse(path.exists())

    def test_times_out_without_deleting_a_live_owner_lock(self):
        owner = {
            "pid": os.getpid(),
            "created_at": time.time() - 100,
            "nonce": "live",
        }
        path = self.write_lock(owner)

        with self.assertRaisesRegex(MODULE.WorkflowError, "timed out"):
            with MODULE.index_lock(
                self.index_path,
                timeout_seconds=0.02,
                stale_seconds=0.001,
                poll_seconds=0.002,
            ):
                self.fail("lock should not have been acquired")

        self.assertEqual(MODULE.read_lock_owner(path), owner)

    def test_guard_wait_is_bounded_while_another_writer_is_live(self):
        entered = threading.Event()
        release = threading.Event()

        def hold_lock():
            with MODULE.index_lock(self.index_path):
                entered.set()
                release.wait(2)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(hold_lock)
            self.assertTrue(entered.wait(1))
            path = MODULE.index_lock_path(self.index_path)
            original_owner = MODULE.read_lock_owner(path)
            self.assertIsNotNone(original_owner)
            with self.assertRaisesRegex(MODULE.WorkflowError, "index guard"):
                with MODULE.index_lock(
                    self.index_path,
                    timeout_seconds=0.02,
                    poll_seconds=0.002,
                ):
                    self.fail("guard should not have been acquired")
            self.assertEqual(MODULE.read_lock_owner(path), original_owner)
            release.set()
            future.result(timeout=2)

    def test_release_does_not_delete_a_different_owners_lock(self):
        path = MODULE.index_lock_path(self.index_path)
        replacement = {
            "pid": os.getpid(),
            "created_at": time.time(),
            "nonce": "replacement",
        }

        with self.assertRaisesRegex(MODULE.WorkflowError, "not owned"):
            with MODULE.index_lock(self.index_path):
                path.write_text(json.dumps(replacement), encoding="utf-8")

        self.assertEqual(MODULE.read_lock_owner(path), replacement)


class StatusAndCleanupTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.emitted = []
        patcher = mock.patch.object(MODULE, "emit", self.emitted.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_status_reports_explicit_state(self):
        path = write_state(self.directory, proposal_count=2)

        MODULE.command_status(
            SimpleNamespace(state=str(path), current=False, repo_root=None)
        )

        result = self.emitted[-1]
        self.assertEqual(result["result"], "ready")
        self.assertEqual(result["pr"]["number"], 7)
        self.assertEqual(result["proposal_count"], 2)
        self.assertIsNone(result["validated_head_sha"])

    def test_status_reports_no_state_for_the_current_branch_pr(self):
        target = MODULE.parse_target("owner/repo#7")
        missing = self.directory / "missing.json"

        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(
                MODULE, "resolve_repo_root", return_value=self.directory
            ),
            mock.patch.object(MODULE, "current_pr_target", return_value=target),
            mock.patch.object(
                MODULE, "default_state_path", return_value=missing
            ),
        ):
            MODULE.command_status(
                SimpleNamespace(state=None, current=True, repo_root=None)
            )

        result = self.emitted[-1]
        self.assertEqual(result["result"], "no_state")
        self.assertEqual(result["pr"]["number"], 7)
        self.assertEqual(result["state"], str(missing))

    def test_current_status_reads_the_stable_index(self):
        target = MODULE.parse_target("owner/repo#7")
        index_path = self.directory / "index.json"
        MODULE.save_state(
            index_path,
            {
                "version": MODULE.STATE_VERSION,
                "kind": MODULE.INDEX_KIND,
                "created_at": "2026-01-01T00:00:00Z",
                "pr": pr_metadata(),
                "runs": [
                    {
                        "run_id": "run-1",
                        "state": str(self.directory / "run.json"),
                    }
                ],
                "latest_run_id": "run-1",
                "latest_state": str(self.directory / "run.json"),
                "validated_head_sha": "head1",
            },
        )
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(
                MODULE, "resolve_repo_root", return_value=self.directory
            ),
            mock.patch.object(MODULE, "current_pr_target", return_value=target),
            mock.patch.object(
                MODULE, "default_state_path", return_value=index_path
            ),
        ):
            MODULE.command_status(
                SimpleNamespace(state=None, current=True, repo_root=None)
            )

        result = self.emitted[-1]
        self.assertEqual(result["kind"], MODULE.INDEX_KIND)
        self.assertEqual(result["latest_run_id"], "run-1")
        self.assertEqual(result["validated_head_sha"], "head1")

    def test_cleanup_removes_valid_state(self):
        path = write_state(self.directory)

        MODULE.command_cleanup(SimpleNamespace(state=str(path)))

        self.assertFalse(path.exists())
        self.assertEqual(self.emitted[-1]["result"], "cleaned_up")


class ParserShapeTest(unittest.TestCase):
    def setUp(self):
        self.parser = MODULE.build_parser()

    def test_parses_every_command_shape(self):
        cases = (
            (
                ["preflight", "owner/repo#7", "--repo-root", "repo", "--state", "state"],
                "command_preflight",
            ),
            (
                [
                    "propose",
                    "--state",
                    "state",
                    "--expected-run-id",
                    "run",
                    "--title",
                    "Title",
                    "--body-file",
                    "body",
                ],
                "command_propose",
            ),
            (
                [
                    "apply",
                    "--state",
                    "state",
                    "--expected-head",
                    "abc",
                    "--expected-run-id",
                    "run",
                    "--expected-proposal-token",
                    "token",
                ],
                "command_apply",
            ),
            (
                [
                    "validate",
                    "--state",
                    "state",
                    "--expected-head",
                    "abc",
                    "--expected-run-id",
                    "run",
                    "--no-change",
                ],
                "command_validate",
            ),
            (
                ["status", "--current", "--repo-root", "repo"],
                "command_status",
            ),
            (["status", "--state", "state"], "command_status"),
            (["cleanup", "--state", "state"], "command_cleanup"),
        )
        for arguments, function_name in cases:
            with self.subTest(arguments=arguments):
                parsed = self.parser.parse_args(arguments)
                self.assertEqual(parsed.function.__name__, function_name)

    def test_requires_exactly_one_status_source(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["status"])
        with self.assertRaises(SystemExit):
            self.parser.parse_args(
                ["status", "--state", "state", "--current"]
            )

    def test_validate_requires_no_change(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(
                [
                    "validate",
                    "--state",
                    "state",
                    "--expected-head",
                    "abc",
                    "--expected-run-id",
                    "run",
                ]
            )


if __name__ == "__main__":
    unittest.main()
