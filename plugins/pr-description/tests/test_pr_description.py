from concurrent.futures import ThreadPoolExecutor
import importlib.util
import inspect
import io
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
SCRIPT = ROOT / "scripts" / "pr_description.py"
AGENT = ROOT / "agents" / "pr-description.agent.md"
PLUGIN = ROOT / "plugin.json"
MARKETPLACE = ROOT.parents[1] / ".github" / "plugin" / "marketplace.json"
SPEC = importlib.util.spec_from_file_location("pr_description", SCRIPT)
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

    def test_is_manually_selected_and_user_invocable(self):
        self.assertIn("user-invocable: true", self.instructions)
        self.assertIn("disable-model-invocation: true", self.instructions)
        self.assertIn("The user selects this agent by hand", self.instructions)

    def test_the_no_target_path_is_not_offered_to_a_detached_worktree(self):
        """A pipeline runs this stage detached, so omitting the target traps it.

        `preflight` resolves the pull request from the checked-out branch, and a
        detached worktree names none.
        """
        self.assertIn(
            "works only while it is attached to one", self.instructions
        )
        self.assertIn(
            "pass the pull request explicitly whenever you have it",
            self.instructions,
        )
        self.assertIn(
            "omit only from a worktree attached to the PR's branch",
            self.instructions,
        )

    def test_renames_once_after_preflight(self):
        self.assertIn(
            "tools: [read, search, execute, skill, todo, rename_session]",
            self.instructions,
        )
        self.assertIn("After preflight succeeds, call `rename_session` exactly once", self.instructions)
        self.assertIn("`PR Description: <number> - <title>`", self.instructions)
        self.assertIn("never rename again during this run", self.instructions)

    def test_unslops_every_replacement_before_automatic_apply(self):
        self.assertIn(
            "invoke the globally installed `unslop` skill with the `skill` tool",
            self.instructions,
        )
        self.assertIn(
            "apply its process to the complete candidate title and body",
            self.instructions,
        )
        self.assertIn(        "Repeat this before every new proposal", self.instructions)
        self.assertIn(
            "do not run `unslop` again or change either value before apply",
            self.instructions,
        )
        self.assertIn(
            "needs another complete display before apply",
            self.instructions,
        )

    def test_always_shows_current_text_before_evaluating_or_proposing(self):
        self.assertIn(
            "show the current title and current description before your evaluation and "
            "before any proposal",
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
            "Never wrap a displayed title or description in a fenced code block or an "
            "inline code span",
            self.instructions,
        )
        self.assertIn(
            "Never put a fenced code block, an inline code span, or any other verbatim "
            "wrapper around the title or the description",
            self.instructions,
        )
        self.assertIn(
            "Render the description as ordinary Markdown so the interface wraps it",
            self.instructions,
        )
        self.assertIn(
            "Never summarize, normalize, reflow, hard wrap, re-indent, or quietly "
            "repair either value",
            self.instructions,
        )
        self.assertIn(
            "a bold label on its own line, then a blank line, then the value "
            "as a blockquote",
            self.instructions,
        )
        self.assertIn(
            "Prefix every line of the value with `> `, including a blank line inside a "
            "description",
            self.instructions,
        )
        self.assertIn(
            "The `> ` prefix is presentation only and is never part of the "
            "stored value",
            self.instructions,
        )
        self.assertIn(
            "Do not add horizontal rules around it. The blockquote is the boundary",
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

    def test_inspects_the_pinned_body_for_actual_newline_characters(self):
        self.assertIn(
            "Treat the pinned preflight `body` as the exact stored string",
            self.instructions,
        )
        self.assertIn(
            "JSON escaping, terminal wrapping, and renderer wrapping do not prove "
            "that the value contains line breaks",
            self.instructions,
        )
        self.assertIn(
            "look at the decoded string for real `\\r` and `\\n` characters",
            self.instructions,
        )
        self.assertIn(
            "read only the pinned run state's body with a local JSON parser",
            self.instructions,
        )
        self.assertIn(
            "Do not issue a separate `gh pr view`, do not normalize the string, and do "
            "not infer a boundary that is missing",
            self.instructions,
        )
        self.assertIn(
            "Look at the decoded `body` for its real newline characters before you "
            "judge its structure",
            self.instructions,
        )
        self.assertIn(
            "never trust how serialized JSON looks",
            self.instructions,
        )

    def test_summarizes_how_a_proposal_differs_from_the_current_text(self):
        self.assertIn("## Summarizing What Changed", self.instructions)
        self.assertIn(
            "Immediately after you display a proposed title and description, and before "
            "you apply it, add a `**What changed**` summary",
            self.instructions,
        )
        self.assertIn(
            "Describe only the differences. Never restate the full proposed title or "
            "body",
            self.instructions,
        )
        self.assertIn(
            "Repeat the summary for every revision",
            self.instructions,
        )
        self.assertIn(
            "add the `**What changed**` summary from \"Summarizing What "
            "Changed\"",
            self.instructions,
        )

    def test_immediately_evaluates_and_recommends_a_decision(self):
        self.assertIn(
            "Evaluate the current text against the diff at once, for clarity, "
            "concision, consistency, and scope",
            self.instructions,
        )
        self.assertIn(
            'Never insert a neutral "does this look good?" turn',
            self.instructions,
        )
        self.assertIn(
            "Keep the current title and description only when they are already "
            "essentially ideal",
            self.instructions,
        )
        self.assertIn(
            '"Good enough," broadly accurate, or easy to improve does not meet this '
            "threshold",
            self.instructions,
        )
        self.assertIn(
            "If a fresh draft would be meaningfully clearer, shorter, more complete, "
            "or easier to scan, replace the current text",
            self.instructions,
        )
        self.assertIn(
            "Do not ask whether the current text looks good",
            self.instructions,
        )

    def test_redrafts_replacements_from_the_authoritative_diff(self):
        self.assertIn(
            "Build every replacement from scratch from the authoritative diff",
            self.instructions,
        )
        self.assertIn(
            "Do not incrementally edit the current body, preserve its outline, or "
            "treat its wording as the draft you must improve",
            self.instructions,
        )
        self.assertIn(
            "Independently choose the shortest scan-friendly structure and wording",
            self.instructions,
        )
        self.assertIn(
            "Retain an essential fact or exact example from the current text only "
            "when the diff supports it and the fresh proposal needs it",
            self.instructions,
        )

    def test_applies_without_user_approval(self):
        self.assertIn(
            "Never ask for approval or wait for another user turn",
            self.instructions,
        )
        self.assertIn(
            "manual selection of this agent authorizes it to keep ideal text or apply "
            "the replacement it judges best",
            self.instructions,
        )
        self.assertIn(
            "then call `propose` and `apply` immediately",
            self.instructions,
        )

    def test_automatically_validates_ideal_text(self):
        self.assertIn(
            "If the current title and description are essentially ideal",
            self.instructions,
        )
        self.assertIn(
            "Run `validate --state <path> --expected-head <head_sha> "
            "--expected-run-id <run_id> --no-change` immediately",
            self.instructions,
        )
        self.assertIn(
            "If validation reports that the head or text changed",
            self.instructions,
        )
        self.assertIn(
            "continue with \"Metadata Changes Before Apply\"",
            self.instructions,
        )
        self.assertIn(
            "Do not ask whether the current text looks good",
            self.instructions,
        )
        self.assertIn(
            "whether the proposal is approved",
            self.instructions,
        )

    def test_validates_ideal_current_text_without_mutation(self):
        self.assertIn(
            "`validate --state <path> --expected-head <head_sha> "
            "--expected-run-id <run_id> --no-change`",
            self.instructions,
        )
        self.assertIn("Do not run `propose` or `apply`", self.instructions)

    def test_documents_description_style_and_diff_source(self):
        self.assertIn(
            "`gh pr diff <pr.url> --repo <pr.repo_name>`",
            self.instructions,
        )
        self.assertIn(
            "If the command output is too large for one tool read and the tool "
            "saves it to a file, read the authoritative diff from that saved file",
            self.instructions,
        )
        for forbidden_header in ("`Summary`", "`Details`", "`Testing`"):
            self.assertIn(forbidden_header, self.instructions)
        self.assertIn("Do not include validation lists", self.instructions)
        self.assertIn(
            "Paragraphs should usually contain one or two short sentences and cover "
            "one idea", self.instructions
        )
        self.assertIn(
            "Readers gloss over large blocks",
            self.instructions,
        )
        self.assertIn(
            "Assume the first draft is at least twice as long as it needs to be",
            self.instructions,
        )
        self.assertIn(
            "Cut repeated context, generic transitions, boilerplate, implementation "
            "narration, obvious diff details, and validation logs",
            self.instructions,
        )
        self.assertIn(
            "Prefer blank space, concise bullets, and tiny code or configuration examples",
            self.instructions,
        )
        self.assertIn("Never hard wrap prose", self.instructions)

    def test_prioritizes_user_facing_examples_and_skimmable_structure(self):
        self.assertIn(
            "Open with a short paragraph that has no heading and states the "
            "user-visible outcome",
            self.instructions,
        )
        self.assertIn(
            "When the pull request changes configuration, put short before-and-after "
            "configuration examples right after the opening paragraph",
            self.instructions,
        )
        self.assertIn(
            "Use the real keys and representative values for each configuration "
            "surface that differs in a way that matters",
            self.instructions,
        )
        self.assertIn(
            "When the pull request changes one, show a concrete usage example early "
            "in the body",
            self.instructions,
        )
        self.assertIn(
            "use before-and-after examples when callers have to change how they call it",
            self.instructions,
        )
        self.assertIn(
            "give each substantial idea its own descriptive heading so readers can "
            "scan the explanation",
            self.instructions,
        )
        self.assertIn(
            "Do not add a heading to a short or single-idea body",
            self.instructions,
        )
        self.assertIn(
            "Do not turn the body into a full change log",
            self.instructions,
        )

    def test_restarts_automatically_on_metadata_change_and_uses_external_body_file(self):
        self.assertIn(
            "## Metadata Changes Before Apply", self.instructions
        )
        self.assertIn(
            "UTF-8 to a body file outside the repository", self.instructions
        )
        self.assertIn(
            "the helper turns CRLF and CR into LF", self.instructions
        )
        self.assertIn(
            "Never read the helper's source to choose a line ending",
            self.instructions,
        )
        self.assertIn(
            "include `live_head`, `live_title`, and `live_body` in a head-mismatch "
            "error",
            self.instructions,
        )
        self.assertIn(
            "Run a fresh `preflight` for the same pull request",
            self.instructions,
        )
        self.assertIn(
            "Display the fresh current title and description",
            self.instructions,
        )
        self.assertIn(
            "If the fresh text is ideal, run `validate --no-change`",
            self.instructions,
        )
        self.assertIn(
            "Never reuse a stale proposal", self.instructions
        )

    def test_documents_run_capabilities_and_residual_update_race(self):
        self.assertIn(
            "returned `run_id` and `proposal_token` as capabilities",
            self.instructions,
        )
        self.assertIn(
            "does not support conditional unsafe requests", self.instructions
        )
        self.assertIn("Never call this an atomic compare-and-swap", self.instructions)
        self.assertIn("twice immediately before a direct REST `PATCH`", self.instructions)

    def test_closes_every_run_with_a_categorized_retrospective(self):
        self.assertIn(
            "## PR Description Agent Retrospective", self.instructions
        )
        self.assertIn(
            "**PR Description Agent Retrospective**", self.instructions
        )
        self.assertIn("Emit exactly one terminal response", self.instructions)
        self.assertIn("must be the very last block", self.instructions)
        self.assertIn("stop immediately after its last list item", self.instructions)
        self.assertIn(
            "never emit a short final response and then a fuller report",
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
            "discarded a proposal, a helper error, and a run that stops early",
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
            "Report only friction you actually hit in this run", self.instructions
        )
        self.assertIn("The retrospective is advice, and it belongs in chat only", self.instructions)
        self.assertIn(
            "never fold it into a pull request title or description", self.instructions
        )
        self.assertIn(
            "leave the label out entirely when there is nothing to report", self.instructions
        )
        self.assertIn(
            "never replaces, reorders, or alters the required final response",
            self.instructions,
        )
        self.assertIn("never send a recap after the retrospective", self.instructions)

    def test_sends_the_terminal_response_as_the_last_message(self):
        self.assertIn(
            "The terminal response is the run's last message", self.instructions
        )
        self.assertIn(
            "send it in a message that calls no tool, and never follow it with a "
            "recap or a second summary",
            self.instructions,
        )
        self.assertIn(
            "Emit exactly one terminal response and make it the last message of the "
            "run",
            self.instructions,
        )
        self.assertIn("Finish every tool call the run needs", self.instructions)
        self.assertIn(
            "then send the whole thing in one message that calls no tool",
            self.instructions,
        )
        self.assertIn(
            "attach any part of it to a message that also calls a tool",
            self.instructions,
        )
        self.assertIn("Once you send it the run is over", self.instructions)
        self.assertIn(
            "never send another message because a tool result, a reminder, or a turn "
            "boundary invites one",
            self.instructions,
        )
        self.assertIn(
            "never open with a narrative recap of what the run did", self.instructions
        )
        self.assertIn(
            "render the `Validated:`, `Applied:`, and `PR:` lines at most once each",
            self.instructions,
        )

    def test_manifest_and_marketplace_versions_match(self):
        plugin = json.loads(PLUGIN.read_text(encoding="utf-8"))
        marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
        entry = next(
            item for item in marketplace["plugins"] if item["name"] == plugin["name"]
        )
        self.assertEqual(plugin["version"], "1.0.26")
        self.assertEqual(entry["version"], plugin["version"])
        self.assertEqual(entry["source"], "./plugins/pr-description")


class TargetParsingTest(unittest.TestCase):
    def test_uses_the_renamed_pr_flight_state_directory(self):
        target = MODULE.parse_target("owner/repo#7")

        with mock.patch("pathlib.Path.home", return_value=Path("home")):
            path = MODULE.default_state_path(target)

        self.assertEqual(
            path,
            Path("home")
            / ".copilot"
            / "run"
            / "pr-description"
            / "owner--repo--7.json",
        )

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

    def test_the_refusal_names_the_way_out(self):
        """A message that names only the fault leaves the caller stuck.

        Detached HEAD is the normal state for a pipeline stage, so the refusal
        has to say what to do instead.
        """
        with mock.patch.object(MODULE, "git", return_value=""):
            with self.assertRaises(MODULE.WorkflowError) as raised:
                MODULE.current_pr_target(Path("repo"))

        self.assertIn("pass the pull request explicitly", str(raised.exception))

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


class SharedStateBackendTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)

    def process(self, returncode=0, stdout="", stderr=""):
        return SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr
        )

    def response_for(self, document, sha="blob-sha"):
        content = MODULE.shared_state_bytes(document)
        return self.process(
            stdout=json.dumps(
                {
                    "content": MODULE.base64.b64encode(content).decode("ascii"),
                    "sha": sha,
                }
            )
        )

    def test_404_starts_with_an_empty_document(self):
        not_found = self.process(returncode=1, stderr="gh: Not Found (HTTP 404)")
        with mock.patch.object(MODULE, "run", return_value=not_found):
            document, content, sha = MODULE.read_shared_state(
                "state/repo", "owner/repo"
            )

        self.assertEqual(
            document,
            {
                "version": 1,
                "repository": "owner/repo",
                "pull_requests": {},
            },
        )
        self.assertEqual(content, b"")
        self.assertIsNone(sha)

    def test_publish_from_404_creates_description_without_a_sha(self):
        not_found = self.process(returncode=1, stderr="gh: Not Found (HTTP 404)")
        with (
            mock.patch.object(
                MODULE, "resolve_shared_state_repo", return_value="state/repo"
            ),
            mock.patch.object(
                MODULE, "run", side_effect=[not_found, self.process()]
            ) as run,
        ):
            MODULE.publish_shared_state(
                {"repo_name": "owner/repo", "number": 7},
                section="description",
                field="validated_head_sha",
                value="head1",
                updated_at="2026-01-02T00:00:00Z",
            )

        payload = json.loads(run.call_args_list[1].kwargs["input_text"])
        self.assertNotIn("sha", payload)
        published = json.loads(
            MODULE.base64.b64decode(payload["content"]).decode("utf-8")
        )
        self.assertEqual(
            published["pull_requests"]["7"]["description"],
            {
                "validated_head_sha": "head1",
                "updated_at": "2026-01-02T00:00:00Z",
            },
        )

    def test_conflict_reloads_and_retries_without_clobbering(self):
        first = {
            "version": 1,
            "repository": "owner/repo",
            "pull_requests": {"8": {"first": True}},
        }
        second = {
            "version": 1,
            "repository": "owner/repo",
            "pull_requests": {
                "8": {"first": True},
                "9": {"concurrent": True},
            },
        }
        conflict = self.process(
            returncode=1, stderr="gh: conflict (HTTP 409)"
        )
        with (
            mock.patch.object(
                MODULE, "resolve_shared_state_repo", return_value="state/repo"
            ),
            mock.patch.object(
                MODULE,
                "run",
                side_effect=[
                    self.response_for(first, "sha-1"),
                    conflict,
                    self.response_for(second, "sha-2"),
                    self.process(),
                ],
            ) as run,
        ):
            MODULE.publish_shared_state(
                {"repo_name": "owner/repo", "number": 7},
                section="description",
                field="validated_head_sha",
                value="head1",
                updated_at="2026-01-02T00:00:00Z",
            )

        payload = json.loads(run.call_args_list[3].kwargs["input_text"])
        published = json.loads(
            MODULE.base64.b64decode(payload["content"]).decode("utf-8")
        )
        self.assertEqual(payload["sha"], "sha-2")
        self.assertEqual(
            published["pull_requests"]["9"], {"concurrent": True}
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
        environment = mock.patch.dict(
            MODULE.os.environ, {MODULE.SHARED_STATE_ENV: ""}, clear=False
        )
        environment.start()
        self.addCleanup(environment.stop)

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

    def test_propose_normalizes_crlf_and_reports_the_newline_convention(self):
        path = write_state(self.directory)
        body_path = self.directory / "body.md"
        body_path.write_bytes(b"First paragraph.\r\n\r\n- One\r- Two")

        MODULE.command_propose(
            SimpleNamespace(
                state=str(path),
                expected_run_id="run-1",
                title="Title",
                body_file=str(body_path),
            )
        )

        state = MODULE.load_state(path)
        self.assertEqual(
            state["proposal"]["body"], "First paragraph.\n\n- One\n- Two"
        )
        self.assertEqual(self.emitted[-1]["body_newline"], "lf")
        self.assertTrue(self.emitted[-1]["body_normalized"])

    def test_propose_reports_an_lf_body_as_unnormalized(self):
        path = write_state(self.directory)
        body_path = self.directory / "body.md"
        body_path.write_bytes(b"First paragraph.\n\n- One\n- Two")

        MODULE.command_propose(
            SimpleNamespace(
                state=str(path),
                expected_run_id="run-1",
                title="Title",
                body_file=str(body_path),
            )
        )

        self.assertEqual(self.emitted[-1]["body_newline"], "lf")
        self.assertFalse(self.emitted[-1]["body_normalized"])

    def test_propose_rejects_a_body_file_that_is_not_utf8(self):
        path = write_state(self.directory)
        body_path = self.directory / "body.md"
        body_path.write_bytes(b"Body \xff")

        with self.assertRaisesRegex(MODULE.WorkflowError, "not valid UTF-8"):
            MODULE.command_propose(
                SimpleNamespace(
                    state=str(path),
                    expected_run_id="run-1",
                    title="Title",
                    body_file=str(body_path),
                )
            )

        self.assertEqual(MODULE.load_state(path)["proposal_count"], 0)

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

        with mock.patch.object(
            MODULE,
            "metadata_for",
            return_value=pr_metadata(
                head_sha="head2", title="Live title", body="Live body"
            ),
        ), mock.patch.object(MODULE, "update_pr") as update_pr:
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "PR head moved"
            ) as raised:
                self.apply(path)

        update_pr.assert_not_called()
        self.assertEqual(
            raised.exception.details,
            {
                "expected_head": "head1",
                "live_head": "head2",
                "live_title": "Live title",
                "live_body": "Live body",
            },
        )
        self.assertNotIn("validated_head_sha", MODULE.load_state(path))

    def test_main_emits_live_metadata_from_a_head_mismatch(self):
        error = MODULE.WorkflowError(
            "PR head moved",
            details={
                "expected_head": "head1",
                "live_head": "head2",
                "live_title": "Live title",
                "live_body": "Live body",
            },
        )
        parser = mock.Mock()
        parser.parse_args.return_value = SimpleNamespace(
            function=mock.Mock(side_effect=error)
        )

        with (
            mock.patch.object(MODULE, "build_parser", return_value=parser),
            mock.patch.object(MODULE, "emit") as emit,
        ):
            result = MODULE.main()

        self.assertEqual(result, 1)
        emit.assert_called_once_with(
            {
                "result": "error",
                "error": "PR head moved",
                "expected_head": "head1",
                "live_head": "head2",
                "live_title": "Live title",
                "live_body": "Live body",
            }
        )

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
        environment = mock.patch.dict(
            MODULE.os.environ, {MODULE.SHARED_STATE_ENV: ""}, clear=False
        )
        environment.start()
        self.addCleanup(environment.stop)

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

        with (
            mock.patch.object(
                MODULE, "metadata_for", return_value=pr_metadata()
            ),
            mock.patch.object(MODULE, "publish_shared_state") as publish,
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
        publish.assert_called_once_with(
            index["pr"],
            section="description",
            field="validated_head_sha",
            value="head1",
            updated_at=index["updated_at"],
        )

    def test_publish_failure_does_not_fail_validation(self):
        index_path = self.directory / "index.json"
        path = write_state(self.directory, index_path=str(index_path))
        stderr = io.StringIO()
        with (
            mock.patch.object(
                MODULE, "metadata_for", return_value=pr_metadata()
            ),
            mock.patch.dict(
                MODULE.os.environ,
                {MODULE.SHARED_STATE_ENV: "state/repo"},
                clear=False,
            ),
            mock.patch.object(
                MODULE,
                "read_shared_state",
                side_effect=MODULE.WorkflowError("state repository unavailable"),
            ),
            mock.patch.object(MODULE.sys, "stderr", stderr),
        ):
            self.validate(path)

        self.assertEqual(MODULE.load_state(path)["validated_head_sha"], "head1")
        self.assertEqual(
            MODULE.load_state(index_path)["validated_head_sha"], "head1"
        )
        self.assertEqual(self.emitted[-1]["result"], "validated")
        self.assertIn("state repository unavailable", stderr.getvalue())


class IndexLockTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.index_path = self.directory / "owner--repo--7.json"
        environment = mock.patch.dict(
            MODULE.os.environ, {MODULE.SHARED_STATE_ENV: ""}, clear=False
        )
        environment.start()
        self.addCleanup(environment.stop)

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

    def test_first_unvalidated_index_publishes_null_for_cross_machine_retraction(self):
        state = self.state_for("new", "2026-01-01T00:00:01Z")
        with mock.patch.object(MODULE, "publish_shared_state") as publish:
            MODULE.update_run_index(
                self.index_path, self.directory / "new.json", state
            )

        index = MODULE.load_state(self.index_path)
        publish.assert_called_once_with(
            index["pr"],
            section="description",
            field="validated_head_sha",
            value=None,
            updated_at=index["updated_at"],
        )

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

    def test_newer_unvalidated_run_publishes_null_retraction(self):
        validated = self.state_for(
            "validated",
            "2026-01-01T00:00:01Z",
            validation="2026-01-01T00:00:01Z",
        )
        unvalidated = self.state_for(
            "unvalidated", "2026-01-01T00:00:02Z"
        )
        with mock.patch.object(MODULE, "publish_shared_state") as publish:
            MODULE.update_run_index(
                self.index_path, self.directory / "validated.json", validated
            )
            MODULE.update_run_index(
                self.index_path, self.directory / "unvalidated.json", unvalidated
            )

        index = MODULE.load_state(self.index_path)
        self.assertIsNone(index["validated_head_sha"])
        self.assertNotIn("validation", index)
        self.assertEqual(publish.call_count, 2)
        publish.assert_called_with(
            index["pr"],
            section="description",
            field="validated_head_sha",
            value=None,
            updated_at=index["updated_at"],
        )

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

    def test_status_reports_when_the_helper_last_wrote_its_state(self):
        """The only signal a reader has for telling working from wedged.

        Every write stamps it, so a stamp minutes old and a stamp an hour old
        are different answers to the question a person actually asks.
        """
        path = write_state(self.directory)
        stamp = MODULE.load_state(path)["updated_at"]

        MODULE.command_status(
            SimpleNamespace(state=str(path), current=False, repo_root=None)
        )

        self.assertEqual(stamp, self.emitted[-1]["last_helper_activity"])

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


class StageOutcomeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.emitted = []
        patcher = mock.patch.object(MODULE, "emit", self.emitted.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_status(self, **overrides):
        path = write_state(self.directory, **overrides)
        MODULE.command_status(
            SimpleNamespace(state=str(path), current=False, repo_root=None)
        )
        return self.emitted[-1], MODULE.load_state(path)

    def index_status(self, **overrides):
        state = {
            "version": MODULE.STATE_VERSION,
            "kind": MODULE.INDEX_KIND,
            "created_at": "2026-01-01T00:00:00Z",
            "pr": pr_metadata(),
            "runs": [{"run_id": "run-1", "state": str(self.directory / "run.json")}],
            "latest_run_id": "run-1",
            "latest_state": str(self.directory / "run.json"),
        }
        state.update(overrides)
        path = self.directory / "index.json"
        MODULE.save_state(path, state)
        MODULE.command_status(
            SimpleNamespace(state=str(path), current=False, repo_root=None)
        )
        return self.emitted[-1], MODULE.load_state(path)

    def marker_of(self, state):
        """Read the validated-at-head marker the way an orchestrator reads it.

        This deliberately repeats the rule rather than calling the helper, so a
        change that lets `stage_outcome` claim `cleared` on its own still fails.
        """

        value = state.get("validated_head_sha")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def test_an_applied_description_cleared(self):
        envelope, _ = self.run_status(
            validated_head_sha="head1",
            validation={"mode": "applied", "head_sha": "head1"},
        )

        self.assertEqual(envelope["result"], "ready")
        self.assertEqual(envelope["stage_outcome"], "cleared")

    def test_a_description_confirmed_unchanged_cleared(self):
        envelope, _ = self.run_status(
            validated_head_sha="head1",
            validation={"mode": "no_change", "head_sha": "head1"},
        )

        self.assertEqual(envelope["stage_outcome"], "cleared")

    def test_a_run_that_settled_nothing_reports_no_outcome(self):
        pinned, _ = self.run_status()
        proposed, _ = self.run_status(
            proposal_count=1,
            proposal={"number": 1, "run_id": "run-1", "title": "New title"},
        )

        self.assertNotIn("stage_outcome", pinned)
        self.assertNotIn("stage_outcome", proposed)

    def test_the_outcome_can_say_that_it_has_no_answer(self):
        """A return type with no absence value has to invent an ending."""

        annotation = inspect.signature(MODULE.stage_outcome).return_annotation

        self.assertEqual(str(annotation).replace("'", ""), "str | None")
        self.assertIsNone(MODULE.stage_outcome({}))

    def test_the_index_reports_the_same_ending(self):
        cleared, _ = self.index_status(validated_head_sha="head1")
        pending, _ = self.index_status()

        self.assertEqual(cleared["result"], "ready")
        self.assertEqual(cleared["stage_outcome"], "cleared")
        self.assertNotIn("stage_outcome", pending)

    def test_a_state_that_holds_no_run_reports_no_outcome(self):
        target = MODULE.parse_target("owner/repo#7")

        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(
                MODULE, "resolve_repo_root", return_value=self.directory
            ),
            mock.patch.object(MODULE, "current_pr_target", return_value=target),
            mock.patch.object(
                MODULE,
                "default_state_path",
                return_value=self.directory / "missing.json",
            ),
        ):
            MODULE.command_status(
                SimpleNamespace(state=None, current=True, repo_root=None)
            )

        result = self.emitted[-1]
        self.assertEqual(result["result"], "no_state")
        self.assertNotIn("stage_outcome", result)

    def test_cleared_never_outruns_the_recorded_validated_head(self):
        runs = [
            {},
            {"validated_head_sha": None},
            {"validated_head_sha": "   "},
            {"proposal": {"number": 1, "run_id": "run-1"}, "proposal_count": 1},
            {
                "validation": {"mode": "applied", "head_sha": "head1"},
                "proposal_count": 1,
            },
            {"validated_head_sha": "head1"},
            {
                "validated_head_sha": "head1",
                "validation": {"mode": "no_change", "head_sha": "head1"},
            },
        ]

        for overrides in runs:
            with self.subTest(kind="run", overrides=overrides):
                envelope, state = self.run_status(**overrides)
                self.assert_outcome_tracks_the_marker(envelope, state)
            with self.subTest(kind="index", overrides=overrides):
                envelope, state = self.index_status(**overrides)
                self.assert_outcome_tracks_the_marker(envelope, state)

    def assert_outcome_tracks_the_marker(self, envelope, state):
        marker = self.marker_of(state)
        cleared = marker is not None
        self.assertEqual(envelope.get("stage_outcome") == "cleared", cleared)
        if cleared:
            self.assertEqual(envelope["validated_head_sha"], marker)
        else:
            self.assertNotIn("stage_outcome", envelope)


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
