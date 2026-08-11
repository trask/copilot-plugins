import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "pr_reviewer.py"
AGENT = Path(__file__).parents[1] / "agents" / "pr-reviewer.agent.md"
SPEC = importlib.util.spec_from_file_location("pr_reviewer", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

DIFF = """\
diff --git a/src/one.py b/src/one.py
index 1111111..2222222 100644
--- a/src/one.py
+++ b/src/one.py
@@ -1,4 +1,5 @@
 context
-old two
+new two
 context three
+new four
 context five
@@ -20,2 +21,2 @@ later
-old twenty
+new twenty-one
 context
diff --git a/docs/two.md b/docs/two.md
index 3333333..4444444 100644
--- a/docs/two.md
+++ b/docs/two.md
@@ -10,3 +10,2 @@
 context
-removed eleven
 context
"""


class AgentInstructionsTest(unittest.TestCase):
    def test_renames_the_session_from_check_metadata(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("tools: [read, search, execute, agent, rename_session]", instructions)
        self.assertIn("## Session Naming", instructions)
        self.assertIn(
            "Call `rename_session` exactly once per run",
            instructions,
        )
        self.assertIn(
            "call `rename_session` with `PR Review: <PR number> - <PR title>` "
            "from its `pr_number` and `pr_title` fields",
            instructions,
        )
        self.assertIn("Never use an interim number-only name", instructions)
        # rename_session only replaces an auto-generated name; a second call is skipped.
        self.assertNotIn("call `rename_session` again", instructions)
        self.assertNotIn("immediately call `rename_session`", instructions)

    def test_bare_pr_reference_starts_the_review(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("## Activation: Bare PR References Start The Review", instructions)
        self.assertIn("a message containing only a PR URL", instructions)
        self.assertIn("bare PR number (such as `123` or `#123`)", instructions)
        self.assertIn(
            "combine it with the current workspace's GitHub repository as `owner/repo#number`",
            instructions,
        )
        self.assertIn("Do not ask what action the user wants", instructions)
        self.assertIn("defer to the generic `github-pr-diff-review` skill", instructions)

    def test_is_manual_only_and_requires_independent_candidate_checks(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("name: PR Reviewer", instructions)
        self.assertIn("user-invocable: true", instructions)
        self.assertIn("disable-model-invocation: true", instructions)
        self.assertIn("gh pr diff", instructions)
        self.assertIn("GPT-5.6 Sol", instructions)
        self.assertIn("reasoning effort **max**", instructions)
        self.assertIn("for **each candidate separately**", instructions)
        self.assertIn("Never add a top-level review body", instructions)
        self.assertIn("Skip local tests by default", instructions)
        self.assertIn("report every dropped candidate", instructions)
        self.assertIn("If no candidates survive", instructions)
        self.assertIn(
            "every suppressed Copilot comment returned by `check`", instructions
        )
        self.assertIn("latest completed, non-dismissed Copilot review", instructions)
        self.assertIn("investigate every entry in `suppressed_comments`", instructions)
        self.assertIn("Deduplicate candidates", instructions)
        self.assertIn(
            "applies equally to candidates discovered directly and candidates derived "
            "from suppressed Copilot comments",
            instructions,
        )
        self.assertIn("derive an honest changed-line anchor", instructions)
        self.assertIn("fails rather than silently omitting them", instructions)

    def test_requires_a_claude_model_gate_before_any_review_work(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("## Model Gate", instructions)
        self.assertIn("Run only on a Claude model", instructions)
        self.assertIn("Clear the **Model Gate**", instructions)
        self.assertIn("positively a Claude model", instructions)
        self.assertIn("before `check` and before fetching any pull request data", instructions)
        self.assertIn("inability to determine the model as a failed gate", instructions)
        self.assertIn("only when the user explicitly confirms", instructions)
        self.assertIn("are never that confirmation", instructions)

    def test_requires_one_recorded_head_snapshot_for_analysis_and_posting(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("record its `head_sha` as the immutable review snapshot", instructions)
        self.assertIn("Analyze only that snapshot", instructions)
        self.assertIn(
            "`post <target> --expected-head <recorded-head_sha> --comments <file-or->`",
            instructions,
        )
        self.assertIn("restart the entire review from `check`", instructions)
        self.assertIn("never translate or re-anchor old findings", instructions)

    def test_closes_every_run_with_a_categorized_retrospective(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("## Retrospective", instructions)
        self.assertIn(
            "Silence is the normal outcome, and a run that went smoothly reports "
            "nothing",
            instructions,
        )
        self.assertIn(
            "Produce the retrospective on every terminal outcome, including "
            "`existing_pending_review`, a review with no findings, a helper error, "
            "and a failed **Model Gate**",
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
        self.assertIn("The retrospective is advisory and chat-only", instructions)
        self.assertIn(
            "never turn it into a review comment or any other GitHub mutation",
            instructions,
        )
        self.assertIn(
            "omit the label entirely when there is nothing to report", instructions
        )
        self.assertIn(
            "never replaces, reorders, or alters the required final response",
            instructions,
        )


class ParseTargetTest(unittest.TestCase):
    def test_parses_url_and_short_target(self):
        url = MODULE.parse_target("https://github.com/owner/repo/pull/42/")
        short = MODULE.parse_target("owner/repo#42")

        self.assertEqual(url, short)
        self.assertEqual(url["repo_name"], "owner/repo")
        self.assertEqual(url["number"], 42)

    def test_parses_a_raw_url_with_a_fragment(self):
        target = MODULE.parse_target(
            "https://github.com/owner/repo/pull/42#pullrequestreview-7"
        )

        self.assertEqual(target, MODULE.parse_target("owner/repo#42"))

    def test_rejects_non_pr_target(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "GitHub PR URL"):
            MODULE.parse_target("42")


class UnifiedDiffTest(unittest.TestCase):
    def test_parses_multiple_files_and_hunks(self):
        anchors = MODULE.parse_unified_diff(DIFF)

        self.assertEqual(set(anchors), {"src/one.py", "docs/two.md"})
        self.assertEqual(anchors["src/one.py"]["RIGHT"], {2, 4, 21})
        self.assertEqual(anchors["src/one.py"]["LEFT"], {2, 20})
        self.assertEqual(anchors["docs/two.md"]["LEFT"], {11})
        self.assertEqual(anchors["docs/two.md"]["RIGHT"], set())

    def test_parses_added_and_deleted_files(self):
        diff = """\
diff --git a/new.txt b/new.txt
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+one
+two
diff --git a/old.txt b/old.txt
--- a/old.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-one
-two
"""

        anchors = MODULE.parse_unified_diff(diff)

        self.assertEqual(anchors["new.txt"]["RIGHT"], {1, 2})
        self.assertEqual(anchors["old.txt"]["LEFT"], {1, 2})

    def test_fetches_the_authoritative_gh_pr_diff(self):
        pr = {
            "repo_name": "owner/repo",
            "pr_url": "https://github.com/owner/repo/pull/42",
        }
        completed = mock.Mock(stdout=DIFF)

        with mock.patch.object(MODULE, "run", return_value=completed) as run:
            result = MODULE.fetch_authoritative_diff(pr)

        self.assertEqual(result, DIFF)
        run.assert_called_once_with(
            [
                "gh",
                "pr",
                "diff",
                "https://github.com/owner/repo/pull/42",
                "--repo",
                "owner/repo",
            ]
        )

    def test_decodes_git_quoted_utf8_paths(self):
        diff = """\
diff --git "a/docs/\\303\\251.md" "b/docs/\\303\\251.md"
--- "a/docs/\\303\\251.md"
+++ "b/docs/\\303\\251.md"
@@ -0,0 +1 @@
+new
"""

        anchors = MODULE.parse_unified_diff(diff)

        self.assertEqual(anchors["docs/é.md"]["RIGHT"], {1})

    def test_unicode_line_separator_stays_within_changed_line(self):
        diff = (
            "diff --git a/data.txt b/data.txt\r\n"
            "--- a/data.txt\r\n"
            "+++ b/data.txt\r\n"
            "@@ -1 +1,2 @@\r\n"
            "-old\u2028value\r\n"
            "+new\u2028value\r\n"
            "+controls\vand\fcontent\r\n"
        )

        anchors = MODULE.parse_unified_diff(diff)

        self.assertEqual(anchors["data.txt"]["LEFT"], {1})
        self.assertEqual(anchors["data.txt"]["RIGHT"], {1, 2})


class CommentValidationTest(unittest.TestCase):
    def setUp(self):
        self.anchors = MODULE.parse_unified_diff(DIFF)

    def test_accepts_valid_right_and_left_anchors(self):
        comments = [
            {"path": "src/one.py", "line": 4, "side": "RIGHT", "body": "Fix this."},
            {"path": "docs/two.md", "line": 11, "side": "LEFT", "body": "Keep this."},
        ]

        self.assertEqual(MODULE.validate_comments(comments, self.anchors), comments)

    def test_rejects_context_anchor(self):
        comment = {
            "path": "src/one.py",
            "line": 3,
            "side": "RIGHT",
            "body": "Context is not valid.",
        }

        with self.assertRaisesRegex(MODULE.WorkflowError, "not a changed RIGHT line"):
            MODULE.validate_comments([comment], self.anchors)

    def test_rejects_wrong_side(self):
        comment = {
            "path": "src/one.py",
            "line": 4,
            "side": "LEFT",
            "body": "Wrong side.",
        }

        with self.assertRaisesRegex(MODULE.WorkflowError, "not a changed LEFT line"):
            MODULE.validate_comments([comment], self.anchors)

    def test_rejects_out_of_diff_path(self):
        comment = {
            "path": "src/missing.py",
            "line": 1,
            "side": "RIGHT",
            "body": "Missing.",
        }

        with self.assertRaisesRegex(MODULE.WorkflowError, "not a changed RIGHT line"):
            MODULE.validate_comments([comment], self.anchors)

    def test_rejects_empty_comments_and_bodies(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "at least one"):
            MODULE.validate_comments([], self.anchors)
        with self.assertRaisesRegex(MODULE.WorkflowError, "must not be empty"):
            MODULE.validate_comments(
                [
                    {
                        "path": "src/one.py",
                        "line": 2,
                        "side": "RIGHT",
                        "body": "  ",
                    }
                ],
                self.anchors,
            )


class SuppressedCommentTest(unittest.TestCase):
    def test_parses_current_and_low_confidence_headings(self):
        body = """
<details>
<summary>Show a summary per file</summary>

**ignored.py:1**
* Not a suppressed comment.
</details>
<details>
<summary>Comments suppressed due to low confidence (1)</summary>

**src/first.py:12**
* Preserve this fallback.
</details>
<details>
<summary>Suppressed comments (1)</summary>

**src/second.py:7**
* Add coverage for this branch.
```python
assert result
```
</details>
"""

        self.assertEqual(
            MODULE.parse_suppressed_comments(body),
            [
                {
                    "path": "src/first.py",
                    "line": 12,
                    "body": "Preserve this fallback.",
                },
                {
                    "path": "src/second.py",
                    "line": 7,
                    "body": "Add coverage for this branch.\n"
                    "```python\nassert result\n```",
                },
            ],
        )

    def test_rejects_missing_declared_count(self):
        body = """
<details><summary>Suppressed comments</summary>
**src/first.py:12**
* Preserve this fallback.
</details>
"""

        with self.assertRaisesRegex(MODULE.WorkflowError, "no declared count"):
            MODULE.parse_suppressed_comments(body)

    def test_rejects_declared_count_mismatch(self):
        body = """
<details><summary>Suppressed comments (2)</summary>
**src/first.py:12**
* Preserve this fallback.
</details>
"""

        with self.assertRaisesRegex(MODULE.WorkflowError, "count mismatch"):
            MODULE.parse_suppressed_comments(body)

    def test_rejects_empty_comment_body(self):
        body = """
<details><summary>Suppressed comments (1)</summary>
**src/first.py:12**
</details>
"""

        with self.assertRaisesRegex(MODULE.WorkflowError, "empty body"):
            MODULE.parse_suppressed_comments(body)

    def test_rejects_unrecognized_suppressed_layout(self):
        body = """
### Suppressed comments (1)

**src/first.py:12**
* Preserve this fallback.
"""

        with self.assertRaisesRegex(MODULE.WorkflowError, "recognized details block"):
            MODULE.parse_suppressed_comments(body)

    def test_rejects_nonpositive_line(self):
        body = """
<details><summary>Suppressed comments (1)</summary>
**src/first.py:0**
* Preserve this fallback.
</details>
"""

        with self.assertRaisesRegex(MODULE.WorkflowError, "invalid location"):
            MODULE.parse_suppressed_comments(body)

    def test_latest_completed_copilot_review_requires_exact_head(self):
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
            {
                "id": 999,
                "commit_id": "head",
                "submitted_at": "2026-08-09T12:04:00Z",
                "state": "COMMENTED",
                "user": {"login": "human"},
            },
            {
                "id": 104,
                "commit_id": "head",
                "submitted_at": "2026-08-09T12:05:00Z",
                "state": "COMMENTED",
                "user": {"login": "copilot-pull-request-reviewer"},
            },
        ]

        self.assertEqual(
            MODULE.latest_copilot_review_for_head(reviews, "head")["id"], 104
        )

    def test_extracts_suppressed_comments_from_latest_exact_head_review(self):
        reviews = [
            {
                "id": 10,
                "commit_id": "head",
                "submitted_at": "2026-08-09T12:00:00Z",
                "state": "COMMENTED",
                "html_url": "https://example.test/review/10",
                "body": """
<details><summary>Suppressed comments (1)</summary>
**src/one.py:2**
* Preserve the old behavior.
</details>
""",
                "user": {"login": "copilot-pull-request-reviewer[bot]"},
            }
        ]

        review, comments = MODULE.suppressed_comments_for_head(reviews, "head")

        self.assertEqual(
            review, {"id": 10, "url": "https://example.test/review/10"}
        )
        self.assertEqual(
            comments,
            [
                {
                    "path": "src/one.py",
                    "line": 2,
                    "body": "Preserve the old behavior.",
                }
            ],
        )

    def test_returns_empty_without_an_exact_head_copilot_review(self):
        reviews = [
            {
                "id": 10,
                "commit_id": "old-head",
                "submitted_at": "2026-08-09T12:00:00Z",
                "state": "COMMENTED",
                "user": {"login": "copilot-pull-request-reviewer[bot]"},
            }
        ]

        result = MODULE.suppressed_comments_for_head(reviews, "head")

        self.assertEqual(result, (None, []))


class PendingReviewTest(unittest.TestCase):
    def test_refuses_existing_viewer_owned_pending_review(self):
        pr = {
            "repo_name": "owner/repo",
            "number": 42,
            "pr_url": "https://github.com/owner/repo/pull/42",
        }
        reviews = [
            {
                "id": 7,
                "state": "PENDING",
                "html_url": f"{pr['pr_url']}#pullrequestreview-7",
                "user": {"login": "Viewer"},
            }
        ]

        pending = MODULE.find_pending_review(reviews, "viewer")

        self.assertEqual(pending["id"], 7)

    def test_fetches_paginated_reviews_once(self):
        pr = {
            "repo_name": "owner/repo",
            "number": 42,
        }

        with mock.patch.object(MODULE, "gh_paginated", return_value=[]) as paginated:
            self.assertEqual(MODULE.fetch_reviews(pr), [])

        paginated.assert_called_once_with(
            "repos/owner/repo/pulls/42/reviews?per_page=100"
        )

    def test_check_returns_existing_pending_review_without_fetching_diff(self):
        pr = {
            "repo_name": "owner/repo",
            "number": 42,
            "pr_url": "https://github.com/owner/repo/pull/42",
            "head_sha": "abc",
        }
        pending_url = f"{pr['pr_url']}#pullrequestreview-7"

        with (
            mock.patch.object(
                MODULE,
                "preflight",
                return_value=(pr, "viewer", {}, pending_url, None, []),
            ),
            mock.patch.object(MODULE, "emit") as emit,
        ):
            MODULE.command_check(SimpleNamespace(target=pr["pr_url"]))

        self.assertEqual(emit.call_args.args[0]["result"], "existing_pending_review")
        self.assertEqual(
            emit.call_args.args[0]["review_url"],
            f"{pr['pr_url']}#pullrequestreview-7",
        )

    def test_check_ready_emits_captured_head_sha_and_pr_identity(self):
        pr = {
            "repo_name": "owner/repo",
            "number": 42,
            "title": "Fix the reviewer",
            "pr_url": "https://github.com/owner/repo/pull/42",
            "head_sha": "abc123",
        }
        anchors = MODULE.parse_unified_diff(DIFF)

        with (
            mock.patch.object(
                MODULE,
                "preflight",
                return_value=(
                    pr,
                    "viewer",
                    anchors,
                    None,
                    {"id": 10, "url": "https://example.test/review/10"},
                    [
                        {
                            "path": "src/one.py",
                            "line": 2,
                            "body": "Preserve the old behavior.",
                        }
                    ],
                ),
            ),
            mock.patch.object(MODULE, "emit") as emit,
        ):
            MODULE.command_check(SimpleNamespace(target=pr["pr_url"]))

        payload = emit.call_args.args[0]
        self.assertEqual(payload["result"], "ready")
        self.assertEqual(payload["head_sha"], "abc123")
        self.assertEqual(payload["pr_number"], 42)
        self.assertEqual(payload["pr_title"], "Fix the reviewer")
        self.assertEqual(payload["copilot_review"]["id"], 10)
        self.assertEqual(payload["suppressed_comments"][0]["path"], "src/one.py")

    def test_check_ready_emits_empty_suppressed_fields_without_copilot_review(self):
        pr = {
            "repo_name": "owner/repo",
            "number": 42,
            "title": "Fix the reviewer",
            "pr_url": "https://github.com/owner/repo/pull/42",
            "head_sha": "abc123",
        }
        anchors = MODULE.parse_unified_diff(DIFF)

        with (
            mock.patch.object(
                MODULE,
                "preflight",
                return_value=(pr, "viewer", anchors, None, None, []),
            ),
            mock.patch.object(MODULE, "emit") as emit,
        ):
            MODULE.command_check(SimpleNamespace(target=pr["pr_url"]))

        payload = emit.call_args.args[0]
        self.assertIsNone(payload["copilot_review"])
        self.assertEqual(payload["suppressed_comments"], [])


class ResolvePrTest(unittest.TestCase):
    def metadata(self, **overrides):
        base = {
            "number": 42,
            "title": "Fix the reviewer",
            "url": "https://github.com/owner/repo/pull/42",
            "headRefOid": "abc123",
        }
        base.update(overrides)
        return base

    def test_returns_the_pr_title(self):
        target = MODULE.parse_target("owner/repo#42")

        with mock.patch.object(
            MODULE, "gh_json", return_value=self.metadata()
        ) as gh_json:
            result = MODULE.resolve_pr(target)

        self.assertEqual(result["title"], "Fix the reviewer")
        self.assertEqual(result["head_sha"], "abc123")
        self.assertIn("title", gh_json.call_args.args[0][-1].split(","))

    def test_rejects_metadata_without_a_title(self):
        target = MODULE.parse_target("owner/repo#42")

        with mock.patch.object(MODULE, "gh_json", return_value=self.metadata(title="  ")):
            with self.assertRaisesRegex(MODULE.WorkflowError, "no title"):
                MODULE.resolve_pr(target)


class HeadStabilityTest(unittest.TestCase):
    def setUp(self):
        self.pr = {
            "repo_name": "owner/repo",
            "number": 42,
            "pr_url": "https://github.com/owner/repo/pull/42",
            "head_sha": "abc123",
        }

    def test_accepts_unchanged_head(self):
        with mock.patch.object(MODULE, "resolve_pr", return_value=self.pr) as resolve:
            MODULE.ensure_head_unchanged(self.pr, "while testing")

        resolve.assert_called_once_with(self.pr)

    def test_preflight_rejects_head_change_after_diff_is_parsed(self):
        changed = {**self.pr, "head_sha": "def456"}
        with (
            mock.patch.object(MODULE, "resolve_pr", side_effect=[self.pr, changed]),
            mock.patch.object(MODULE, "resolve_viewer", return_value="viewer"),
            mock.patch.object(MODULE, "fetch_reviews", return_value=[]),
            mock.patch.object(MODULE, "find_pending_review", return_value=None),
            mock.patch.object(MODULE, "fetch_authoritative_diff", return_value=DIFF),
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError,
                "PR head changed after fetching and parsing the authoritative diff "
                "and Copilot review",
            ):
                MODULE.preflight(self.pr["pr_url"])

    def test_post_preflight_rejects_head_changed_since_check_before_other_calls(self):
        changed = {**self.pr, "head_sha": "def456"}
        with (
            mock.patch.object(MODULE, "resolve_pr", return_value=changed),
            mock.patch.object(MODULE, "resolve_viewer") as resolve_viewer,
            mock.patch.object(MODULE, "fetch_reviews") as fetch_reviews,
            mock.patch.object(MODULE, "find_pending_review") as find_pending,
            mock.patch.object(MODULE, "fetch_authoritative_diff") as fetch_diff,
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError,
                "does not match the snapshot analyzed by check",
            ):
                MODULE.preflight(self.pr["pr_url"], self.pr["head_sha"])

        resolve_viewer.assert_not_called()
        fetch_reviews.assert_not_called()
        find_pending.assert_not_called()
        fetch_diff.assert_not_called()


class PostingTest(unittest.TestCase):
    def setUp(self):
        self.pr = {
            "repo_name": "owner/repo",
            "number": 42,
            "pr_url": "https://github.com/owner/repo/pull/42",
            "head_sha": "abc123",
        }
        self.anchors = MODULE.parse_unified_diff(DIFF)
        self.comments = [
            {
                "path": "src/one.py",
                "line": 2,
                "side": "RIGHT",
                "body": "This breaks callers. Preserve the old behavior.",
            }
        ]

    def write_comments(self, directory):
        path = Path(directory) / "comments.json"
        path.write_text(json.dumps(self.comments), encoding="utf-8")
        return path

    def test_payload_omits_body_and_event_and_success_is_verified(self):
        created = {"id": 9, "html_url": f"{self.pr['pr_url']}#pullrequestreview-9"}
        verified = {
            **created,
            "commit_id": self.pr["head_sha"],
            "state": "PENDING",
            "user": {"login": "viewer"},
        }
        with tempfile.TemporaryDirectory() as directory:
            comments_path = self.write_comments(directory)
            with (
                mock.patch.object(
                    MODULE,
                    "preflight",
                    return_value=(self.pr, "viewer", self.anchors, None, None, []),
                ) as preflight,
                mock.patch.object(MODULE, "gh_json", return_value=created) as gh_json,
                mock.patch.object(
                    MODULE, "verify_created_review", return_value=verified
                ) as verify,
                mock.patch.object(MODULE, "ensure_head_unchanged") as ensure_head,
                mock.patch.object(MODULE, "emit") as emit,
            ):
                MODULE.command_post(
                    SimpleNamespace(
                        target=self.pr["pr_url"],
                        expected_head=self.pr["head_sha"],
                        comments=str(comments_path),
                    )
                )

        payload = gh_json.call_args.kwargs["input_payload"]
        preflight.assert_called_once_with(self.pr["pr_url"], self.pr["head_sha"])
        self.assertNotIn("body", payload)
        self.assertNotIn("event", payload)
        self.assertEqual(payload["commit_id"], "abc123")
        self.assertEqual(payload["comments"], self.comments)
        verify.assert_called_once_with(self.pr, "viewer", 9, self.comments)
        ensure_head.assert_called_once_with(
            self.pr, "immediately before creating the review"
        )
        self.assertEqual(emit.call_args.args[0]["result"], "created_pending_review")
        self.assertEqual(
            emit.call_args.args[0]["review_url"],
            f"{self.pr['pr_url']}#pullrequestreview-9",
        )

    def test_post_verification_failure_is_exposed(self):
        created = {"id": 9, "html_url": f"{self.pr['pr_url']}#pullrequestreview-9"}
        with tempfile.TemporaryDirectory() as directory:
            comments_path = self.write_comments(directory)
            with (
                mock.patch.object(
                    MODULE,
                    "preflight",
                    return_value=(self.pr, "viewer", self.anchors, None, None, []),
                ),
                mock.patch.object(MODULE, "gh_json", return_value=created),
                mock.patch.object(
                    MODULE,
                    "verify_created_review",
                    side_effect=MODULE.WorkflowError("state is SUBMITTED"),
                ),
                mock.patch.object(MODULE, "ensure_head_unchanged"),
            ):
                with self.assertRaisesRegex(
                    MODULE.WorkflowError,
                    "was created but verification failed: state is SUBMITTED",
                ) as caught:
                    MODULE.command_post(
                        SimpleNamespace(
                            target=self.pr["pr_url"],
                            expected_head=self.pr["head_sha"],
                            comments=str(comments_path),
                        )
                    )

        self.assertIn(created["html_url"], str(caught.exception))

    def test_head_change_immediately_before_post_prevents_creation(self):
        with (
            mock.patch.object(
                MODULE,
                "preflight",
                return_value=(self.pr, "viewer", self.anchors, None, None, []),
            ),
            mock.patch.object(MODULE, "load_comments", return_value=self.comments),
            mock.patch.object(
                MODULE,
                "ensure_head_unchanged",
                side_effect=MODULE.WorkflowError(
                    "PR head changed immediately before creating the review"
                ),
            ),
            mock.patch.object(MODULE, "gh_json") as gh_json,
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError,
                "PR head changed immediately before creating the review",
            ):
                MODULE.command_post(
                    SimpleNamespace(
                        target=self.pr["pr_url"],
                        expected_head=self.pr["head_sha"],
                        comments="comments.json",
                    )
                )

        gh_json.assert_not_called()

    def test_post_rejects_preflight_head_mismatch_before_mutation(self):
        changed = {**self.pr, "head_sha": "def456"}
        with (
            mock.patch.object(
                MODULE,
                "preflight",
                return_value=(changed, "viewer", self.anchors, None, None, []),
            ),
            mock.patch.object(MODULE, "load_comments") as load_comments,
            mock.patch.object(MODULE, "gh_json") as gh_json,
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError,
                "does not match the snapshot analyzed by check",
            ):
                MODULE.command_post(
                    SimpleNamespace(
                        target=self.pr["pr_url"],
                        expected_head=self.pr["head_sha"],
                        comments="comments.json",
                    )
                )

        load_comments.assert_not_called()
        gh_json.assert_not_called()

    def test_post_parser_requires_expected_head(self):
        parser = MODULE.build_parser()

        with (
            mock.patch.object(MODULE.sys, "stderr", new=io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            parser.parse_args(["post", self.pr["pr_url"], "--comments", "comments.json"])
        args = parser.parse_args(
            [
                "post",
                self.pr["pr_url"],
                "--expected-head",
                self.pr["head_sha"],
                "--comments",
                "comments.json",
            ]
        )
        self.assertEqual(args.expected_head, self.pr["head_sha"])

    def test_review_verification_rejects_commit_mismatch(self):
        review = {
            "id": 9,
            "commit_id": "def456",
            "state": "PENDING",
            "html_url": f"{self.pr['pr_url']}#pullrequestreview-9",
            "user": {"login": "viewer"},
        }

        with (
            mock.patch.object(MODULE, "gh_json", return_value=review),
            mock.patch.object(MODULE, "gh_paginated") as gh_paginated,
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "commit does not match expected PR head"
            ):
                MODULE.verify_created_review(
                    self.pr, "viewer", review["id"], self.comments
                )

        gh_paginated.assert_not_called()

    def test_review_verification_rejects_current_head_change(self):
        review = {
            "id": 9,
            "commit_id": self.pr["head_sha"],
            "state": "PENDING",
            "html_url": f"{self.pr['pr_url']}#pullrequestreview-9",
            "user": {"login": "viewer"},
        }

        with (
            mock.patch.object(MODULE, "gh_json", return_value=review),
            mock.patch.object(MODULE, "gh_paginated", return_value=self.comments),
            mock.patch.object(
                MODULE,
                "ensure_head_unchanged",
                side_effect=MODULE.WorkflowError(
                    "PR head changed during final verification"
                ),
            ),
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "PR head changed during final verification"
            ):
                MODULE.verify_created_review(
                    self.pr, "viewer", review["id"], self.comments
                )

    def test_review_verification_rejects_comment_mismatch(self):
        review = {
            "id": 9,
            "commit_id": self.pr["head_sha"],
            "state": "PENDING",
            "html_url": f"{self.pr['pr_url']}#pullrequestreview-9",
            "user": {"login": "viewer"},
        }
        wrong = [{**self.comments[0], "body": "Different."}]

        with (
            mock.patch.object(MODULE, "gh_json", return_value=review),
            mock.patch.object(MODULE, "gh_paginated", return_value=wrong),
            mock.patch.object(MODULE, "ensure_head_unchanged"),
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "inline comments failed verification"
            ):
                MODULE.verify_created_review(
                    self.pr, "viewer", review["id"], self.comments
                )

    def test_review_verification_accepts_exact_pending_review(self):
        review = {
            "id": 9,
            "commit_id": self.pr["head_sha"],
            "state": "PENDING",
            "html_url": f"{self.pr['pr_url']}#pullrequestreview-9",
            "user": {"login": "viewer"},
        }

        with (
            mock.patch.object(MODULE, "gh_json", return_value=review),
            mock.patch.object(MODULE, "gh_paginated", return_value=self.comments),
            mock.patch.object(MODULE, "resolve_pr", return_value=self.pr) as resolve_pr,
        ):
            result = MODULE.verify_created_review(
                self.pr, "viewer", review["id"], self.comments
            )

        self.assertEqual(result, review)
        resolve_pr.assert_called_once_with(self.pr)


if __name__ == "__main__":
    unittest.main()
