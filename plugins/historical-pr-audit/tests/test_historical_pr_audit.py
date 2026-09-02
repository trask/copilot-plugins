import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "historical_pr_audit.py"
AGENT = Path(__file__).parents[1] / "agents" / "historical-pr-audit.agent.md"
SPEC = importlib.util.spec_from_file_location("historical_pr_audit", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 import os
-value = 1
+value = 2
+extra = 3
 print(value)
"""

CUMULATIVE_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,5 @@
 import os
-value = 1
+value = 2
+extra = 3
+audited = 4
 print(value)
"""

PR_METADATA = {
    "number": 7,
    "title": "Add a thing",
    "pr_url": "https://github.com/owner/repo/pull/7",
    "repo_name": "owner/repo",
    "state": "MERGED",
    "merged_at": "2024-05-01T00:00:00Z",
    "merge_commit": "merge1",
    "upstream_owner": "owner",
    "upstream_repo": "repo",
    "head_owner": "fork",
    "head_repo": "repo",
    "head_branch": "feature",
    "head_sha": "head1",
    "base_branch": "main",
    "base_sha": "base1",
    "commits": [{"sha": "commit1", "message": "Change app"}],
}

CONTEXT = {
    "body": "Original description",
    "closing_issues": [{"number": 3}],
    "issue_comments": [{"body": "looks good"}],
    "reviews": [{"body": "approved"}],
    "review_threads": [{"path": "app.py", "isResolved": True}],
}


def publish_args(path: Path, **overrides) -> SimpleNamespace:
    arguments = {
        "state": str(path),
        "validated": None,
        "not_validated": None,
        "rewrote": None,
        "validation_commit": None,
    }
    arguments.update(overrides)
    return SimpleNamespace(**arguments)


def write_state(directory: Path, *, published_head=None, **overrides) -> Path:
    state = {
        "version": MODULE.STATE_VERSION,
        "created_at": "2026-01-01T00:00:00Z",
        "iterations": 0,
        "next_candidate_id": 1,
        "history": [],
        "repo_root": str(directory),
        "audit_branch": "trask-pr-audit-7",
        "context_path": str(directory / "state.json.context.json"),
        "pr": dict(PR_METADATA),
        "original": {
            "base_sha": "base1",
            "head_sha": "head1",
            "base_branch": "main",
            "head_branch": "feature",
            "merge_commit": "merge1",
            "merged_at": "2024-05-01T00:00:00Z",
            "captured_at": "2026-01-01T00:00:00Z",
            "commits": [
                {"sha": "commit1", "message": "Change app", "files": ["app.py"]}
            ],
        },
        "audit": {
            "id": "pr-7-audit-1",
            "status": "active",
            "iteration": 1,
            "iteration_head_sha": "head1",
            "branch": "trask-pr-audit-7",
            "diff_path": str(directory / "state.json.diff"),
            "diff_source": MODULE.GITHUB_PR_DIFF,
            "audit_commits": [],
            "anchors": {"app.py": {"LEFT": [2], "RIGHT": [2, 3]}},
            "candidates": [],
            "batches": [],
        },
    }
    if published_head:
        state["audit"]["status"] = "published"
        state["audit"]["published_head_sha"] = published_head
    state.update(overrides)
    path = directory / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


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

    def test_declares_the_agent_and_its_activation(self):
        self.assertIn("name: Historical PR Audit", self.instructions)
        self.assertIn(
            'description: "Use when selected with only a merged PR URL, PR number, '
            'or owner/repo#number',
            self.instructions,
        )
        self.assertIn(
            "tools: [read, edit, search, execute, agent, todo, rename_session, "
            "rename_branch]",
            self.instructions,
        )
        self.assertIn(
            "## Activation: Bare PR References Run The Full Audit", self.instructions
        )
        self.assertIn(
            "Never defer to the generic `github-pr-diff-review` skill for these "
            "inputs, and never call it or pass the work to it",
            self.instructions,
        )
        self.assertIn("with `--new-invocation`", self.instructions)
        self.assertIn("`--invocation-run <token>`", self.instructions)

    def test_changes_nothing_on_github_except_the_audit_branch(self):
        section = _agent_section(
            self.instructions, "## The Merged Pull Request Never Changes"
        )
        self.assertIn(
            "It never creates or changes a pull request, a review, an inline "
            "comment, a pull request comment, an issue, a label, a milestone, a "
            "title, or a description.",
            section,
        )
        self.assertIn(
            "Its only change to GitHub is pushing the audit branch", section
        )
        self.assertIn("read-only allowlist", section)
        self.assertIn("Do not open a pull request from it", section)

    def test_requires_a_merged_pull_request(self):
        self.assertIn(
            "The pull request must be merged. The helper refuses an open or a "
            "closed one, and that refusal is the end of the run.",
            self.instructions,
        )

    def test_pins_the_historical_snapshot_and_never_the_live_branch(self):
        self.assertIn(
            "On the first pass that is the pull request diff GitHub reported for "
            "the merged snapshot. On every later pass it is the cumulative diff "
            "between the same pinned original base commit and the current audit "
            "head.",
            self.instructions,
        )
        self.assertIn(
            "Never use the current base branch tip, the current default branch, "
            "the live pull request branch ref, or the working tree in its place.",
            self.instructions,
        )
        self.assertIn(
            "Repository instructions, path-specific instructions, sibling "
            "implementations, and precedents all come from that checkout, never "
            "from the current default branch.",
            self.instructions,
        )

    def test_documents_the_original_snapshot_capture(self):
        section = _agent_section(self.instructions, "### The Original Snapshot")
        self.assertIn("the exact `baseRefOid` and `headRefOid`", section)
        self.assertIn(
            "fetched between two metadata reads that must agree", section
        )
        self.assertIn(
            "the original title, body, commit list, linked issues, issue comments, "
            "reviews, and review threads",
            section,
        )
        self.assertIn("head_ref_moved", section)
        self.assertIn("keeps the recorded SHAs", section)
        self.assertIn("never audit the newer commits", section)

    def test_documents_the_audit_branch_rules(self):
        section = _agent_section(self.instructions, "### The Audit Branch")
        self.assertIn(
            "The audit branch is named `trask-pr-audit-<PR number>`", section
        )
        self.assertIn("The helper verifies the branch that is actually checked out", section)
        self.assertIn("call `rename_branch` once with `pr-audit-<PR number>`", section)
        self.assertIn(
            "proves that branch is clean and holds no commit of its own before it "
            "moves the branch to the original head commit",
            section,
        )
        self.assertIn("fetches that commit by its exact SHA", section)
        self.assertIn("It never runs a destructive reset to get there.", section)
        self.assertIn(
            "refuses a dirty worktree, a branch that holds unique work, an audit "
            "branch that already exists locally under another checkout, and a "
            "remote audit branch left over from an earlier run",
            section,
        )
        self.assertIn(
            "Never check out, move, or push the pull request's own head branch, "
            "and never work in the repository's main checkout.",
            section,
        )

    def test_keeps_the_claude_only_model_gate_and_one_evaluator_per_candidate(self):
        self.assertIn("## Model Gate", self.instructions)
        self.assertIn("Run only on a Claude model.", self.instructions)
        self.assertIn(
            "using agent type **general-purpose**, model **GPT-5.6 Sol**, and "
            "reasoning effort **max**",
            self.instructions,
        )
        self.assertIn(
            "The agent type is required even when you set the model override",
            self.instructions,
        )
        self.assertIn(
            "do not substitute an explore, task, review, or other specialized agent",
            self.instructions,
        )
        self.assertIn("for **each candidate separately**", self.instructions)
        self.assertIn("## Parallel Evaluation", self.instructions)
        self.assertIn(
            "issue the calls together in one tool-call response so they execute "
            "concurrently",
            self.instructions,
        )

    def test_ports_the_repository_precedent_rules(self):
        self.assertIn(
            "For each changed area, find the closest existing implementations in "
            "the same historical tree, especially sibling implementations of the "
            "same feature or instrumentation.",
            self.instructions,
        )
        self.assertIn(
            "A precedent is strong when multiple comparable implementations use "
            "the same pattern, or when comparable code uses one canonical shared "
            "helper or structure.",
            self.instructions,
        )
        self.assertIn(
            "Record the paths and symbols that establish the precedent, the exact "
            "way this pull request departs from it",
            self.instructions,
        )
        self.assertIn(
            "A single similar file, a broad style preference, or novelty by itself "
            "establishes nothing.",
            self.instructions,
        )
        self.assertIn(
            "build a candidate even when no written repository instruction names "
            "the pattern and the departure has not caused a runtime defect",
            self.instructions,
        )
        self.assertIn(
            "For a precedent candidate, also give it the cited paths and symbols, "
            "the pattern they establish, why that pattern applies here, the exact "
            "departure, and any evidence that may explain the difference.",
            self.instructions,
        )

    def test_the_evaluation_standard_admits_precedent_and_rejects_taste(self):
        standard = _agent_section(self.instructions, "## Evaluation Standard")
        self.assertIn(
            "- an unexplained departure from a strong, directly applicable "
            "repository precedent, when the evaluator can cite the precedent and "
            "show why it applies.",
            standard,
        )
        self.assertIn(
            "One similar file, generic consistency, or reviewer taste does not "
            "establish one.",
            standard,
        )
        self.assertIn(
            '"Unexplained" means the repository instructions, pull request '
            "context, linked work, maintainer comments, and code constraints give "
            "no concrete reason for the difference. It never means the author had "
            "to write a rationale.",
            standard,
        )
        self.assertIn(
            "A preference with no repository instruction or strong, directly "
            "applicable precedent behind it does not clear decision 2.",
            standard,
        )
        self.assertIn(
            '"It cannot be ruled out" states that evidence is missing, so it '
            "decides nothing.",
            standard,
        )

    def test_commit_body_uses_the_audit_finding_label(self):
        section = _agent_section(self.instructions, "## Commit Content")
        self.assertIn("Audit finding:", section)
        self.assertIn("Analysis: <technical analysis and rationale>", section)
        self.assertIn("Upsides: <concrete benefits>", section)
        self.assertIn("Downsides:", section)
        self.assertIn("git commit -F <path>", section)
        self.assertIn("Never build the message with `git commit -m`", section)

    def test_records_both_clean_exits(self):
        self.assertEqual(
            self.instructions.count("run `resolve --state <path> --outcome clean`"),
            2,
        )

    def test_documents_the_capped_autonomous_loop(self):
        self.assertIn(
            "The loop is `preflight -> audit -> evaluate -> batch -> commit -> "
            "publish`, repeated for each new audit head.",
            self.instructions,
        )
        self.assertIn("The maximum is 5 iterations.", self.instructions)
        self.assertIn(
            "Respect `max_iterations_reached` before you edit anything",
            self.instructions,
        )
        self.assertIn(
            "Never wait for `next`, `commit`, `looks good`, `publish`, or "
            "`push etc`",
            self.instructions,
        )

    def test_a_clean_first_pass_leaves_no_branch_on_the_remote(self):
        section = _agent_section(
            self.instructions, "## Publishing And The Next Iteration"
        )
        self.assertIn(
            "The helper pushed nothing, so a first pass that found nothing leaves "
            "no branch on the remote at all.",
            section,
        )
        self.assertIn(
            "the helper pushed the audit branch and verified that the remote "
            "branch matches the local head",
            section,
        )

    def test_final_response_links_the_source_pr_and_the_audit_branch(self):
        section = _agent_section(self.instructions, "## Final Response")
        self.assertIn(
            "- `**PR:** [#<pr.number> <pr.title>](<pr.pr_url>)`", section
        )
        self.assertIn(
            "- `**Audit branch:** [<audit.branch>]"
            "(https://github.com/<repo_name>/tree/<audit.branch>)`",
            section,
        )
        self.assertIn(
            "- `[<short-sha> <short batch summary>]"
            "(https://github.com/<repo_name>/commit/<full-sha>)`",
            section,
        )
        self.assertIn("- `**Outcome:** clean after <n> iteration(s).`", section)
        self.assertIn("- `**Not validated locally:** <reason>`", section)
        self.assertIn("- `**Snapshot drift:**", section)
        self.assertIn("- `**Dropped candidates:**`", section)
        self.assertIn(
            "Link each commit to the audit branch's own commit page, never to a "
            "pull request file view",
            section,
        )
        self.assertIn(
            "Render the `**Audit branch:**` line only when this run pushed the "
            "branch",
            section,
        )

    def test_keeps_plain_language_and_a_categorized_retrospective(self):
        self.assertIn("## Plain Language", self.instructions)
        self.assertIn("Say one thing per sentence.", self.instructions)
        retrospective = _agent_section(
            self.instructions, "## Historical PR Audit Agent Retrospective"
        )
        for category in (
            "- **Agent**:",
            "- **Helper**:",
            "- **General instructions**:",
            "- **Repository**:",
        ):
            self.assertIn(category, retrospective)
        self.assertIn(
            "Produce the retrospective on every terminal outcome", retrospective
        )
        self.assertIn(
            "The retrospective is advice, and it belongs in chat only.",
            retrospective,
        )
        self.assertIn(
            "the rule that nothing on GitHub changes", retrospective
        )

    def test_documents_the_helper_activity_stamp_without_overselling_it(self):
        self.assertIn("`last_helper_activity`", self.instructions)
        self.assertIn(
            "the moment this helper last wrote its state", self.instructions
        )
        self.assertIn("not proof the stage is alive", self.instructions)

    def test_never_mentions_posting_a_review_or_a_comment_as_its_own_work(self):
        for forbidden in (
            "pending review",
            "post the review",
            "gh pr comment",
            "gh pr edit",
        ):
            self.assertNotIn(forbidden, self.instructions)

    def test_names_the_session_from_preflight_metadata_idempotently(self):
        section = _agent_section(self.instructions, "## Session Naming")
        self.assertIn(
            "ensure the session name is `Historical PR Audit: <PR number> - "
            "<PR title>`",
            section,
        )
        self.assertIn("do not call `rename_session`", section)
        self.assertIn("Otherwise call `rename_session` once", section)
        self.assertIn("Never use an interim number-only name", section)

    def test_helper_command_paths_name_this_plugin(self):
        section = _agent_section(self.instructions, "## Mechanical Helper")
        self.assertIn(
            "installed-plugins/trask-plugins/historical-pr-audit/scripts/"
            "historical_pr_audit.py",
            section,
        )
        self.assertIn(
            "Never pass a `~`-prefixed helper path to native Windows Python from "
            "Git Bash.",
            section,
        )
        for subcommand in (
            "`preflight ",
            "`candidates ",
            "`drop ",
            "`plan ",
            "`resolve ",
            "`publish ",
            "`status ",
            "`cleanup ",
        ):
            self.assertIn(subcommand, section)

    def test_treats_the_preloaded_repository_instructions_as_stale(self):
        self.assertIn(
            "Any repository instruction the app preloaded into this session came "
            "from the live tree, before `preflight` moved the branch to the "
            "historical head. It is stale for this audit.",
            self.instructions,
        )
        self.assertIn(
            "Re-read `AGENTS.md`, `CLAUDE.md`, `.github/copilot-instructions.md`, "
            "and every path-specific instruction file from the checked-out "
            "historical tree",
            self.instructions,
        )
        self.assertIn(
            "Where the two disagree, the historical version wins and the preloaded "
            "one counts for nothing.",
            self.instructions,
        )
        self.assertIn(
            "Read those instruction files yourself even when this session already "
            "shows you a copy",
            self.instructions,
        )

    def test_names_all_five_commit_identities_separately(self):
        section = _agent_section(self.instructions, "## Mechanical Helper")
        for identity in (
            "the **pinned original head** is `head_sha`",
            "the **iteration head** is `audit.iteration_head_sha`",
            "the **local head** is whatever the branch points at right now",
            "the **published head** is `audit.published_head_sha`",
            "the **clean head** is `audit.clean_at_head_sha`",
        ):
            self.assertIn(identity, section)

    def test_documents_the_stored_state_stops_and_the_resume_rule(self):
        section = _agent_section(self.instructions, "## Mechanical Helper")
        self.assertIn(
            "Two stored-state answers come back before it reads GitHub, moves the "
            "branch, archives anything, or writes a file: `max_iterations_reached` "
            "when the cap is already spent, and `already_complete` when a previous "
            "iteration recorded a clean outcome.",
            section,
        )
        self.assertIn("Neither writes a `preflight_path`.", section)
        self.assertIn(
            "A later pass resumes only from an iteration that published.", section
        )
        self.assertIn(
            "a merged pull request's head is an ancestor of the branch it merged "
            "into, so a renamed live default branch would pass that test",
            section,
        )
        branch = _agent_section(self.instructions, "### The Audit Branch")
        self.assertIn(
            "the local branch and the remote audit branch must both sit at that "
            "exact commit",
            branch,
        )

    def test_one_audit_branch_is_one_audit(self):
        section = _agent_section(self.instructions, "## Target And Preflight")
        self.assertIn(
            "`already_complete`: a previous run already resolved this audit clean "
            "at `clean_at_head_sha`.",
            section,
        )
        self.assertIn("One audit branch is one audit, on purpose.", section)
        self.assertIn(
            "Auditing the same pull request again is a deliberate act that happens "
            "outside this run: someone runs `cleanup` for the state and deals with "
            "the remote audit branch.",
            section,
        )
        self.assertIn("Never do either yourself to get a second run started.", section)

    def test_documents_the_validation_commit_and_the_partial_report(self):
        section = _agent_section(
            self.instructions, "### Local Validation Before A Push"
        )
        self.assertIn(
            "Run a batch's fixing commands before you commit that batch, so its "
            "rewrites land in the batch commit itself.",
            section,
        )
        self.assertIn(
            "Commit those rewrites on their own, before you publish.", section
        )
        self.assertIn("`--validation-commit <sha>`", section)
        self.assertIn(
            "keep every path inside the paths your batches already planned", section
        )
        self.assertIn(
            "pass it next to `--validated` when some ran and others could not, "
            "naming what could not run and why",
            section,
        )
        self.assertIn("`passed`, `partial`, `skipped`, or `unreported`", section)
        self.assertEqual(section.count("Tell `publish` what you did."), 1)

    def test_planned_paths_bind_the_recorded_commit(self):
        section = _agent_section(self.instructions, "## Batching And Batch Execution")
        self.assertIn(
            "`record --commit` enforces this: it refuses a batch that was never "
            "planned, a candidate list that is not the one that batch plans, an "
            "empty planned path list, and a commit that touches any path the batch "
            "did not declare.",
            section,
        )


class ReadOnlyGitHubTest(unittest.TestCase):
    def test_allows_the_reads_the_audit_needs(self):
        MODULE.require_read_only_gh(
            ["pr", "view", "https://github.com/owner/repo/pull/7", "--json", "state"]
        )
        MODULE.require_read_only_gh(
            ["pr", "diff", "https://github.com/owner/repo/pull/7", "--repo", "owner/repo"]
        )
        MODULE.require_read_only_gh(["api", "repos/owner/repo/git/ref/heads/main"])
        MODULE.require_read_only_gh(
            ["api", "graphql", "-f", "query=query($n:Int!){viewer{login}}"]
        )

    def test_refuses_every_shape_of_pull_request_mutation(self):
        for arguments in (
            ["pr", "edit", "7", "--title", "x"],
            ["pr", "comment", "7", "--body", "x"],
            ["pr", "review", "7", "--approve"],
            ["pr", "merge", "7"],
            ["pr", "close", "7"],
            ["pr", "reopen", "7"],
            ["issue", "create", "--title", "x"],
            ["issue", "comment", "3", "--body", "x"],
        ):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.require_read_only_gh(arguments)
            self.assertIn("read-only allowlist", str(error.exception))

    def test_refuses_a_mutating_api_method_in_every_spelling(self):
        for arguments in (
            ["api", "--method", "POST", "repos/owner/repo/issues"],
            ["api", "--method=PATCH", "repos/owner/repo/pulls/7"],
            ["api", "-X", "PUT", "repos/owner/repo/contents/x"],
            ["api", "-XDELETE", "repos/owner/repo/git/refs/heads/x"],
        ):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.require_read_only_gh(arguments)
            self.assertIn("mutating GitHub request", str(error.exception))

    def test_refuses_an_implicit_rest_post(self):
        for arguments in (
            ["api", "repos/owner/repo/issues/7/comments", "-f", "body=x"],
            ["api", "repos/owner/repo/issues/7/comments", "-Fbody=x"],
            ["api", "repos/owner/repo/issues/7/comments", "--field=body=x"],
            ["api", "repos/owner/repo/issues/7/comments", "--input", "body.json"],
        ):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.require_read_only_gh(arguments)
            self.assertIn("mutating GitHub request", str(error.exception))

    def test_allows_explicit_get_parameters(self):
        MODULE.require_read_only_gh(
            [
                "api",
                "--method",
                "GET",
                "search/issues",
                "-f",
                "q=repo:owner/repo",
            ]
        )

    def test_refuses_a_graphql_mutation(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.require_read_only_gh(
                ["api", "graphql", "-f", "query=mutation{addComment(input:{}){id}}"]
            )
        self.assertIn("GraphQL mutation", str(error.exception))

    def test_mutation_in_a_repo_name_or_variable_is_not_a_graphql_mutation(self):
        MODULE.require_read_only_gh(
            ["api", "repos/infection/mutation-testing/git/ref/heads/main"]
        )
        MODULE.require_read_only_gh(
            [
                "api",
                "graphql",
                "-f",
                "query=query($repo:String!){repository(name:$repo){id}}",
                "-f",
                "repo=mutation-testing",
            ]
        )

    def test_refuses_graphql_without_an_explicit_query(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.require_read_only_gh(
                ["api", "graphql", "-f", "repo=owner"]
            )
        self.assertIn("without an explicit query", str(error.exception))

    def test_every_github_call_in_the_helper_goes_through_the_guard(self):
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertEqual(source.count('["gh"'), 1)
        self.assertEqual(source.count('run(["gh", *arguments]'), 1)
        self.assertIn("require_read_only_gh(arguments)\n    return run([\"gh\"", source)
        self.assertNotIn("run(['gh'", source)

    def test_the_helper_never_names_a_pull_request_mutation_command(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in (
            "pr edit",
            "pr comment",
            "pr review",
            "pr merge",
            "issue create",
            "issue comment",
        ):
            self.assertNotIn(f'"{forbidden}"', source)


class TargetParsingTest(unittest.TestCase):
    def test_accepts_urls_and_short_targets(self):
        self.assertEqual(
            MODULE.parse_target("https://github.com/owner/repo/pull/7")["number"], 7
        )
        self.assertEqual(MODULE.parse_target("owner/repo#7")["repo_name"], "owner/repo")

    def test_rejects_unsupported_targets(self):
        for value in ("7", "owner/repo", "https://example.com/owner/repo/pull/7"):
            with self.assertRaises(MODULE.WorkflowError):
                MODULE.parse_target(value)

    def test_the_target_is_required_because_no_branch_can_stand_in_for_it(self):
        parser = MODULE.build_parser()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["preflight"])


class AuditBranchNameTest(unittest.TestCase):
    def test_builds_the_branch_name_from_the_pull_request_number(self):
        self.assertEqual(MODULE.audit_branch_name(7), "trask-pr-audit-7")
        self.assertEqual(MODULE.audit_branch_name(1234), "trask-pr-audit-1234")

    def test_reads_the_number_back_only_from_an_exact_audit_branch_name(self):
        self.assertEqual(MODULE.audit_branch_number("trask-pr-audit-7"), 7)
        for value in (
            "",
            "main",
            "trask-pr-audit-",
            "trask-pr-audit-7x",
            "copilot/trask-pr-audit-7",
            "trask-pr-audit-7/fix",
        ):
            self.assertIsNone(MODULE.audit_branch_number(value))


class MergedMetadataTest(unittest.TestCase):
    def payload(self, **overrides):
        payload = {
            "number": 7,
            "title": "Add a thing",
            "url": "https://github.com/owner/repo/pull/7",
            "state": "MERGED",
            "mergedAt": "2024-05-01T00:00:00Z",
            "mergeCommit": {"oid": "merge1"},
            "baseRefName": "main",
            "baseRefOid": "base1",
            "headRefName": "feature",
            "headRefOid": "head1",
            "headRepositoryOwner": {"login": "fork"},
            "headRepository": {"name": "repo"},
            "commits": [{"oid": "commit1", "messageHeadline": "Change app"}],
        }
        payload.update(overrides)
        return payload

    def metadata(self, payload):
        with mock.patch.object(MODULE, "gh_json", return_value=payload):
            return MODULE.merged_metadata_for(MODULE.parse_target("owner/repo#7"))

    def test_pins_the_exact_base_and_head_object_ids(self):
        metadata = self.metadata(self.payload())

        self.assertEqual(metadata["base_sha"], "base1")
        self.assertEqual(metadata["head_sha"], "head1")
        self.assertEqual(metadata["merge_commit"], "merge1")
        self.assertEqual(metadata["merged_at"], "2024-05-01T00:00:00Z")
        self.assertEqual(
            metadata["commits"], [{"sha": "commit1", "message": "Change app"}]
        )

    def test_never_reads_a_branch_tip_for_the_base_commit(self):
        """A branch tip names today's code, which is the opposite of an audit."""
        calls = []

        def record(arguments):
            calls.append(arguments)
            return self.payload()

        with mock.patch.object(MODULE, "gh_json", side_effect=record):
            metadata = MODULE.merged_metadata_for(MODULE.parse_target("owner/repo#7"))

        self.assertEqual(metadata["base_sha"], "base1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["pr", "view"])
        self.assertFalse(hasattr(MODULE, "base_ref_tip"))

    def test_refuses_a_pull_request_that_is_not_merged(self):
        for state in ("OPEN", "CLOSED"):
            with self.assertRaises(MODULE.WorkflowError) as error:
                self.metadata(self.payload(state=state))
            self.assertIn("only on a merged pull request", str(error.exception))
            self.assertIn(state, str(error.exception))

    def test_requires_both_pinned_object_ids(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            self.metadata(self.payload(baseRefOid=""))
        self.assertIn("no base commit", str(error.exception))
        with self.assertRaises(MODULE.WorkflowError) as error:
            self.metadata(self.payload(headRefOid=None))
        self.assertIn("no head commit", str(error.exception))

    def test_tolerates_a_deleted_head_fork(self):
        metadata = self.metadata(
            self.payload(headRepositoryOwner=None, headRepository=None)
        )

        self.assertIsNone(metadata["head_owner"])
        self.assertIsNone(metadata["head_repo"])
        self.assertEqual(metadata["head_branch"], "feature")

    def test_rejects_metadata_for_another_pull_request(self):
        with self.assertRaises(MODULE.WorkflowError):
            self.metadata(self.payload(number=8))


class DiffSourceTest(unittest.TestCase):
    def test_the_first_pass_reads_the_pull_request_diff_github_reports(self):
        with mock.patch.object(
            MODULE, "gh", return_value=SimpleNamespace(stdout=DIFF)
        ) as command:
            self.assertEqual(MODULE.fetch_pr_diff(PR_METADATA), DIFF)

        command.assert_called_once_with(
            [
                "pr",
                "diff",
                "https://github.com/owner/repo/pull/7",
                "--repo",
                "owner/repo",
            ]
        )

    def test_a_later_pass_diffs_the_pinned_base_against_the_audit_head(self):
        with mock.patch.object(
            MODULE, "run", return_value=SimpleNamespace(stdout=CUMULATIVE_DIFF)
        ) as command:
            diff = MODULE.cumulative_diff(Path("/repo"), "base1", "audit2")

        self.assertEqual(diff, CUMULATIVE_DIFF)
        arguments = command.call_args.args[0]
        self.assertEqual(arguments[-2:], ["--no-color", "base1...audit2"])
        self.assertNotIn("origin/main", arguments)
        self.assertNotIn("main", arguments)
        self.assertNotIn("HEAD", arguments)

    def test_a_later_pass_excludes_base_work_added_after_the_branches_split(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary).resolve()
            MODULE.run(["git", "init", "-b", "main", str(repo)])
            MODULE.run(["git", "-C", str(repo), "config", "user.name", "Test"])
            MODULE.run(
                ["git", "-C", str(repo), "config", "user.email", "test@example.com"]
            )
            (repo / "shared.txt").write_text("shared\n", encoding="utf-8")
            MODULE.run(["git", "-C", str(repo), "add", "shared.txt"])
            MODULE.run(["git", "-C", str(repo), "commit", "-m", "shared"])
            MODULE.run(["git", "-C", str(repo), "switch", "-c", "feature"])
            (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
            MODULE.run(["git", "-C", str(repo), "add", "feature.txt"])
            MODULE.run(["git", "-C", str(repo), "commit", "-m", "feature"])
            head_sha = MODULE.git(repo, "rev-parse", "HEAD")
            MODULE.run(["git", "-C", str(repo), "switch", "main"])
            (repo / "base-only.txt").write_text("base\n", encoding="utf-8")
            MODULE.run(["git", "-C", str(repo), "add", "base-only.txt"])
            MODULE.run(["git", "-C", str(repo), "commit", "-m", "base"])
            base_sha = MODULE.git(repo, "rev-parse", "HEAD")

            diff = MODULE.cumulative_diff(repo, base_sha, head_sha)

        self.assertIn("feature.txt", diff)
        self.assertNotIn("base-only.txt", diff)

    def test_parses_changed_lines_per_side(self):
        anchors = MODULE.parse_unified_diff(DIFF)

        self.assertEqual(anchors["app.py"]["RIGHT"], {2, 3})
        self.assertEqual(anchors["app.py"]["LEFT"], {2})


class AuditCommitsTest(unittest.TestCase):
    def test_lists_only_the_commits_this_audit_added(self):
        with mock.patch.object(
            MODULE,
            "git",
            return_value="audit1\x1fFix one\naudit2\x1fFix two",
        ) as command:
            commits = MODULE.audit_commits(Path("/repo"), "head1")

        self.assertEqual(
            commits,
            [
                {"sha": "audit1", "message": "Fix one"},
                {"sha": "audit2", "message": "Fix two"},
            ],
        )
        self.assertEqual(command.call_args.args[-1], "head1..HEAD")

    def test_compares_recorded_history_commits_with_current_audit_commits(self):
        presence = MODULE.compare_history_commits(
            [
                {"id": 1, "commit": "audit1"},
                {"id": 2, "commit": None},
                {"id": 3, "commit": "gone"},
            ],
            [{"sha": "audit1", "message": "Fix one"}],
        )

        self.assertEqual(
            presence,
            [
                {"history_id": 1, "commit": "audit1", "in_audit_commits": True},
                {"history_id": 3, "commit": "gone", "in_audit_commits": False},
            ],
        )


class BranchPreparationTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path("/repo")
        self.git_results = {
            ("branch", "--show-current"): "trask-pr-audit-7",
            ("rev-parse", "HEAD"): "main9",
            ("remote",): "origin",
            ("remote", "get-url", "origin"): "https://github.com/owner/repo.git",
            ("rev-list", "refs/remotes/origin/main..HEAD"): "",
            ("cherry", "refs/remotes/origin/main", "HEAD"): "",
            ("switch", "--detach", "head1"): "",
            ("branch", "--force", "trask-pr-audit-7", "head1"): "",
            ("switch", "trask-pr-audit-7"): "",
        }
        self.git_try_results = {
            ("show-ref", "--verify", "--quiet", "refs/heads/trask-pr-audit-7"): 0,
            ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): (
                0,
                "refs/remotes/origin/main",
            ),
            ("config", "--get", "branch.trask-pr-audit-7.remote"): 1,
            ("config", "--get", "branch.trask-pr-audit-7.merge"): 1,
            (
                "rev-parse",
                "--verify",
                "--quiet",
                "refs/remotes/origin/main^{commit}",
            ): 0,
            ("cat-file", "-e", "head1^{commit}"): 1,
            ("cat-file", "-e", "base1^{commit}"): 1,
            ("fetch", "--no-tags", "origin", "head1"): 0,
            ("fetch", "--no-tags", "origin", "base1"): 0,
        }
        self.commits_present = set()
        self.ancestors = set()
        self.remote_audit_head = None
        self.git_calls = []

    def fake_git(self, root, *arguments):
        self.git_calls.append(arguments)
        if arguments in self.git_results:
            return self.git_results[arguments]
        raise AssertionError(f"unexpected git call: {arguments}")

    def fake_git_try(self, root, *arguments):
        if arguments[:2] == ("cat-file", "-e"):
            sha = arguments[2].split("^", 1)[0]
            code = 0 if sha in self.commits_present else 1
            return SimpleNamespace(returncode=code, stdout="", stderr="")
        if arguments[:1] == ("fetch",):
            self.commits_present.add(arguments[-1])
        if arguments[:2] == ("merge-base", "--is-ancestor"):
            ancestor, descendant = arguments[2], arguments[3]
            code = 0 if (ancestor, descendant) in self.ancestors else 1
            return SimpleNamespace(returncode=code, stdout="", stderr="")
        outcome = self.git_try_results.get(arguments, 1)
        if isinstance(outcome, tuple):
            code, stdout = outcome
        else:
            code, stdout = outcome, ""
        return SimpleNamespace(returncode=code, stdout=stdout, stderr="")

    def prepare(self, **overrides):
        arguments = {
            "pr": dict(PR_METADATA),
            "audit_branch": "trask-pr-audit-7",
            "original_head_sha": "head1",
            "original_base_sha": "base1",
            "resuming": False,
        }
        arguments.update(overrides)
        with (
            mock.patch.object(MODULE, "git", side_effect=self.fake_git),
            mock.patch.object(MODULE, "git_try", side_effect=self.fake_git_try),
            mock.patch.object(
                MODULE, "remote_head", return_value=self.remote_audit_head
            ),
        ):
            return MODULE.prepare_audit_branch(self.repo, **arguments)

    def resume(self, *, local_head, published_head, remote_head=..., pinned="head1"):
        self.git_results[("rev-parse", "HEAD")] = local_head
        self.commits_present.add(pinned)
        self.ancestors = {(pinned, local_head)}
        self.remote_audit_head = (
            published_head if remote_head is ... else remote_head
        )
        return self.prepare(resuming=True, expected_resume_head=published_head)

    def test_moves_a_proven_clean_fresh_branch_onto_the_historical_head(self):
        self.git_results[("rev-parse", "HEAD")] = "main9"
        calls = []

        def rev_parse(root, *arguments):
            calls.append(arguments)
            if arguments == ("rev-parse", "HEAD"):
                return "main9" if len(
                    [call for call in calls if call == ("rev-parse", "HEAD")]
                ) == 1 else "head1"
            return self.fake_git(root, *arguments)

        with (
            mock.patch.object(MODULE, "git", side_effect=rev_parse),
            mock.patch.object(MODULE, "git_try", side_effect=self.fake_git_try),
            mock.patch.object(MODULE, "remote_head", return_value=None),
        ):
            result = MODULE.prepare_audit_branch(
                self.repo,
                pr=dict(PR_METADATA),
                audit_branch="trask-pr-audit-7",
                original_head_sha="head1",
                original_base_sha="base1",
                resuming=False,
            )

        self.assertEqual(result["branch_action"], "realigned")
        self.assertEqual(result["local_head"], "head1")
        self.assertEqual(result["reference"], "refs/remotes/origin/main")
        self.assertIn(("switch", "--detach", "head1"), calls)
        self.assertIn(("branch", "--force", "trask-pr-audit-7", "head1"), calls)
        self.assertIn(("switch", "trask-pr-audit-7"), calls)

    def test_never_uses_a_destructive_reset_to_realign(self):
        source = SCRIPT.read_text(encoding="utf-8")

        for forbidden in (
            '"reset"',
            '"--hard"',
            '"-B"',
            '"--force-with-lease"',
            '"clean", "-',
            '"checkout", "-f"',
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn('git(repo_root, "switch", "--detach", sha)', source)
        self.assertIn('git(repo_root, "branch", "--force", branch, sha)', source)

    def test_fetches_the_historical_head_by_sha_before_it_moves_anything(self):
        fetches = []

        def record_fetch(root, *arguments):
            if arguments[:1] == ("fetch",):
                fetches.append(arguments)
            return self.fake_git_try(root, *arguments)

        with (
            mock.patch.object(MODULE, "git", side_effect=self.fake_git),
            mock.patch.object(MODULE, "git_try", side_effect=record_fetch),
            mock.patch.object(MODULE, "remote_head", return_value=None),
            mock.patch.object(
                MODULE, "realign_branch"
            ),
        ):
            self.git_results[("rev-parse", "HEAD")] = "head1"
            MODULE.prepare_audit_branch(
                self.repo,
                pr=dict(PR_METADATA),
                audit_branch="trask-pr-audit-7",
                original_head_sha="head1",
                original_base_sha="base1",
                resuming=False,
            )

        self.assertEqual(
            fetches,
            [
                ("fetch", "--no-tags", "origin", "head1"),
                ("fetch", "--no-tags", "origin", "base1"),
            ],
        )

    def test_falls_back_to_the_pull_request_ref_when_the_sha_fetch_fails(self):
        self.commits_present = set()
        attempts = []

        def fetch_try(root, *arguments):
            if arguments[:1] == ("fetch",):
                attempts.append(arguments)
                if arguments[-1] == "head1":
                    return SimpleNamespace(returncode=1, stdout="", stderr="no such")
                self.commits_present.add("head1")
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return self.fake_git_try(root, *arguments)

        with mock.patch.object(MODULE, "git_try", side_effect=fetch_try):
            MODULE.fetch_commit(self.repo, "origin", "head1", "refs/pull/7/head")

        self.assertEqual(
            attempts,
            [
                ("fetch", "--no-tags", "origin", "head1"),
                ("fetch", "--no-tags", "origin", "refs/pull/7/head"),
            ],
        )

    def test_refuses_a_starting_branch_that_holds_unique_work(self):
        self.git_results[("rev-list", "refs/remotes/origin/main..HEAD")] = "local1"
        self.git_results[("cherry", "refs/remotes/origin/main", "HEAD")] = "+ local1"

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.prepare()

        self.assertIn("holds unique work", str(error.exception))
        self.assertIn("local1", str(error.exception))

    def test_accepts_a_branch_whose_commits_already_landed_upstream(self):
        self.git_results[("rev-list", "refs/remotes/origin/main..HEAD")] = "local1"
        self.git_results[("cherry", "refs/remotes/origin/main", "HEAD")] = "- local1"

        with mock.patch.object(MODULE, "realign_branch"):
            self.git_results[("rev-parse", "HEAD")] = "head1"
            result = self.prepare()

        self.assertEqual(result["branch_action"], "already_at_original_head")

    def test_refuses_a_branch_with_another_name(self):
        self.git_results[("branch", "--show-current")] = "copilot/some-session"
        self.git_try_results[
            ("show-ref", "--verify", "--quiet", "refs/heads/trask-pr-audit-7")
        ] = 1

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.prepare()

        self.assertIn("current branch is 'copilot/some-session'", str(error.exception))
        self.assertIn("exactly 'trask-pr-audit-7'", str(error.exception))

    def test_refuses_an_audit_branch_that_already_exists_in_another_checkout(self):
        self.git_results[("branch", "--show-current")] = "copilot/some-session"
        self.git_try_results[
            ("show-ref", "--verify", "--quiet", "refs/heads/trask-pr-audit-7")
        ] = 0

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.prepare()

        self.assertIn("already exists locally", str(error.exception))

    def test_refuses_a_remote_audit_branch_left_over_from_an_earlier_run(self):
        self.remote_audit_head = "stale1"

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.prepare()

        self.assertIn("already exists at stale1", str(error.exception))

    def test_refuses_the_pull_requests_own_branch_and_its_base_branch(self):
        for branch in ("feature", "main"):
            self.git_results[("branch", "--show-current")] = branch
            with self.assertRaises(MODULE.WorkflowError) as error:
                self.prepare()
            self.assertIn(
                "never uses the pull request's own head branch or its base branch",
                str(error.exception),
            )

    def test_refuses_a_detached_worktree(self):
        self.git_results[("branch", "--show-current")] = ""

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.prepare()

        self.assertIn("detached HEAD", str(error.exception))

    def test_resuming_keeps_the_branch_it_published_last_iteration(self):
        result = self.resume(local_head="audit2", published_head="audit2")

        self.assertEqual(result["branch_action"], "resumed")
        self.assertEqual(result["local_head"], "audit2")
        self.assertNotIn(("switch", "--detach", "head1"), self.git_calls)

    def test_resuming_refuses_a_branch_that_lost_the_pinned_head(self):
        self.git_results[("rev-parse", "HEAD")] = "audit2"
        self.commits_present.add("head1")
        self.ancestors = set()
        self.remote_audit_head = "audit2"

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.prepare(resuming=True, expected_resume_head="audit2")

        self.assertIn("no longer contains the pinned original head", str(error.exception))

    def test_resuming_refuses_a_live_main_branch_wearing_the_audit_name(self):
        """A merged pull request's head is an ancestor of the branch it merged into.

        Renaming that live branch to the audit name therefore satisfies every
        ancestry check, so only the head this audit itself published proves the
        branch is the audit branch.
        """
        with self.assertRaises(MODULE.WorkflowError) as error:
            self.resume(local_head="main9", published_head="audit2")

        self.assertIn("is at main9", str(error.exception))
        self.assertIn("audit2 the previous iteration published", str(error.exception))
        self.assertIn("cannot prove it created", str(error.exception))

    def test_resuming_refuses_a_remote_audit_branch_that_moved(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            self.resume(
                local_head="audit2", published_head="audit2", remote_head="someone9"
            )

        self.assertIn("remote branch 'trask-pr-audit-7' is at someone9", str(error.exception))
        self.assertIn("refuses to resume over a branch that moved", str(error.exception))

    def test_resuming_refuses_a_remote_audit_branch_that_disappeared(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            self.resume(local_head="audit2", published_head="audit2", remote_head=None)

        self.assertIn("is at no commit", str(error.exception))

    def test_resuming_refuses_without_a_published_head_to_match(self):
        self.git_results[("rev-parse", "HEAD")] = "audit2"

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.prepare(resuming=True, expected_resume_head=None)

        self.assertIn("no published head from the previous iteration", str(error.exception))


class ResumeHeadTest(unittest.TestCase):
    def state(self, audit):
        return {"version": MODULE.STATE_VERSION, "audit": audit}

    def test_accepts_the_published_head_of_the_previous_iteration(self):
        head = MODULE.resume_head_for(
            self.state({"status": "published", "published_head_sha": " audit2 "})
        )

        self.assertEqual(head, "audit2")

    def test_refuses_an_iteration_that_is_still_active(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.resume_head_for(self.state({"status": "active"}))

        self.assertIn("'active', not 'published'", str(error.exception))
        self.assertIn("refuses to resume", str(error.exception))

    def test_refuses_a_state_with_no_previous_iteration(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.resume_head_for({"version": MODULE.STATE_VERSION})

        self.assertIn("records no previous iteration", str(error.exception))

    def test_refuses_a_published_iteration_with_no_recorded_head(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.resume_head_for(
                self.state({"status": "published", "published_head_sha": "  "})
            )

        self.assertIn("records no published head", str(error.exception))


class StartingPointReferenceTest(unittest.TestCase):
    def reference(self, results, *, branch="trask-pr-audit-7"):
        def git_try(root, *arguments):
            outcome = results.get(arguments, (1, ""))
            return SimpleNamespace(returncode=outcome[0], stdout=outcome[1], stderr="")

        with mock.patch.object(MODULE, "git_try", side_effect=git_try):
            return MODULE.starting_point_reference(
                Path("/repo"), remote="origin", branch=branch, base_branch="main"
            )

    def test_prefers_the_configured_upstream(self):
        reference = self.reference(
            {
                ("config", "--get", "branch.trask-pr-audit-7.remote"): (0, "origin"),
                ("config", "--get", "branch.trask-pr-audit-7.merge"): (
                    0,
                    "refs/heads/develop",
                ),
                ("rev-parse", "--verify", "--quiet", "origin/develop^{commit}"): (0, ""),
            }
        )

        self.assertEqual(reference, "origin/develop")

    def test_falls_back_to_the_remote_default_branch(self):
        reference = self.reference(
            {
                ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): (
                    0,
                    "refs/remotes/origin/main",
                ),
                (
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    "refs/remotes/origin/main^{commit}",
                ): (0, ""),
            }
        )

        self.assertEqual(reference, "refs/remotes/origin/main")

    def test_falls_back_to_the_pull_requests_base_branch(self):
        reference = self.reference(
            {("rev-parse", "--verify", "--quiet", "origin/main^{commit}"): (0, "")}
        )

        self.assertEqual(reference, "origin/main")

    def test_refuses_when_nothing_can_prove_the_branch_is_fresh(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            self.reference({})

        self.assertIn("cannot prove the starting branch holds no unique work", str(error.exception))


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.emitted = []
        patcher = mock.patch.object(MODULE, "emit", self.emitted.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.metadata = dict(PR_METADATA)
        self.git_results = {
            ("status", "--porcelain=v1"): "",
            ("branch", "--show-current"): "trask-pr-audit-7",
            ("rev-parse", "HEAD"): "head1",
        }
        self.branch_state = {
            "branch": "trask-pr-audit-7",
            "branch_action": "realigned",
            "local_head": "head1",
            "reference": "refs/remotes/origin/main",
        }
        self.commits_added = []

    def preflight(
        self,
        state_path,
        *,
        metadata_sequence=None,
        max_iterations=5,
        target="owner/repo#7",
        diff=DIFF,
        cumulative=CUMULATIVE_DIFF,
        new_invocation=False,
        invocation_run=None,
    ):
        arguments = SimpleNamespace(
            target=target,
            repo_root=str(self.directory),
            state=str(state_path),
            max_iterations=max_iterations,
            new_invocation=new_invocation,
            invocation_run=invocation_run,
        )
        metadata_sequence = metadata_sequence or [self.metadata, self.metadata]
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(
                MODULE, "resolve_repo_root", return_value=self.directory
            ),
            mock.patch.object(
                MODULE, "git", side_effect=lambda root, *args: self.git_results[args]
            ),
            mock.patch.object(
                MODULE, "merged_metadata_for", side_effect=metadata_sequence
            ) as metadata,
            mock.patch.object(
                MODULE, "original_context_for", return_value=dict(CONTEXT)
            ) as context,
            mock.patch.object(MODULE, "fetch_pr_diff", return_value=diff) as pr_diff,
            mock.patch.object(
                MODULE, "cumulative_diff", return_value=cumulative
            ) as cumulative_call,
            mock.patch.object(
                MODULE, "prepare_audit_branch", return_value=self.branch_state
            ) as prepare,
            mock.patch.object(
                MODULE,
                "commit_provenance",
                return_value=[
                    {"sha": "commit1", "message": "Change app", "files": ["app.py"]}
                ],
            ),
            mock.patch.object(
                MODULE, "audit_commits", return_value=self.commits_added
            ) as audit_commits,
        ):
            MODULE.command_preflight(arguments)
            self.calls = SimpleNamespace(
                metadata=metadata,
                context=context,
                pr_diff=pr_diff,
                cumulative_diff=cumulative_call,
                prepare_audit_branch=prepare,
                audit_commits=audit_commits,
            )
        return self.emitted[-1]

    def full_result(self, envelope):
        return json.loads(
            Path(envelope["preflight_path"]).read_text(encoding="utf-8")
        )

    def test_first_pass_pins_the_snapshot_and_captures_the_original_context(self):
        state_path = self.directory / "state.json"

        envelope = self.preflight(state_path)
        result = self.full_result(envelope)

        self.assertEqual(envelope["result"], "ready")
        self.assertEqual(envelope["head_sha"], "head1")
        self.assertEqual(envelope["pr"]["state"], "MERGED")
        self.assertEqual(envelope["audit"]["branch"], "trask-pr-audit-7")
        self.assertEqual(envelope["audit"]["base_sha"], "base1")
        self.assertEqual(envelope["audit"]["head_sha"], "head1")
        self.assertEqual(envelope["audit"]["local_head"], "head1")
        self.assertEqual(envelope["audit"]["iteration_head_sha"], "head1")
        self.assertEqual(envelope["audit"]["diff_source"], MODULE.GITHUB_PR_DIFF)
        self.assertFalse(envelope["audit"]["head_ref_moved"])
        self.assertFalse(envelope["audit"]["base_ref_moved"])
        self.assertEqual(envelope["diff_bytes"], len(DIFF.encode("utf-8")))
        self.assertEqual(
            envelope["counts"],
            {
                "changed_files": 1,
                "original_commits": 1,
                "audit_commits": 0,
                "history": 0,
                "history_commits_missing": 0,
                "issue_comments": 1,
                "review_threads": 1,
                "reviews": 1,
                "closing_issues": 1,
            },
        )
        for field in ("changed_files", "original_commits", "history"):
            self.assertNotIn(field, envelope)
        self.assertEqual(result["changed_files"], ["app.py"])
        self.assertEqual(
            result["original_commits"],
            [{"sha": "commit1", "message": "Change app", "files": ["app.py"]}],
        )
        self.assertEqual(result["iteration"], 1)

        context = json.loads(
            Path(envelope["context_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(context["body"], "Original description")
        self.assertEqual(context["issue_comments"], [{"body": "looks good"}])
        self.assertEqual(context["reviews"], [{"body": "approved"}])
        self.assertEqual(
            context["review_threads"], [{"path": "app.py", "isResolved": True}]
        )
        self.assertEqual(context["closing_issues"], [{"number": 3}])
        self.assertEqual(context["original"]["head_sha"], "head1")

        diff_path = Path(envelope["diff_path"])
        self.assertEqual(diff_path.read_text(encoding="utf-8"), DIFF)

        saved = MODULE.load_state(state_path)
        self.assertEqual(saved["audit"]["iteration_head_sha"], "head1")
        self.assertNotIn("head_sha", saved["audit"])
        self.assertEqual(saved["original"]["head_sha"], "head1")

    def test_rejects_a_dirty_worktree_before_it_reads_github(self):
        self.git_results[("status", "--porcelain=v1")] = " M app.py"

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.preflight(self.directory / "state.json")

        self.assertIn("worktree is not clean", str(error.exception))

    def test_rejects_a_snapshot_that_moved_while_the_diff_was_captured(self):
        moved = dict(self.metadata, head_sha="head2")

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.preflight(
                self.directory / "state.json",
                metadata_sequence=[self.metadata, moved],
            )

        self.assertIn("head_moved", str(error.exception))
        self.assertIn("head2", str(error.exception))

    def test_rejects_a_base_that_moved_while_the_diff_was_captured(self):
        moved = dict(self.metadata, base_sha="base2")

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.preflight(
                self.directory / "state.json",
                metadata_sequence=[self.metadata, moved],
            )

        self.assertIn("head_moved", str(error.exception))
        self.assertIn("base2", str(error.exception))

    def test_a_later_pass_uses_the_cumulative_diff_from_the_pinned_base(self):
        state_path = write_state(self.directory, iterations=1, published_head="audit2")
        self.git_results[("rev-parse", "HEAD")] = "audit2"
        self.branch_state = dict(
            self.branch_state, branch_action="resumed", local_head="audit2"
        )
        self.commits_added = [{"sha": "audit2", "message": "Address audit finding"}]

        with mock.patch.object(
            MODULE, "cumulative_diff", return_value=CUMULATIVE_DIFF
        ) as cumulative:
            envelope = self.preflight(state_path, metadata_sequence=[self.metadata])

        self.calls.cumulative_diff.assert_called_once_with(
            self.directory, "base1", "audit2"
        )
        self.assertEqual(envelope["audit"]["diff_source"], MODULE.CUMULATIVE_GIT_DIFF)
        self.assertEqual(envelope["audit"]["base_sha"], "base1")
        self.assertEqual(envelope["audit"]["local_head"], "audit2")
        self.assertEqual(envelope["audit"]["iteration_head_sha"], "audit2")
        self.assertEqual(envelope["iteration"], 2)
        self.assertEqual(envelope["counts"]["audit_commits"], 1)
        self.assertEqual(
            Path(envelope["diff_path"]).read_text(encoding="utf-8"), CUMULATIVE_DIFF
        )

    def test_a_later_pass_resumes_only_from_the_head_it_published(self):
        state_path = write_state(self.directory, iterations=1, published_head="audit2")
        self.branch_state = dict(
            self.branch_state, branch_action="resumed", local_head="audit2"
        )
        self.git_results[("rev-parse", "HEAD")] = "audit2"

        self.preflight(state_path, metadata_sequence=[self.metadata])

        prepare = self.calls.prepare_audit_branch
        self.assertTrue(prepare.call_args.kwargs["resuming"])
        self.assertEqual(prepare.call_args.kwargs["expected_resume_head"], "audit2")

    def test_a_later_pass_refuses_an_iteration_that_never_published(self):
        state_path = write_state(self.directory, iterations=1)
        before = state_path.read_bytes()

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.preflight(state_path, metadata_sequence=[self.metadata])

        self.assertIn("not 'published'", str(error.exception))
        self.assertEqual(state_path.read_bytes(), before)

    def test_a_later_pass_never_captures_the_pull_request_diff_again(self):
        state_path = write_state(self.directory, iterations=1, published_head="head1")
        self.branch_state = dict(self.branch_state, branch_action="resumed")

        self.preflight(state_path, metadata_sequence=[self.metadata])

        self.calls.pr_diff.assert_not_called()
        self.calls.context.assert_not_called()

    def test_a_force_pushed_head_branch_never_replaces_the_recorded_snapshot(self):
        """A merged pull request's branch can be reused, restacked, or deleted."""
        state_path = write_state(self.directory, iterations=1, published_head="audit2")
        self.branch_state = dict(
            self.branch_state, branch_action="resumed", local_head="audit2"
        )
        self.git_results[("rev-parse", "HEAD")] = "audit2"
        restacked = dict(self.metadata, head_sha="restacked9", base_sha="rebased9")

        envelope = self.preflight(state_path, metadata_sequence=[restacked])
        result = self.full_result(envelope)

        self.assertEqual(envelope["head_sha"], "head1")
        self.assertEqual(envelope["audit"]["head_sha"], "head1")
        self.assertEqual(envelope["audit"]["base_sha"], "base1")
        self.assertTrue(envelope["audit"]["head_ref_moved"])
        self.assertTrue(envelope["audit"]["base_ref_moved"])
        self.assertEqual(result["original"]["head_sha"], "head1")
        self.assertEqual(result["original"]["base_sha"], "base1")
        saved = MODULE.load_state(state_path)
        self.assertEqual(saved["original"]["head_sha"], "head1")

    def test_passes_the_pinned_commits_to_branch_preparation(self):
        self.preflight(self.directory / "state.json")
        prepare = self.calls.prepare_audit_branch

        self.assertEqual(prepare.call_args.kwargs["audit_branch"], "trask-pr-audit-7")
        self.assertEqual(prepare.call_args.kwargs["original_head_sha"], "head1")
        self.assertEqual(prepare.call_args.kwargs["original_base_sha"], "base1")
        self.assertFalse(prepare.call_args.kwargs["resuming"])
        self.assertIsNone(prepare.call_args.kwargs["expected_resume_head"])

    def test_carries_history_forward_and_starts_the_next_iteration(self):
        state_path = write_state(self.directory, published_head="audit1")
        state = MODULE.load_state(state_path)
        state["iterations"] = 1
        state["audit"]["candidates"] = [
            {
                "id": 1,
                "status": "handled",
                "path": "app.py",
                "line": 2,
                "side": "RIGHT",
                "body": "Fix it",
                "commit": "audit1",
                "summary": "Fix it",
            }
        ]
        MODULE.save_state(state_path, state)
        self.branch_state = dict(
            self.branch_state, branch_action="resumed", local_head="audit1"
        )
        self.git_results[("rev-parse", "HEAD")] = "audit1"
        self.commits_added = [{"sha": "audit1", "message": "Address audit finding"}]

        envelope = self.preflight(state_path, metadata_sequence=[self.metadata])
        result = self.full_result(envelope)

        self.assertEqual(envelope["iteration"], 2)
        self.assertEqual(envelope["counts"]["history"], 1)
        self.assertEqual(result["history"][0]["outcome"], "addressed")
        self.assertEqual(
            result["history_commit_presence"],
            [{"history_id": 1, "commit": "audit1", "in_audit_commits": True}],
        )

    def test_reports_a_history_commit_the_audit_branch_no_longer_holds(self):
        state_path = write_state(self.directory, published_head="head1")
        state = MODULE.load_state(state_path)
        state["iterations"] = 1
        state["history"] = [
            {
                "id": 1,
                "iteration": 1,
                "path": "app.py",
                "line": 2,
                "side": "RIGHT",
                "body": "Fix it",
                "outcome": "addressed",
                "detail": None,
                "commit": "gone1",
            }
        ]
        state["audit"]["candidates"] = []
        MODULE.save_state(state_path, state)
        self.branch_state = dict(self.branch_state, branch_action="resumed")

        envelope = self.preflight(state_path, metadata_sequence=[self.metadata])

        self.assertEqual(envelope["counts"]["history_commits_missing"], 1)
        self.assertEqual(
            self.full_result(envelope)["history_commit_presence"],
            [{"history_id": 1, "commit": "gone1", "in_audit_commits": False}],
        )

    def test_stops_at_the_iteration_cap_before_it_touches_anything(self):
        state_path = write_state(
            self.directory, iterations=5, published_head="audit5"
        )
        before = state_path.read_bytes()
        diff_path = MODULE.diff_path_for(state_path)
        diff_path.write_text("earlier diff", encoding="utf-8", newline="")
        preflight_path = MODULE.preflight_path_for(state_path)
        preflight_path.write_text("{}", encoding="utf-8")

        envelope = self.preflight(state_path, metadata_sequence=[])

        self.assertEqual(envelope["result"], "max_iterations_reached")
        self.assertEqual(envelope["iterations"], 5)
        self.assertEqual(envelope["max_iterations"], 5)
        self.assertEqual(envelope["pr"]["number"], 7)
        self.assertEqual(envelope["pr"]["pr_url"], PR_METADATA["pr_url"])
        self.assertEqual(envelope["audit_branch"], "trask-pr-audit-7")
        self.assertEqual(envelope["audit"]["status"], "published")
        self.assertEqual(envelope["audit"]["published_head_sha"], "audit5")
        self.assertEqual(envelope["original_head_sha"], "head1")
        self.assertFalse(envelope["pushed"])
        self.assertNotIn("preflight_path", envelope)
        self.calls.metadata.assert_not_called()
        self.calls.prepare_audit_branch.assert_not_called()
        self.calls.pr_diff.assert_not_called()
        self.calls.cumulative_diff.assert_not_called()
        self.calls.context.assert_not_called()
        self.calls.audit_commits.assert_not_called()
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(diff_path.read_text(encoding="utf-8"), "earlier diff")
        self.assertEqual(preflight_path.read_text(encoding="utf-8"), "{}")

    def test_a_new_invocation_does_not_inherit_the_lifetime_cap(self):
        state_path = write_state(
            self.directory, iterations=5, published_head="audit5"
        )
        self.branch_state = dict(
            self.branch_state, branch_action="resumed", local_head="audit5"
        )

        with mock.patch.object(MODULE.uuid, "uuid4") as token:
            token.return_value.hex = "manual-run"
            envelope = self.preflight(
                state_path,
                metadata_sequence=[self.metadata],
                new_invocation=True,
            )

        self.assertEqual("ready", envelope["result"])
        self.assertEqual(0, envelope["completed_iterations"])
        self.assertEqual("invocation", envelope["budget_scope"])
        self.assertEqual("manual-run", envelope["invocation_run"])

    def test_an_active_invocation_requires_its_token(self):
        state_path = write_state(
            self.directory, iterations=5, published_head="audit5"
        )
        state = MODULE.load_state(state_path)
        state["budget_scope"] = "invocation"
        state["invocation_budget"] = {"run": "manual-run", "baseline": 5}
        MODULE.save_state(state_path, state)

        with self.assertRaisesRegex(MODULE.WorkflowError, "--invocation-run"):
            self.preflight(state_path, metadata_sequence=[])

    def test_a_terminal_clean_audit_is_already_complete(self):
        state_path = write_state(self.directory, iterations=1, published_head="audit1")
        state = MODULE.load_state(state_path)
        state["audit"]["outcome"] = "clean"
        state["audit"]["clean_at_head_sha"] = "audit1"
        MODULE.save_state(state_path, state)
        before = state_path.read_bytes()

        envelope = self.preflight(state_path, metadata_sequence=[])

        self.assertEqual(envelope["result"], "already_complete")
        self.assertEqual(envelope["clean_at_head_sha"], "audit1")
        self.assertEqual(envelope["stage_outcome"], "cleared")
        self.assertEqual(envelope["audit"]["clean_at_head_sha"], "audit1")
        self.calls.metadata.assert_not_called()
        self.calls.prepare_audit_branch.assert_not_called()
        self.calls.audit_commits.assert_not_called()
        self.assertEqual(state_path.read_bytes(), before)

    def test_a_terminal_clean_audit_outranks_the_iteration_cap(self):
        state_path = write_state(self.directory, iterations=5, published_head="audit5")
        state = MODULE.load_state(state_path)
        state["audit"]["outcome"] = "clean"
        state["audit"]["clean_at_head_sha"] = "audit5"
        MODULE.save_state(state_path, state)

        envelope = self.preflight(state_path, metadata_sequence=[])

        self.assertEqual(envelope["result"], "already_complete")


class RecordCommandTest(unittest.TestCase):
    """A recorded commit must stay inside the paths its batch planned."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.emitted = []
        patcher = mock.patch.object(MODULE, "emit", self.emitted.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = write_state(self.directory)
        self.touched = ["app.py"]
        source = self.directory / "candidates.json"
        source.write_text(
            json.dumps(
                [
                    {"path": "app.py", "line": 3, "side": "RIGHT", "body": "Fix it"},
                    {"path": "app.py", "line": 2, "side": "LEFT", "body": "And this"},
                ]
            ),
            encoding="utf-8",
        )
        MODULE.command_candidates(
            SimpleNamespace(state=str(self.path), input=str(source))
        )

    def plan(self, *, batch="b1", candidates=(1,), paths=("app.py",)):
        MODULE.command_plan(
            SimpleNamespace(
                state=str(self.path),
                batch=batch,
                candidates=list(candidates),
                label="Fix it",
                paths=list(paths),
                validation="pytest",
            )
        )

    def fake_git(self, root, *arguments):
        if arguments[:1] == ("rev-parse",):
            return "audit1"
        if arguments[:1] == ("diff-tree",):
            return "\n".join(self.touched)
        raise AssertionError(f"unexpected git call: {arguments}")

    def record(self, *, batch="b1", candidates=(1,), commit="HEAD", rationale=None):
        with (
            mock.patch.object(MODULE, "git", side_effect=self.fake_git),
            mock.patch.object(MODULE, "commit_paths", return_value=self.touched),
        ):
            MODULE.command_record(
                SimpleNamespace(
                    state=str(self.path),
                    batch=batch,
                    candidates=list(candidates),
                    summary="Fix it",
                    commit=commit,
                    rationale=rationale,
                )
            )
        return self.emitted[-1]

    def test_records_a_commit_that_stays_inside_the_planned_paths(self):
        self.plan(paths=("app.py", "docs/app.md"))

        result = self.record()

        self.assertEqual(result["result"], "recorded")
        self.assertEqual(result["commit"], "audit1")
        self.assertEqual(result["commit_paths"], ["app.py"])
        state = MODULE.load_state(self.path)
        self.assertEqual(state["audit"]["candidates"][0]["commit"], "audit1")
        self.assertEqual(state["audit"]["batches"][0]["commit_paths"], ["app.py"])

    def test_refuses_a_commit_that_touches_an_undeclared_path(self):
        self.plan(paths=("app.py",))
        self.touched = ["app.py", "setup.py"]

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.record()

        self.assertIn("does not declare: ['setup.py']", str(error.exception))
        self.assertEqual(
            MODULE.load_state(self.path)["audit"]["candidates"][0]["status"], "pending"
        )

    def test_refuses_a_commit_for_a_batch_that_was_never_planned(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            self.record(batch="ghost")

        self.assertIn("batch 'ghost' was never planned", str(error.exception))

    def test_refuses_a_commit_whose_candidates_are_not_the_planned_ones(self):
        self.plan(candidates=(1,))

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.record(candidates=(1, 2))

        self.assertIn("plans candidates [1], so it cannot record [1, 2]", str(error.exception))

    def test_refuses_a_commit_for_a_batch_that_declared_no_paths(self):
        self.plan(paths=())

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.record()

        self.assertIn("declares no paths", str(error.exception))

    def test_a_no_code_outcome_needs_no_commit_path_check(self):
        self.plan(paths=())

        result = self.record(commit=None, rationale="the caller already handles it")

        self.assertEqual(result["result"], "recorded")
        self.assertIsNone(result["commit"])
        self.assertEqual(result["commit_paths"], [])
        state = MODULE.load_state(self.path)
        self.assertEqual(state["audit"]["candidates"][0]["status"], "handled")


class StateCommandTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.emitted = []
        patcher = mock.patch.object(MODULE, "emit", self.emitted.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = write_state(self.directory)

    def register(self, candidates):
        source = self.directory / "candidates.json"
        source.write_text(json.dumps(candidates), encoding="utf-8")
        MODULE.command_candidates(
            SimpleNamespace(state=str(self.path), input=str(source))
        )
        return self.emitted[-1]

    def test_registers_candidates_anchored_to_the_pinned_diff(self):
        result = self.register(
            [{"path": "app.py", "line": 3, "side": "RIGHT", "body": "Fix it"}]
        )

        self.assertEqual(result["result"], "registered")
        self.assertEqual(result["candidates"][0]["id"], 1)
        self.assertEqual(result["candidates"][0]["status"], "pending")

    def test_rejects_a_candidate_that_is_not_a_changed_line(self):
        with self.assertRaises(MODULE.WorkflowError) as error:
            self.register(
                [{"path": "app.py", "line": 9, "side": "RIGHT", "body": "Fix it"}]
            )

        self.assertIn("not a changed RIGHT line", str(error.exception))

    def test_drop_reads_a_shell_sensitive_rationale_from_a_file(self):
        self.register(
            [{"path": "app.py", "line": 3, "side": "RIGHT", "body": "Fix it"}]
        )
        rationale = self.directory / "rationale.txt"
        rationale.write_text(
            "Decision 2 failed: the caller in `app.py` (line 3) already handles it.",
            encoding="utf-8",
        )

        MODULE.command_drop(
            SimpleNamespace(
                state=str(self.path),
                candidates=[1],
                rationale=None,
                rationale_file=str(rationale),
            )
        )

        state = MODULE.load_state(self.path)
        self.assertEqual(state["audit"]["candidates"][0]["status"], "dropped")
        self.assertIn("Decision 2 failed", state["audit"]["candidates"][0]["rationale"])

    def test_resolve_marks_the_audit_clean_at_the_pinned_audit_head(self):
        with mock.patch.object(
            MODULE,
            "git",
            side_effect=lambda root, *args: {
                ("branch", "--show-current"): "trask-pr-audit-7",
                ("rev-parse", "HEAD"): "head1",
            }[args],
        ):
            MODULE.command_resolve(
                SimpleNamespace(state=str(self.path), outcome="clean")
            )

        state = MODULE.load_state(self.path)
        self.assertEqual(state["audit"]["outcome"], "clean")
        self.assertEqual(state["audit"]["clean_at_head_sha"], "head1")
        self.assertEqual(MODULE.stage_outcome(state), "cleared")

    def test_resolve_refuses_a_moved_audit_head(self):
        with mock.patch.object(
            MODULE,
            "git",
            side_effect=lambda root, *args: {
                ("branch", "--show-current"): "trask-pr-audit-7",
                ("rev-parse", "HEAD"): "audit9",
            }[args],
        ):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.command_resolve(
                    SimpleNamespace(state=str(self.path), outcome="clean")
                )

        self.assertIn("audit head changed before clean resolution", str(error.exception))

    def test_resolve_refuses_a_worktree_on_another_branch(self):
        with mock.patch.object(
            MODULE,
            "git",
            side_effect=lambda root, *args: {
                ("branch", "--show-current"): "main",
                ("rev-parse", "HEAD"): "head1",
            }[args],
        ):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.command_resolve(
                    SimpleNamespace(state=str(self.path), outcome="clean")
                )

        self.assertIn("audit branch mismatch", str(error.exception))

    def test_resolve_requires_no_candidates_or_all_dropped(self):
        self.register(
            [{"path": "app.py", "line": 3, "side": "RIGHT", "body": "Fix it"}]
        )

        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.command_resolve(
                SimpleNamespace(state=str(self.path), outcome="clean")
            )

        self.assertIn("only with no candidates", str(error.exception))

    def test_cleanup_removes_every_file_the_run_wrote(self):
        for path in (
            MODULE.diff_path_for(self.path),
            MODULE.preflight_path_for(self.path),
            MODULE.status_path_for(self.path),
            MODULE.context_path_for(self.path),
        ):
            path.write_text("{}", encoding="utf-8")

        MODULE.command_cleanup(SimpleNamespace(state=str(self.path)))

        self.assertFalse(self.path.exists())
        self.assertFalse(MODULE.context_path_for(self.path).exists())
        self.assertFalse(MODULE.diff_path_for(self.path).exists())


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.emitted = []
        patcher = mock.patch.object(MODULE, "emit", self.emitted.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = write_state(self.directory)
        self.git_results = {
            ("status", "--porcelain=v1"): "",
            ("branch", "--show-current"): "trask-pr-audit-7",
            ("rev-parse", "HEAD"): "audit1",
            ("rev-list", "head1..HEAD"): "audit1",
        }
        self.pushes = []

    def handled(self, commit="audit1"):
        state = MODULE.load_state(self.path)
        state["audit"]["candidates"] = [
            {
                "id": 1,
                "status": "handled",
                "path": "app.py",
                "line": 3,
                "side": "RIGHT",
                "body": "Fix it",
                "commit": commit,
                "summary": "Fix it",
                "batch": "b1",
            }
        ]
        state["audit"]["batches"] = [
            {
                "id": "b1",
                "label": "Fix it",
                "candidate_ids": [1],
                "paths": ["app.py"],
                "validation": "pytest",
                "status": "approved",
            }
        ]
        MODULE.save_state(self.path, state)

    def publish(self, *, remote_heads=None, **overrides):
        remote_heads = remote_heads or [None, "audit1"]

        def push(command, **kwargs):
            self.pushes.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        def paths(root, sha):
            value = self.git_results.get(
                (
                    "diff-tree",
                    "--root",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "-m",
                    sha,
                ),
                "",
            )
            return [path for path in value.splitlines() if path]

        with (
            mock.patch.object(
                MODULE, "git", side_effect=lambda root, *args: self.git_results[args]
            ),
            mock.patch.object(MODULE, "commit_paths", side_effect=paths),
            mock.patch.object(MODULE, "find_remote", return_value="origin"),
            mock.patch.object(MODULE, "remote_head", side_effect=remote_heads),
            mock.patch.object(MODULE, "run", side_effect=push),
            mock.patch.object(MODULE, "time"),
        ):
            MODULE.command_publish(publish_args(self.path, **overrides))
        return self.emitted[-1]

    def test_pushes_only_the_audit_branch_and_verifies_the_remote(self):
        self.handled()

        result = self.publish()

        self.assertEqual(result["result"], "published")
        self.assertEqual(result["branch"], "trask-pr-audit-7")
        self.assertEqual(result["head_sha"], "audit1")
        self.assertTrue(result["pushed"])
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(
            self.pushes[0][-3:],
            ["push", "origin", "HEAD:refs/heads/trask-pr-audit-7"],
        )
        self.assertNotIn("feature", " ".join(self.pushes[0]))
        state = MODULE.load_state(self.path)
        self.assertEqual(state["iterations"], 1)
        self.assertEqual(state["audit"]["status"], "published")

    def test_a_clean_first_pass_pushes_nothing_at_all(self):
        self.git_results[("rev-list", "head1..HEAD")] = ""
        self.git_results[("rev-parse", "HEAD")] = "head1"

        result = self.publish(remote_heads=[])

        self.assertEqual(result["result"], "nothing_to_publish")
        self.assertFalse(result["pushed"])
        self.assertEqual(self.pushes, [])
        self.assertEqual(MODULE.load_state(self.path)["iterations"], 0)

    def test_refuses_to_push_when_the_audit_branch_is_the_pull_requests_branch(self):
        state = MODULE.load_state(self.path)
        state["audit_branch"] = "feature"
        MODULE.save_state(self.path, state)
        self.handled()

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.publish()

        self.assertIn("refusing to push 'feature'", str(error.exception))
        self.assertEqual(self.pushes, [])

    def test_refuses_to_push_from_another_branch(self):
        self.handled()
        self.git_results[("branch", "--show-current")] = "main"

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.publish()

        self.assertIn("refusing to push: local branch is 'main'", str(error.exception))
        self.assertEqual(self.pushes, [])

    def test_refuses_a_dirty_worktree(self):
        self.handled()
        self.git_results[("status", "--porcelain=v1")] = " M app.py"

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.publish()

        self.assertIn("worktree is not clean", str(error.exception))
        self.assertEqual(self.pushes, [])

    def test_refuses_a_commit_this_iteration_never_recorded(self):
        self.handled()
        self.git_results[("rev-list", "head1..HEAD")] = "audit1\nstray1"

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.publish()

        self.assertIn("unrecorded ['stray1']", str(error.exception))

    def test_refuses_to_publish_a_skipped_batch(self):
        state = MODULE.load_state(self.path)
        state["audit"]["candidates"] = [
            {
                "id": 1,
                "status": "skipped",
                "path": "app.py",
                "line": 3,
                "side": "RIGHT",
                "body": "Fix it",
                "rationale": "validation failed",
            }
        ]
        MODULE.save_state(self.path, state)

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.publish()

        self.assertIn("must stop without publishing partial work", str(error.exception))

    def test_refuses_while_a_candidate_is_still_pending(self):
        state = MODULE.load_state(self.path)
        state["audit"]["candidates"] = [
            {
                "id": 1,
                "status": "pending",
                "path": "app.py",
                "line": 3,
                "side": "RIGHT",
                "body": "Fix it",
            }
        ]
        MODULE.save_state(self.path, state)

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.publish()

        self.assertIn("neither dropped nor handled", str(error.exception))

    def test_fails_when_the_remote_branch_does_not_match_the_local_head(self):
        self.handled()

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.publish(remote_heads=[None, "other1", "other1", "other1", "other1"])

        self.assertIn("audit branch mismatch", str(error.exception))

    def test_skips_the_push_when_the_remote_already_matches(self):
        self.handled()

        result = self.publish(remote_heads=["audit1", "audit1"])

        self.assertEqual(result["result"], "published")
        self.assertEqual(self.pushes, [])

    def test_records_the_local_validation_behind_the_push(self):
        self.handled()

        result = self.publish(
            validated=["pytest tests/test_app.py"], rewrote=["black app.py"]
        )

        self.assertEqual(
            result["local_validation"],
            {
                "head_sha": "audit1",
                "status": "passed",
                "commands": ["pytest tests/test_app.py", "black app.py"],
                "rewrote": ["black app.py"],
            },
        )

    def test_publishes_a_run_that_could_not_validate_the_old_snapshot(self):
        self.handled()

        result = self.publish(not_validated="the 2019 toolchain is unavailable")

        self.assertEqual(
            result["local_validation"],
            {
                "head_sha": "audit1",
                "status": "skipped",
                "reason": "the 2019 toolchain is unavailable",
            },
        )
        self.assertEqual(len(self.pushes), 1)

    def test_reports_the_checks_that_ran_next_to_the_ones_that_could_not(self):
        self.handled()

        result = self.publish(
            validated=["mvn -pl module test"],
            not_validated="the integration tests need a service this machine has no access to",
        )

        self.assertEqual(
            result["local_validation"],
            {
                "head_sha": "audit1",
                "status": "partial",
                "commands": ["mvn -pl module test"],
                "rewrote": [],
                "reason": (
                    "the integration tests need a service this machine has no "
                    "access to"
                ),
            },
        )
        self.assertEqual(len(self.pushes), 1)

    def test_accepts_a_validation_commit_that_rewrote_planned_paths(self):
        self.handled()
        self.git_results[("rev-list", "head1..HEAD")] = "format1\naudit1"
        self.git_results[("rev-parse", "HEAD")] = "format1"
        self.git_results[("rev-parse", "format1")] = "format1"
        self.git_results[
            ("diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-m", "format1")
        ] = "app.py"

        result = self.publish(
            remote_heads=[None, "format1"],
            validated=["black app.py"],
            rewrote=["black app.py"],
            validation_commit=["format1"],
        )

        self.assertEqual(result["result"], "published")
        self.assertEqual(result["commits"], ["audit1", "format1"])
        self.assertEqual(result["validation_commits"], ["format1"])
        self.assertEqual(result["iteration_head_sha"], "head1")
        self.assertEqual(
            result["local_validation"],
            {
                "head_sha": "format1",
                "status": "passed",
                "commands": ["black app.py"],
                "rewrote": ["black app.py"],
                "validation_commits": ["format1"],
            },
        )
        state = MODULE.load_state(self.path)
        self.assertEqual(state["audit"]["validation_commits"], ["format1"])

    def test_refuses_a_validation_commit_that_touches_an_unplanned_path(self):
        self.handled()
        self.git_results[("rev-list", "head1..HEAD")] = "format1\naudit1"
        self.git_results[("rev-parse", "HEAD")] = "format1"
        self.git_results[("rev-parse", "format1")] = "format1"
        self.git_results[
            ("diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-m", "format1")
        ] = "app.py\nunrelated.py"

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.publish(validation_commit=["format1"])

        self.assertIn("any planned batch does not declare", str(error.exception))
        self.assertIn("unrelated.py", str(error.exception))
        self.assertEqual(self.pushes, [])

    def test_refuses_a_validation_commit_from_outside_this_iteration(self):
        self.handled()
        self.git_results[("rev-parse", "older9")] = "older9"

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.publish(validation_commit=["older9"])

        self.assertIn("is not one of this iteration's commits", str(error.exception))
        self.assertIn("head1..HEAD", str(error.exception))
        self.assertEqual(self.pushes, [])

    def test_a_rewrite_commit_nobody_declared_is_still_unrecorded(self):
        self.handled()
        self.git_results[("rev-list", "head1..HEAD")] = "format1\naudit1"
        self.git_results[("rev-parse", "HEAD")] = "format1"

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.publish()

        self.assertIn("unrecorded ['format1']", str(error.exception))
        self.assertEqual(self.pushes, [])

    def test_still_requires_every_recorded_candidate_commit(self):
        self.handled()
        self.git_results[("rev-list", "head1..HEAD")] = "format1"
        self.git_results[("rev-parse", "HEAD")] = "format1"
        self.git_results[("rev-parse", "format1")] = "format1"
        self.git_results[
            ("diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-m", "format1")
        ] = "app.py"

        with self.assertRaises(MODULE.WorkflowError) as error:
            self.publish(validation_commit=["format1"])

        self.assertIn("missing ['audit1']", str(error.exception))
        self.assertEqual(self.pushes, [])


class LocalValidationRecordTest(unittest.TestCase):
    def entry(self, *, validation_commits=None, **overrides):
        arguments = {"validated": None, "not_validated": None, "rewrote": None}
        arguments.update(overrides)
        return MODULE.local_validation_entry(
            SimpleNamespace(**arguments), "audit1", validation_commits
        )

    def test_records_the_commands_that_ran(self):
        self.assertEqual(
            self.entry(validated=["pytest"]),
            {
                "head_sha": "audit1",
                "status": "passed",
                "commands": ["pytest"],
                "rewrote": [],
            },
        )

    def test_a_rewriting_command_counts_as_one_that_ran(self):
        self.assertEqual(
            self.entry(rewrote=["black ."]),
            {
                "head_sha": "audit1",
                "status": "passed",
                "commands": ["black ."],
                "rewrote": ["black ."],
            },
        )

    def test_records_a_partial_report_as_neither_passed_nor_skipped(self):
        self.assertEqual(
            self.entry(validated=["pytest"], not_validated="the linter is gone"),
            {
                "head_sha": "audit1",
                "status": "partial",
                "commands": ["pytest"],
                "rewrote": [],
                "reason": "the linter is gone",
            },
        )

    def test_records_the_commits_that_carry_the_rewrites(self):
        self.assertEqual(
            self.entry(
                validated=["black ."],
                rewrote=["black ."],
                validation_commits=["format1"],
            ),
            {
                "head_sha": "audit1",
                "status": "passed",
                "commands": ["black ."],
                "rewrote": ["black ."],
                "validation_commits": ["format1"],
            },
        )

    def test_records_that_the_publication_claimed_nothing(self):
        self.assertEqual(
            self.entry(), {"head_sha": "audit1", "status": "unreported"}
        )


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.emitted = []
        patcher = mock.patch.object(MODULE, "emit", self.emitted.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = write_state(self.directory)

    def test_writes_the_complete_snapshot_and_emits_a_compact_envelope(self):
        MODULE.command_status(
            SimpleNamespace(state=str(self.path), current=False, repo_root=None)
        )
        envelope = self.emitted[-1]
        complete = json.loads(
            Path(envelope["status_path"]).read_text(encoding="utf-8")
        )

        self.assertEqual(envelope["result"], "ready")
        self.assertEqual(envelope["pr"]["number"], 7)
        self.assertEqual(envelope["pr"]["state"], "MERGED")
        self.assertEqual(envelope["audit"]["branch"], "trask-pr-audit-7")
        self.assertEqual(envelope["audit"]["iteration_head_sha"], "head1")
        self.assertIsNone(envelope["audit"]["published_head_sha"])
        self.assertNotIn("head_sha", envelope["audit"])
        self.assertEqual(envelope["audit"]["diff_source"], MODULE.GITHUB_PR_DIFF)
        self.assertEqual(envelope["counts"]["changed_files"], 1)
        self.assertNotIn("stage_outcome", envelope)
        self.assertEqual(complete["original"]["head_sha"], "head1")
        self.assertEqual(complete["audit"]["anchors"]["app.py"]["RIGHT"], [2, 3])

    def test_finds_the_state_through_the_checked_out_audit_branch(self):
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(
                MODULE, "resolve_repo_root", return_value=self.directory
            ),
            mock.patch.object(MODULE, "current_branch", return_value="trask-pr-audit-7"),
            mock.patch.object(
                MODULE, "repo_name_from_remotes", return_value="owner/repo"
            ),
        ):
            MODULE.command_status(
                SimpleNamespace(
                    state=None, current=True, repo_root=str(self.directory)
                )
            )

        envelope = self.emitted[-1]
        self.assertEqual(envelope["result"], "no_state")
        self.assertEqual(envelope["pr"]["number"], 7)
        self.assertEqual(
            envelope["state"],
            str(MODULE.default_state_path(MODULE.parse_target("owner/repo#7"))),
        )

    def test_refuses_to_guess_the_pull_request_from_another_branch(self):
        with (
            mock.patch.object(MODULE, "require_tools"),
            mock.patch.object(
                MODULE, "resolve_repo_root", return_value=self.directory
            ),
            mock.patch.object(MODULE, "current_branch", return_value="main"),
        ):
            with self.assertRaises(MODULE.WorkflowError) as error:
                MODULE.command_status(
                    SimpleNamespace(
                        state=None, current=True, repo_root=str(self.directory)
                    )
                )

        self.assertIn("is not an audit branch", str(error.exception))

    def test_reports_cleared_only_after_a_recorded_clean_audit(self):
        state = MODULE.load_state(self.path)
        self.assertIsNone(MODULE.stage_outcome(state))
        state["audit"]["outcome"] = "clean"
        state["audit"]["clean_at_head_sha"] = "head1"
        self.assertEqual(MODULE.stage_outcome(state), "cleared")
        state["audit"]["clean_at_head_sha"] = ""
        self.assertIsNone(MODULE.stage_outcome(state))


class HistoryTest(unittest.TestCase):
    def test_maps_candidate_status_to_history_outcome(self):
        self.assertEqual(
            MODULE.history_outcome({"status": "handled", "commit": "audit1"}),
            "addressed",
        )
        self.assertEqual(
            MODULE.history_outcome({"status": "handled", "commit": None}), "no_code"
        )
        self.assertEqual(MODULE.history_outcome({"status": "dropped"}), "dropped")
        self.assertEqual(MODULE.history_outcome({"status": "pending"}), "unresolved")

    def test_archives_only_resolved_candidates(self):
        state = {
            "history": [],
            "audit": {
                "iteration": 1,
                "candidates": [
                    {
                        "id": 1,
                        "status": "handled",
                        "path": "app.py",
                        "line": 3,
                        "side": "RIGHT",
                        "body": "Fix it",
                        "commit": "audit1",
                        "summary": "Fix it",
                    },
                    {
                        "id": 2,
                        "status": "pending",
                        "path": "app.py",
                        "line": 2,
                        "side": "LEFT",
                        "body": "Maybe",
                    },
                ],
            },
        }

        MODULE.archive_audit(state)

        self.assertEqual([entry["id"] for entry in state["history"]], [1])
        self.assertEqual(state["history"][0]["outcome"], "addressed")


class RealGitBranchTest(unittest.TestCase):
    """Prove the branch mechanics against real git, not against mocked calls."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git") is None:
            raise unittest.SkipTest("git is not installed")
        cls.template = tempfile.TemporaryDirectory()
        root = Path(cls.template.name).resolve()
        origin = root / "origin.git"
        work = root / "work"
        cls.git(root, "init", "--bare", "--initial-branch=main", str(origin))
        # A bare repository refuses to serve a bare SHA unless it is told to,
        # and fetching the merged head by SHA is the whole point of the fixture.
        MODULE.run(
            [
                "git",
                "config",
                "--file",
                str(origin / "config"),
                "uploadpack.allowAnySHA1InWant",
                "true",
            ]
        )
        cls.git(root, "init", "--initial-branch=main", str(work))
        for key, value in (
            ("user.name", "Audit Test"),
            ("user.email", "audit@example.com"),
            ("commit.gpgsign", "false"),
        ):
            cls.git(work, "config", key, value)
        cls.base_sha = cls.commit_in(work, "base.txt", "base")
        cls.git(work, "remote", "add", "origin", str(origin))
        cls.git(work, "push", "-u", "origin", "main")
        cls.git(work, "checkout", "-b", "feature", cls.base_sha)
        cls.head_sha = cls.commit_in(work, "feature.txt", "merged work")
        cls.git(work, "push", "origin", "feature")
        cls.git(work, "checkout", "main")
        cls.moved_main = cls.commit_in(work, "later.txt", "moved on after the merge")
        cls.git(work, "push", "origin", "main")
        cls.git(work, "checkout", "-b", "trask-pr-audit-7", "origin/main")

    @classmethod
    def tearDownClass(cls):
        cls.template.cleanup()

    @classmethod
    def git(cls, cwd, *arguments):
        return MODULE.run(["git", "-C", str(cwd), *arguments]).stdout.strip()

    @classmethod
    def commit_in(cls, work, name, text):
        (work / name).write_text(text, encoding="utf-8")
        cls.git(work, "add", name)
        cls.git(work, "commit", "-m", f"Add {name}")
        return cls.git(work, "rev-parse", "HEAD")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "fixture"
        shutil.copytree(self.template.name, self.root)
        self.origin = self.root / "origin.git"
        self.work = self.root / "work"
        self.git(self.work, "remote", "set-url", "origin", str(self.origin))

    def commit(self, name, text):
        return self.commit_in(self.work, name, text)

    def test_a_fresh_branch_at_the_remote_tip_holds_no_unique_work(self):
        self.assertEqual(MODULE.unique_local_work(self.work, "origin/main"), [])

    def test_a_branch_with_its_own_commit_is_reported_as_unique_work(self):
        own = self.commit("mine.txt", "unrelated session work")

        self.assertEqual(MODULE.unique_local_work(self.work, "origin/main"), [own])

    def test_realigning_moves_the_branch_onto_the_historical_head(self):
        MODULE.realign_branch(self.work, "trask-pr-audit-7", self.head_sha)

        self.assertEqual(MODULE.current_branch(self.work), "trask-pr-audit-7")
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), self.head_sha)
        self.assertEqual(
            self.git(self.work, "rev-parse", "trask-pr-audit-7"), self.head_sha
        )
        self.assertTrue((self.work / "feature.txt").is_file())
        self.assertFalse((self.work / "later.txt").exists())
        self.assertEqual(self.git(self.work, "status", "--porcelain=v1"), "")

    def test_realigning_refuses_to_discard_an_uncommitted_change(self):
        (self.work / "later.txt").write_text("edited", encoding="utf-8")

        with self.assertRaises(MODULE.WorkflowError):
            MODULE.realign_branch(self.work, "trask-pr-audit-7", self.head_sha)

        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), self.moved_main)

    def test_fetches_the_merged_head_by_its_exact_sha(self):
        clone = self.root / "clone"
        MODULE.run(
            [
                "git",
                "clone",
                "--single-branch",
                "--branch",
                "main",
                self.origin.as_uri(),
                str(clone),
            ]
        )
        self.assertFalse(MODULE.commit_present(clone, self.head_sha))

        MODULE.fetch_commit(clone, "origin", self.head_sha, None)

        self.assertTrue(MODULE.commit_present(clone, self.head_sha))

    def test_the_audit_branch_name_matches_what_the_helper_expects(self):
        self.assertEqual(MODULE.current_branch(self.work), MODULE.audit_branch_name(7))
        self.assertTrue(MODULE.local_branch_exists(self.work, "trask-pr-audit-7"))
        self.assertFalse(MODULE.local_branch_exists(self.work, "trask-pr-audit-8"))

    def test_the_starting_point_reference_resolves_the_tracked_remote_branch(self):
        self.assertEqual(
            MODULE.starting_point_reference(
                self.work, remote="origin", branch="main", base_branch="main"
            ),
            "origin/main",
        )

    def test_reads_the_paths_a_commit_touches(self):
        (self.work / "one.txt").write_text("one", encoding="utf-8")
        (self.work / "two.txt").write_text("two", encoding="utf-8")
        self.git(self.work, "add", "one.txt", "two.txt")
        self.git(self.work, "commit", "-m", "Add two files")
        sha = self.git(self.work, "rev-parse", "HEAD")

        self.assertEqual(MODULE.commit_paths(self.work, sha), ["one.txt", "two.txt"])

    def test_reads_the_paths_of_a_repositorys_very_first_commit(self):
        self.assertEqual(MODULE.commit_paths(self.work, self.base_sha), ["base.txt"])

    def test_reads_non_ascii_commit_paths_without_gits_quote_escaping(self):
        path = "caf\u00e9.py"
        (self.work / path).write_text("value = 1\n", encoding="utf-8")
        self.git(self.work, "add", path)
        self.git(self.work, "commit", "-m", "Add unicode path")
        sha = self.git(self.work, "rev-parse", "HEAD")

        self.assertEqual(MODULE.commit_paths(self.work, sha), [path])
        self.assertEqual(
            MODULE.require_declared_commit_paths(
                self.work, sha, [path], label="batch 'b1'"
            ),
            [path],
        )

    def test_refuses_a_commit_that_leaves_the_declared_paths(self):
        (self.work / "one.txt").write_text("one", encoding="utf-8")
        (self.work / "two.txt").write_text("two", encoding="utf-8")
        self.git(self.work, "add", "one.txt", "two.txt")
        self.git(self.work, "commit", "-m", "Add two files")
        sha = self.git(self.work, "rev-parse", "HEAD")

        self.assertEqual(
            MODULE.require_declared_commit_paths(
                self.work, sha, ["one.txt", "two.txt"], label="batch 'b1'"
            ),
            ["one.txt", "two.txt"],
        )
        with self.assertRaises(MODULE.WorkflowError) as error:
            MODULE.require_declared_commit_paths(
                self.work, sha, ["one.txt"], label="batch 'b1'"
            )

        self.assertIn("batch 'b1' does not declare: ['two.txt']", str(error.exception))


class ParserTest(unittest.TestCase):
    def test_publish_accepts_every_validation_flag_the_agent_names(self):
        parser = MODULE.build_parser()
        arguments = parser.parse_args(
            [
                "publish",
                "--state",
                "state.json",
                "--validated",
                "pytest",
                "--rewrote",
                "black .",
                "--validation-commit",
                "abc123",
                "--validation-commit",
                "def456",
            ]
        )

        self.assertEqual(arguments.validated, ["pytest"])
        self.assertEqual(arguments.rewrote, ["black ."])
        self.assertEqual(arguments.validation_commit, ["abc123", "def456"])

    def test_publish_accepts_a_partial_validation_report(self):
        parser = MODULE.build_parser()
        arguments = parser.parse_args(
            [
                "publish",
                "--state",
                "state.json",
                "--validated",
                "pytest",
                "--not-validated",
                "the linter is gone",
            ]
        )

        self.assertEqual(arguments.validated, ["pytest"])
        self.assertEqual(arguments.not_validated, "the linter is gone")

    def test_publish_defaults_every_validation_flag_to_nothing(self):
        parser = MODULE.build_parser()
        arguments = parser.parse_args(["publish", "--state", "state.json"])

        self.assertIsNone(arguments.validated)
        self.assertIsNone(arguments.not_validated)
        self.assertIsNone(arguments.rewrote)
        self.assertIsNone(arguments.validation_commit)

    def test_preflight_defaults_to_five_iterations(self):
        parser = MODULE.build_parser()
        arguments = parser.parse_args(["preflight", "owner/repo#7"])

        self.assertEqual(arguments.max_iterations, MODULE.DEFAULT_MAX_ITERATIONS)
        self.assertEqual(MODULE.DEFAULT_MAX_ITERATIONS, 5)
        self.assertFalse(arguments.new_invocation)
        self.assertIsNone(arguments.invocation_run)

        fresh = parser.parse_args(
            ["preflight", "owner/repo#7", "--new-invocation"]
        )
        self.assertTrue(fresh.new_invocation)
        resumed = parser.parse_args(
            [
                "preflight",
                "owner/repo#7",
                "--invocation-run",
                "manual-run",
            ]
        )
        self.assertEqual("manual-run", resumed.invocation_run)


if __name__ == "__main__":
    unittest.main()
