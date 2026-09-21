import importlib.util
import json
from pathlib import Path
import re
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


class WindowsSubprocessTest(unittest.TestCase):
    def test_run_hides_windows_console_processes(self):
        completed = MODULE.subprocess.CompletedProcess(["gh"], 0, "", "")
        with (
            mock.patch.object(MODULE, "IS_WINDOWS", True),
            mock.patch.object(
                MODULE.subprocess,
                "CREATE_NO_WINDOW",
                0x08000000,
                create=True,
            ),
            mock.patch.object(
                MODULE.subprocess, "run", return_value=completed
            ) as subprocess_run,
        ):
            MODULE.run(["gh"])

        self.assertEqual(
            subprocess_run.call_args.kwargs["creationflags"], 0x08000000
        )
        self.assertEqual(
            subprocess_run.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8"
        )

    def test_run_leaves_non_windows_process_options_unchanged(self):
        completed = MODULE.subprocess.CompletedProcess(["gh"], 0, "", "")
        with (
            mock.patch.object(MODULE, "IS_WINDOWS", False),
            mock.patch.object(
                MODULE.subprocess, "run", return_value=completed
            ) as subprocess_run,
        ):
            MODULE.run(["gh"])

        self.assertNotIn("creationflags", subprocess_run.call_args.kwargs)


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


class ThinCoordinatorInstructionsTest(unittest.TestCase):
    def test_uses_only_the_coordinator_with_independent_hosted_critique(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("tools: [execute, rename_session]", instructions)
        self.assertIn("disable-model-invocation: true", instructions)
        self.assertIn("`PR Review: <PR number> - <PR title>`", instructions)
        self.assertIn('run <target> --model sol --post-pending-review --execution-handle', instructions)
        self.assertIn("separate fresh Astra task", instructions)
        self.assertIn("verifies each task's actual model", instructions)
        self.assertIn("no hosted max-effort attestation", instructions)
        self.assertIn("Sol fallback for Astra", instructions)
        self.assertIn("Empty discovery uses one task; nonempty discovery uses two", instructions)

    def test_forbids_local_analysis_and_fallbacks(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("Never run another local repository command", instructions)
        self.assertIn("Never invoke `gh pr diff`", instructions)
        self.assertIn("install or execute PR code locally", instructions)
        self.assertIn("Cloud Sandboxes", instructions)
        self.assertIn("local critique, replacement task", instructions)
        self.assertIn("All semantic work stays hosted", instructions)

    def test_preserves_pending_review_and_recovery_contract(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn("only its verified check result", instructions)
        self.assertIn("creates and verifies one viewer-owned pending review", instructions)
        self.assertIn("never submits it", instructions)
        self.assertIn("A `ready` result alone grants no posting permission", instructions)
        self.assertIn("Never retry or use direct `gh api` as a fallback", instructions)
        self.assertIn("no findings with no mutation", instructions)

    def test_all_evaluator_rejections_end_without_posting(self):
        instructions = AGENT.read_text(encoding="utf-8")

        self.assertIn(
            "Astra rejecting every candidate",
            instructions,
        )
        self.assertIn("no review mutation is needed", instructions)
        self.assertIn("comments_file` unchanged", instructions)


class ForegroundDriverTest(unittest.TestCase):
    def run_driver(self, result, *, authorize=False, failure=None):
        arguments = SimpleNamespace(target="owner/repo#42", model="sol",
                                    repo_root=None, post_pending_review=authorize)
        calls = []

        def checked(args, *, result_sink):
            result_sink(result)

        def posted(args, *, result_sink):
            calls.append(args)
            if failure:
                raise failure
            result_sink({"result": "created_pending_review", "review_url": "https://example/review"})

        with (
            mock.patch.object(MODULE, "command_check", side_effect=checked),
            mock.patch.object(MODULE, "command_post", side_effect=posted),
            mock.patch.object(MODULE, "emit") as emit,
        ):
            MODULE.command_run(arguments)
        return calls, emit.call_args.args[0]

    def test_ready_does_not_grant_posting_permission(self):
        calls, output = self.run_driver({"result": "ready"})
        self.assertEqual([], calls)
        self.assertEqual("ready", output["result"])

    def test_empty_or_existing_pending_never_posts_even_with_permission(self):
        for result in ("no_findings", "existing_pending_review"):
            with self.subTest(result=result):
                calls, output = self.run_driver({"result": result}, authorize=True)
                self.assertEqual([], calls)
                self.assertEqual(result, output["result"])

    def test_authorized_post_uses_only_exact_check_identity(self):
        checked = {"result": "ready", "head_sha": "abc", "state": "state.json",
                   "run_id": "run", "comments_file": "comments.json", "pr_number": 42}
        calls, output = self.run_driver(checked, authorize=True)
        self.assertEqual(1, len(calls))
        self.assertEqual({"target": "owner/repo#42", "expected_head": "abc",
                          "state": "state.json", "run_id": "run",
                          "comments": "comments.json"}, vars(calls[0]))
        self.assertEqual("created_pending_review", output["result"])
        self.assertEqual("state.json", output["state"])
        self.assertEqual(42, output["pr_number"])

    def test_post_verification_failure_has_no_retry(self):
        checked = {"result": "ready", "head_sha": "abc", "state": "state.json",
                   "run_id": "run", "comments_file": "comments.json"}
        with self.assertRaisesRegex(MODULE.WorkflowError, "created but unverified"):
            self.run_driver(checked, authorize=True,
                            failure=MODULE.WorkflowError("created but unverified"))

    def test_cancel_fences_pending_review_creation(self):
        checked = {"result": "ready"}
        context = mock.Mock()
        context.check_cancel.side_effect = MODULE.WorkflowError("local stop")
        with (
            mock.patch.object(MODULE, "_EXECUTION", context),
            mock.patch.object(MODULE, "command_check",
                              side_effect=lambda args, result_sink: result_sink(checked)),
            mock.patch.object(MODULE, "command_post") as post,
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "local stop"):
                MODULE.command_run(SimpleNamespace(post_pending_review=True))
            post.assert_not_called()


class ParseTargetTest(unittest.TestCase):
    def test_parses_url_and_short_target(self):
        url = MODULE.parse_target("https://github.com/owner/repo/pull/42/")
        short = MODULE.parse_target("owner/repo#42")
        bare = MODULE.parse_target("42", repo_name="owner/repo")

        self.assertEqual(url, short)
        self.assertEqual(url, bare)
        self.assertEqual(url["repo_name"], "owner/repo")
        self.assertEqual(url["number"], 42)

    def test_parses_a_raw_url_with_a_fragment(self):
        target = MODULE.parse_target(
            "https://github.com/owner/repo/pull/42#pullrequestreview-7"
        )

        self.assertEqual(target, MODULE.parse_target("owner/repo#42"))

    def test_rejects_context_free_bare_number(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "repository workspace"):
            MODULE.parse_target("42")

    def test_resolves_workspace_repository_context(self):
        with mock.patch.object(
            MODULE,
            "gh_json",
            return_value={"nameWithOwner": "owner/repo"},
        ) as gh_json:
            self.assertEqual(MODULE.repository_context(), "owner/repo")

        gh_json.assert_called_once_with(["repo", "view", "--json", "nameWithOwner"])


class UnifiedDiffTest(unittest.TestCase):
    def test_parses_multiple_files_and_hunks(self):
        anchors = MODULE.parse_unified_diff(DIFF)

        self.assertEqual(set(anchors), {"src/one.py", "docs/two.md"})
        self.assertEqual(anchors["src/one.py"]["RIGHT"], {2: 3, 4: 5, 21: 9})
        self.assertEqual(anchors["src/one.py"]["LEFT"], {2: 2, 20: 8})
        self.assertEqual(anchors["docs/two.md"]["LEFT"], {11: 2})
        self.assertEqual(anchors["docs/two.md"]["RIGHT"], {})

    def test_positions_skip_the_first_hunk_header_and_count_later_ones(self):
        anchors = MODULE.parse_unified_diff(DIFF)

        # The line right below the first "@@" is position 1, and each later
        # "@@" header consumes a position of its own.
        self.assertEqual(anchors["src/one.py"]["LEFT"][2], 2)
        self.assertEqual(anchors["src/one.py"]["LEFT"][20], 8)

    def test_positions_count_a_no_newline_marker_inside_a_hunk(self):
        # A trailing marker after a hunk's counts are exhausted marks end of
        # file, so no later position in that file can depend on it.
        diff = """\
diff --git a/data.txt b/data.txt
--- a/data.txt
+++ b/data.txt
@@ -1,2 +1,2 @@
 context
-old
+new
@@ -10,2 +10,2 @@
 tail
-later old
\\ No newline at end of file
+later new
\\ No newline at end of file
"""

        anchors = MODULE.parse_unified_diff(diff)

        self.assertEqual(anchors["data.txt"]["LEFT"], {2: 2, 11: 6})
        self.assertEqual(anchors["data.txt"]["RIGHT"], {2: 3, 11: 8})

    def test_positions_reverse_map_to_a_single_changed_line(self):
        anchors = MODULE.parse_unified_diff(DIFF)

        positions = MODULE.positions_by_path(anchors)

        self.assertEqual(positions["src/one.py"][3], ("RIGHT", 2))
        self.assertEqual(positions["src/one.py"][8], ("LEFT", 20))
        self.assertEqual(positions["docs/two.md"][2], ("LEFT", 11))
        self.assertNotIn(1, positions["src/one.py"])

    def test_captures_line_text_for_changed_and_context_lines(self):
        anchors = MODULE.parse_unified_diff(DIFF)

        self.assertEqual(anchors["src/one.py"]["LEFT_TEXT"][2], "old two")
        self.assertEqual(anchors["src/one.py"]["RIGHT_TEXT"][2], "new two")
        self.assertEqual(anchors["src/one.py"]["LEFT_TEXT"][3], "context three")
        self.assertEqual(anchors["src/one.py"]["RIGHT_TEXT"][3], "context three")

    def test_extracts_original_line_text_from_a_comment_diff_hunk(self):
        hunk = """\
@@ -7,3 +7,3 @@
-old seven
+new seven
 context eight
 context nine"""

        self.assertEqual(
            MODULE.line_text_from_diff_hunk(hunk, 7, "LEFT"), "old seven"
        )
        self.assertEqual(
            MODULE.line_text_from_diff_hunk(hunk, 7, "RIGHT"), "new seven"
        )
        self.assertEqual(
            MODULE.line_text_from_diff_hunk(hunk, 8, "RIGHT"), "context eight"
        )
        self.assertIsNone(MODULE.line_text_from_diff_hunk(hunk, 10, "RIGHT"))

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

        self.assertEqual(anchors["new.txt"]["RIGHT"], {1: 1, 2: 2})
        self.assertEqual(anchors["old.txt"]["LEFT"], {1: 1, 2: 2})

    def test_extracts_a_left_anchored_excerpt_from_a_deleted_file(self):
        diff = """\
diff --git a/old.txt b/old.txt
deleted file mode 100644
index 1111111..0000000
--- a/old.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-one
-two
"""

        excerpt = MODULE.extract_diff_excerpt(diff, "old.txt", "LEFT", 2)

        self.assertIn("--- a/old.txt", excerpt)
        self.assertIn("+++ /dev/null", excerpt)
        self.assertIn("-two", excerpt)

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

        self.assertEqual(anchors["docs/é.md"]["RIGHT"], {1: 1})

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

        self.assertEqual(anchors["data.txt"]["LEFT"], {1: 1})
        self.assertEqual(anchors["data.txt"]["RIGHT"], {1: 2, 2: 3})


class CommentValidationTest(unittest.TestCase):
    def setUp(self):
        self.anchors = MODULE.parse_unified_diff(DIFF)

    def test_accepts_valid_right_and_left_anchors(self):
        comments = [
            {"path": "src/one.py", "line": 4, "side": "RIGHT", "body": "Fix this."},
            {"path": "docs/two.md", "line": 11, "side": "LEFT", "body": "Keep this."},
        ]

        self.assertEqual(MODULE.validate_comments(comments, self.anchors), comments)

    def test_accepts_a_multi_line_range_with_context_in_one_hunk(self):
        comment = {
            "path": "src/one.py",
            "start_line": 2,
            "start_side": "RIGHT",
            "line": 4,
            "side": "RIGHT",
            "body": "Use this instead.\n```suggestion\nreplacement\n```",
        }

        self.assertEqual(
            MODULE.validate_comments([comment], self.anchors),
            [comment],
        )

    def test_rejects_an_incomplete_or_cross_side_range(self):
        incomplete = {
            "path": "src/one.py",
            "start_line": 2,
            "line": 4,
            "side": "RIGHT",
            "body": "Missing start side.",
        }
        cross_side = {
            **incomplete,
            "start_side": "LEFT",
        }

        with self.assertRaisesRegex(
            MODULE.WorkflowError, "provide start_line and start_side"
        ):
            MODULE.validate_comments([incomplete], self.anchors)
        with self.assertRaisesRegex(MODULE.WorkflowError, "same diff side"):
            MODULE.validate_comments([cross_side], self.anchors)

    def test_rejects_a_reversed_or_cross_hunk_range(self):
        reversed_range = {
            "path": "src/one.py",
            "start_line": 4,
            "start_side": "RIGHT",
            "line": 2,
            "side": "RIGHT",
            "body": "Backwards.",
        }
        cross_hunk = {
            **reversed_range,
            "start_line": 4,
            "line": 21,
        }

        with self.assertRaisesRegex(MODULE.WorkflowError, "less than line"):
            MODULE.validate_comments([reversed_range], self.anchors)
        with self.assertRaisesRegex(MODULE.WorkflowError, "within one RIGHT diff hunk"):
            MODULE.validate_comments([cross_hunk], self.anchors)

    def test_rejects_a_range_without_a_changed_line(self):
        anchors = MODULE.parse_unified_diff(
            """\
diff --git a/file.py b/file.py
--- a/file.py
+++ b/file.py
@@ -1,4 +1,4 @@
 context one
 context two
-old
+new
 context four
"""
        )
        comment = {
            "path": "file.py",
            "start_line": 1,
            "start_side": "RIGHT",
            "line": 2,
            "side": "RIGHT",
            "body": "Only context.",
        }

        with self.assertRaisesRegex(
            MODULE.WorkflowError, "contains no changed RIGHT line"
        ):
            MODULE.validate_comments([comment], anchors)

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

    def test_parses_suppressed_heading_inside_review_details(self):
        body = """
<details>
<summary>Review details</summary>

### Suppressed comments (1)

**model/network/common.yaml:27**
* `network.local` groups stable attributes but is marked as development.
```
stability: development
```

- **Files reviewed:** 3/3 changed files
- **Comments generated:** 2
- **Review effort level:** Lite
</details>
"""

        self.assertEqual(
            MODULE.parse_suppressed_comments(body),
            [
                {
                    "path": "model/network/common.yaml",
                    "line": 27,
                    "body": "`network.local` groups stable attributes but is marked "
                    "as development.\n"
                    "```\n"
                    "stability: development\n"
                    "```",
                }
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

        with self.assertRaisesRegex(
            MODULE.WorkflowError, "unrecognized layout near:.*Suppressed comments"
        ):
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

    def test_fetches_and_normalizes_paginated_issue_comments(self):
        pr = {"repo_name": "owner/repo", "number": 42}
        comment = {
            "id": 17,
            "html_url": "https://github.com/owner/repo/pull/42#issuecomment-17",
            "body": "Please split this into a follow-up.",
            "user": {"login": "maintainer"},
            "author_association": "MEMBER",
            "created_at": "2026-08-11T12:00:00Z",
            "updated_at": "2026-08-11T12:01:00Z",
            "ignored": "value",
        }

        with mock.patch.object(
            MODULE, "gh_paginated", return_value=[comment]
        ) as paginated:
            result = MODULE.fetch_issue_comments(pr)

        paginated.assert_called_once_with(
            "repos/owner/repo/issues/42/comments?per_page=100"
        )
        self.assertEqual(
            result,
            [
                {
                    "id": 17,
                    "url": comment["html_url"],
                    "author": "maintainer",
                    "author_association": "MEMBER",
                    "created_at": "2026-08-11T12:00:00Z",
                    "updated_at": "2026-08-11T12:01:00Z",
                    "body": "Please split this into a follow-up.",
                }
            ],
        )

    def test_fetches_and_normalizes_paginated_review_threads(self):
        pr = {"owner": "owner", "repo": "repo", "number": 42}

        def comment(comment_id, *, line=None, original_line=None):
            return {
                "databaseId": comment_id,
                "url": f"https://example.test/comment/{comment_id}",
                "author": {"login": "maintainer"},
                "authorAssociation": "MEMBER",
                "createdAt": "2026-08-11T12:00:00Z",
                "updatedAt": "2026-08-11T12:01:00Z",
                "path": "src/app.py",
                "line": line,
                "originalLine": original_line,
                "startLine": None,
                "originalStartLine": None,
                "diffHunk": (
                    "@@ -7,3 +7,3 @@\n"
                    "-old seven\n"
                    "+new seven\n"
                    " context eight\n"
                    " context nine"
                ),
                "body": f"Comment {comment_id}",
            }

        first_page = {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "THREAD_1",
                                "isResolved": True,
                                "isOutdated": True,
                                "path": "src/app.py",
                                "line": None,
                                "diffSide": "RIGHT",
                                "startLine": None,
                                "startDiffSide": None,
                                "comments": {
                                    "nodes": [comment(1, original_line=7)],
                                    "pageInfo": {
                                        "hasNextPage": True,
                                        "endCursor": "COMMENT_CURSOR",
                                    },
                                },
                            }
                        ],
                        "pageInfo": {
                            "hasNextPage": True,
                            "endCursor": "THREAD_CURSOR",
                        },
                    }
                }
            }
        }
        more_comments = {
            "node": {
                "comments": {
                    "nodes": [comment(2, line=8)],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        more_threads = {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "THREAD_2",
                                "isResolved": False,
                                "isOutdated": False,
                                "path": "src/app.py",
                                "line": 9,
                                "diffSide": "RIGHT",
                                "startLine": 8,
                                "startDiffSide": "RIGHT",
                                "comments": {
                                    "nodes": [comment(3, line=9)],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                },
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }

        with mock.patch.object(
            MODULE,
            "graphql_data",
            side_effect=[first_page, more_comments, more_threads],
        ) as graphql:
            result = MODULE.fetch_review_threads(pr)

        self.assertEqual([thread["id"] for thread in result], ["THREAD_1", "THREAD_2"])
        self.assertTrue(result[0]["resolved"])
        self.assertFalse(result[1]["resolved"])
        self.assertTrue(result[0]["is_resolved"])
        self.assertTrue(result[0]["is_outdated"])
        self.assertEqual(result[0]["path"], "src/app.py")
        self.assertIsNone(result[0]["line"])
        self.assertEqual(result[0]["side"], "RIGHT")
        self.assertFalse(result[1]["is_resolved"])
        self.assertFalse(result[1]["is_outdated"])
        self.assertEqual(result[1]["line"], 9)
        self.assertEqual(result[1]["side"], "RIGHT")
        self.assertEqual(result[1]["start_line"], 8)
        self.assertEqual(result[1]["start_side"], "RIGHT")
        self.assertEqual(
            [item["line"] for item in result[0]["comments"]],
            [7, 8],
        )
        self.assertEqual(
            [item["line_text"] for item in result[0]["comments"]],
            ["new seven", "context eight"],
        )
        self.assertEqual(result[0]["comments"][0]["author"], "maintainer")
        self.assertEqual(
            graphql.call_args_list[1].args[1],
            {"id": "THREAD_1", "cursor": "COMMENT_CURSOR"},
        )
        self.assertEqual(
            graphql.call_args_list[2].args[1]["cursor"], "THREAD_CURSOR"
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
                return_value=(pr, "viewer", {}, pending_url, None, [], [], None),
            ),
            mock.patch.object(MODULE, "emit") as emit,
        ):
            MODULE.command_check(SimpleNamespace(
                target=pr["pr_url"], diff_file=None, context_file=None
            ))

        self.assertEqual(emit.call_args.args[0]["result"], "existing_pending_review")
        self.assertEqual(
            emit.call_args.args[0]["review_url"],
            f"{pr['pr_url']}#pullrequestreview-7",
        )


class ResolvePrTest(unittest.TestCase):
    def metadata(self, **overrides):
        base = {
            "number": 42,
            "title": "Fix the reviewer",
            "body": "Body",
            "html_url": "https://github.com/owner/repo/pull/42",
            "state": "open",
            "draft": False,
            "base": {
                "repo": {"full_name": "owner/repo"},
                "ref": "main",
                "sha": "1" * 40,
            },
            "head": {
                "repo": {"full_name": "owner/repo"},
                "ref": "feature",
                "sha": "2" * 40,
            },
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
        self.assertEqual(result["head_sha"], "2" * 40)
        self.assertEqual(
            gh_json.call_args.args[0],
            ["api", "repos/owner/repo/pulls/42"],
        )

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
                "PR head changed after fetching the authoritative diff",
            ):
                MODULE.preflight(self.pr["pr_url"])

    def test_preflight_returns_the_exact_authoritative_diff(self):
        with (
            mock.patch.object(
                MODULE, "resolve_pr", side_effect=[self.pr, self.pr]
            ),
            mock.patch.object(MODULE, "resolve_viewer", return_value="viewer"),
            mock.patch.object(MODULE, "fetch_reviews", return_value=[]),
            mock.patch.object(MODULE, "find_pending_review", return_value=None),
            mock.patch.object(
                MODULE, "fetch_authoritative_diff", return_value=DIFF
            ) as fetch_diff,
        ):
            result = MODULE.preflight(self.pr["pr_url"])

        self.assertEqual(result[-1], DIFF)
        fetch_diff.assert_called_once_with(self.pr)

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


class ManagedCoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.pr = {
            "owner": "owner",
            "repo": "repo",
            "repo_name": "owner/repo",
            "number": 42,
            "pr_url": "https://github.com/owner/repo/pull/42",
            "url": "https://github.com/owner/repo/pull/42",
            "title": "Fix the reviewer",
            "body": "Body",
            "state": "open",
            "is_draft": False,
            "base": {
                "repository": "owner/repo",
                "ref": "main",
                "sha": "1" * 40,
            },
            "head": {
                "repository": "owner/repo",
                "ref": "feature",
                "sha": "2" * 40,
            },
            "head_sha": "2" * 40,
            "cross_repository": False,
        }
        self.identity = {"head": "4" * 40, "status": ""}

    def result(self, **overrides):
        request_id = "request-1"
        generated_head = "3" * 40
        value = {
            "schema": MODULE.AGENT_TASK_RESULT_SCHEMA,
            "status": "success",
            "mode": "report",
            "repository": {"name_with_owner": self.pr["repo_name"]},
            "pull_request": MODULE.expected_cloud_pull_request(self.pr),
            "requested_model": "gpt-5.6-sol",
            "policy": MODULE.AGENT_TASK_POLICY_IDENTITY,
            "task": {
                "id": "task-1",
                "url": "https://github.com/owner/repo/agent-tasks/1",
                "state": "completed",
                "base_ref": "feature",
                "base_sha": self.pr["head_sha"],
            },
            "generated": {
                "branch": "copilot/task-1",
                "head_sha": generated_head,
                "commits": [],
            },
            "application": {
                "status": "not_applicable",
                "final_local_head": self.identity["head"],
            },
            "report": {
                "path": f".github/agent-task-reports/{request_id}.md",
                "commit": generated_head,
                "sha256": "5" * 64,
            },
            "attestation": {
                "kind": "dispatcher_structural",
                "structural_complete": True,
            },
            "error": None,
        }
        value.update(overrides)
        return value

    def report(self, *, candidate=False, **fields):
        metadata = {
            "Repository": self.pr["repo_name"],
            "Pull request": f"#{self.pr['number']}",
            "Head SHA": self.pr["head_sha"],
            "Base SHA": self.pr["base"]["sha"],
            "Review complete": "yes",
            "Changed files reviewed": "2",
        }
        metadata.update(fields)
        lines = ["# PR review report", ""]
        lines.extend(f"- **{label}:** `{value}`" for label, value in metadata.items())
        if not candidate:
            lines.extend(["", "## No findings", ""])
        else:
            lines.extend(
                [
                    "",
                    "## Candidate findings",
                    "",
                    "### [blocking] Wrong result",
                    "",
                    "- **File:** `src/one.py`",
                    "- **Anchor:** `RIGHT:2`",
                    "- **Confidence:** `0.98`",
                    "",
                    "The changed branch returns the wrong result.",
                    "",
                    "Evidence: the changed line reaches the failing branch.",
                    "",
                ]
            )
        return "\n".join(lines)

    def test_worker_prompt_requires_artifact_commit_for_no_findings(self):
        prompt = MODULE.build_worker_prompt(
            self.pr,
            {"login": "viewer"},
            "gpt-5.6-sol",
            ["src/one.py", "docs/two.md"],
        )

        self.assertEqual(MODULE.WORKER_PROMPT_VERSION, 5)
        self.assertIn(
            "Write exactly one human-readable Markdown report",
            prompt,
        )
        self.assertIn(
            "create the exact final commit required by the marketplace policy footer",
            prompt,
        )
        self.assertIn("even when there are no findings", prompt)
        self.assertIn(
            "A chat response without that committed report is a failed task",
            prompt,
        )
        self.assertIn("`{{MARKETPLACE_REPORT_PATH}}`", prompt)
        self.assertNotIn("{{MARKETPLACE_VALIDATION_PATH}}", prompt)
        self.assertNotIn('"command": "full-diff-reviewed"', prompt)
        self.assertNotIn('"changed_files":', prompt)
        self.assertNotIn('"candidates":', prompt)
        self.assertNotIn("candidate-report.json", prompt)
        self.assertNotIn("worker-validation.json", prompt)
        self.assertIn("- **Review complete:** yes", prompt)
        self.assertIn("## Candidate findings", prompt)

    def test_validates_success_and_no_findings_reports(self):
        result = self.result()
        remote = MODULE.validate_success_result(
            result,
            pr=self.pr,
            requested_model="gpt-5.6-sol",
            identity=self.identity,
        )
        report = MODULE.validate_candidate_report(
            self.report(),
            pr=self.pr,
            anchors=MODULE.parse_unified_diff(DIFF),
            changed_paths=["src/one.py", "docs/two.md"],
        )

        self.assertEqual(report["candidates"], [])
        self.assertEqual(remote["generated_head"], "3" * 40)

    def test_validates_candidate_schema_and_changed_anchor(self):
        report = MODULE.validate_candidate_report(
            self.report(candidate=True),
            pr=self.pr,
            anchors=MODULE.parse_unified_diff(DIFF),
            changed_paths=["src/one.py", "docs/two.md"],
        )

        self.assertEqual(report["candidates"][0]["candidate_id"], "candidate-001")
        excerpt = MODULE.extract_diff_excerpt(DIFF, "src/one.py", "RIGHT", 2)
        self.assertIn("+++ b/src/one.py", excerpt)
        self.assertIn("+new four", excerpt)
        self.assertNotIn("@@ -20,2 +21,2 @@", excerpt)

        stale = self.report(candidate=True).replace("RIGHT:2", "RIGHT:999")
        with self.assertRaisesRegex(MODULE.WorkflowError, "not a changed RIGHT line"):
            MODULE.validate_candidate_report(
                stale,
                pr=self.pr,
                anchors=MODULE.parse_unified_diff(DIFF),
                changed_paths=["src/one.py", "docs/two.md"],
            )

    def test_rejects_malformed_candidates_and_credentials(self):
        malformed = self.report(candidate=True).replace(
            "- **Anchor:** `RIGHT:2`\n", ""
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "no reconstructable"):
            MODULE.validate_candidate_report(
                malformed,
                pr=self.pr,
                anchors=MODULE.parse_unified_diff(DIFF),
                changed_paths=["src/one.py", "docs/two.md"],
            )

        credential = self.report(candidate=True) + (
            "\ntoken=github_pat_" + "a" * 20
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "credentials"):
            MODULE.validate_candidate_report(
                credential,
                pr=self.pr,
                anchors=MODULE.parse_unified_diff(DIFF),
                changed_paths=["src/one.py", "docs/two.md"],
            )

    def test_rejects_wrong_policy_repo_pr_head_and_model(self):
        cases = [
            ("policy", {"policy": {"id": "wrong", "version": 1, "sha256": "x"}}),
            ("repository", {"repository": {"name_with_owner": "other/repo"}}),
            ("pull request", {"pull_request": {}}),
            ("model", {"requested_model": "gpt-5.6-terra"}),
        ]
        for label, change in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(MODULE.WorkflowError, "does not match"):
                    MODULE.validate_result_identity(
                        self.result(**change),
                        pr=self.pr,
                        requested_model="gpt-5.6-sol",
                        identity=self.identity,
                    )

    def test_rejects_malformed_results_tasks_and_incomplete_attestation(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.json"
            result_path.write_text('{"schema":', encoding="utf-8")
            with self.assertRaisesRegex(MODULE.WorkflowError, "invalid JSON"):
                MODULE.load_agent_task_result(result_path)

        malformed_task = self.result(task={})
        with self.assertRaisesRegex(MODULE.WorkflowError, "malformed task"):
            MODULE.validate_success_result(
                malformed_task,
                pr=self.pr,
                requested_model="gpt-5.6-sol",
                identity=self.identity,
            )
        incomplete = self.result(
            attestation={
                "kind": "dispatcher_structural",
                "structural_complete": False,
            }
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "malformed task|identity"):
            MODULE.validate_success_result(
                incomplete,
                pr=self.pr,
                requested_model="gpt-5.6-sol",
                identity=self.identity,
            )

    def test_rejects_unexpected_report_commit_paths(self):
        remote = MODULE.validate_success_result(
            self.result(),
            pr=self.pr,
            requested_model="gpt-5.6-sol",
            identity=self.identity,
        )
        commit = {
            "sha": remote["generated_head"],
            "parents": [{"sha": self.pr["head_sha"]}],
            "files": [
                {"filename": remote["report_path"]},
                {"filename": "unexpected.txt"},
            ],
        }
        with mock.patch.object(MODULE, "gh_json", return_value=commit):
            with self.assertRaisesRegex(MODULE.WorkflowError, "exactly one"):
                MODULE.validate_report_commit(self.pr, remote)

        fixture = json.loads(
            (
                Path(__file__).parent
                / "fixtures"
                / "task-92e6f838-artifact-path-failure.json"
            ).read_text(encoding="utf-8")
        )
        production_pr = {
            **self.pr,
            "head_sha": fixture["source_head"],
        }
        production_remote = {
            **remote,
            "generated_head": fixture["generated_commit"],
            "report_path": fixture["assigned_paths"][0],
        }
        failed_commit = {
            "sha": fixture["generated_commit"],
            "parents": [{"sha": fixture["source_head"]}],
            "files": [
                {"filename": path}
                for path in fixture["produced_paths"]
            ],
        }
        with mock.patch.object(MODULE, "gh_json", return_value=failed_commit):
            with self.assertRaisesRegex(MODULE.WorkflowError, "exactly one"):
                MODULE.validate_report_commit(production_pr, production_remote)

    def test_rejects_wrong_task_and_markdown_identity(self):
        wrong_task = self.result()
        wrong_task["task"]["base_sha"] = "9" * 40
        with self.assertRaisesRegex(MODULE.WorkflowError, "malformed task"):
            MODULE.validate_success_result(
                wrong_task,
                pr=self.pr,
                requested_model="gpt-5.6-sol",
                identity=self.identity,
            )

        wrong_report = self.report().replace("owner/repo", "other/repo")
        with self.assertRaisesRegex(MODULE.WorkflowError, "Repository"):
            MODULE.validate_candidate_report(
                wrong_report,
                pr=self.pr,
                anchors=MODULE.parse_unified_diff(DIFF),
                changed_paths=["src/one.py", "docs/two.md"],
            )

    def test_rejects_ambiguous_markdown_candidate_sections(self):
        report = self.report(candidate=True) + "\n## No findings\n"
        with self.assertRaisesRegex(MODULE.WorkflowError, "ambiguous"):
            MODULE.validate_candidate_report(
                report,
                pr=self.pr,
                anchors=MODULE.parse_unified_diff(DIFF),
                changed_paths=["src/one.py", "docs/two.md"],
            )

    def test_candidate_section_stops_at_the_next_report_section(self):
        report = self.report(candidate=True) + (
            "\n## Discovery notes\n\nThis is not candidate evidence.\n"
        )

        candidates = MODULE.parse_markdown_candidates(
            report,
            anchors=MODULE.parse_unified_diff(DIFF),
            changed_paths=["src/one.py", "docs/two.md"],
        )

        self.assertNotIn("Discovery notes", candidates[0]["report_excerpt"])

    def test_rejects_candidate_anchor_ambiguous_between_diff_sides(self):
        report = self.report(candidate=True).replace("RIGHT:2", "2")

        with self.assertRaisesRegex(MODULE.WorkflowError, "ambiguous"):
            MODULE.parse_markdown_candidates(
                report,
                anchors=MODULE.parse_unified_diff(DIFF),
                changed_paths=["src/one.py", "docs/two.md"],
            )

    def test_rejects_candidate_without_concrete_evidence(self):
        report = self.report(candidate=True).replace(
            "The changed branch returns the wrong result.\n\n"
            "Evidence: the changed line reaches the failing branch.",
            "",
        )

        with self.assertRaisesRegex(MODULE.WorkflowError, "concrete Markdown evidence"):
            MODULE.parse_markdown_candidates(
                report,
                anchors=MODULE.parse_unified_diff(DIFF),
                changed_paths=["src/one.py", "docs/two.md"],
            )

    def test_rejects_duplicate_candidate_fields(self):
        report = self.report(candidate=True).replace(
            "- **Anchor:** `RIGHT:2`",
            "- **Anchor:** `RIGHT:2`\n- **Anchor:** `LEFT:2`",
        )

        with self.assertRaisesRegex(MODULE.WorkflowError, "duplicate Anchor"):
            MODULE.parse_markdown_candidates(
                report,
                anchors=MODULE.parse_unified_diff(DIFF),
                changed_paths=["src/one.py", "docs/two.md"],
            )

    def test_rejects_oversized_report_and_candidate_fanout(self):
        with self.assertRaisesRegex(MODULE.WorkflowError, "1 MiB"):
            MODULE.parse_markdown_candidates(
                "x" * (MODULE.MAX_REPORT_BYTES + 1),
                anchors=MODULE.parse_unified_diff(DIFF),
                changed_paths=["src/one.py", "docs/two.md"],
            )

        candidate_section = self.report(candidate=True).split(
            "## Candidate findings\n\n",
            1,
        )[1]
        report = "## Candidate findings\n\n" + (
            candidate_section * (MODULE.MAX_CANDIDATES + 1)
        )
        with self.assertRaisesRegex(MODULE.WorkflowError, "candidate limit"):
            MODULE.parse_markdown_candidates(
                report,
                anchors=MODULE.parse_unified_diff(DIFF),
                changed_paths=["src/one.py", "docs/two.md"],
            )

    def test_extracts_candidate_from_task_47_markdown_fixture(self):
        report = (
            (
                Path(__file__).parent
                / "fixtures"
                / "task-47d8715a-candidate-report.md"
            ).read_text(encoding="utf-8")
        )
        path = (
            "instrumentation/grpc-1.6/testing/src/main/java/"
            "io/opentelemetry/instrumentation/grpc/v1_6/AbstractGrpcTest.java"
        )
        anchors = {path: {"RIGHT": {1754: 1}, "LEFT": {}}}

        candidates = MODULE.parse_markdown_candidates(
            report,
            anchors=anchors,
            changed_paths=[path],
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["path"], path)
        self.assertEqual(candidates[0]["anchor"]["line"], 1754)
        self.assertEqual(candidates[0]["anchor"]["side"], "RIGHT")
        with self.assertRaisesRegex(MODULE.WorkflowError, "Repository"):
            MODULE.validate_candidate_report(
                report,
                pr={
                    **self.pr,
                    "repo_name": "open-telemetry/opentelemetry-java-instrumentation",
                    "number": 20130,
                    "head_sha": "fc375c2363c743c1f63370b1ac85f55f973e3943",
                },
                anchors=anchors,
                changed_paths=[path],
            )

    def test_task_failure_is_deterministic(self):
        error = MODULE.task_failure_from_result(
            self.result(
                status="failure",
                error={"code": "task_failed", "message": "worker stopped"},
            )
        )
        self.assertEqual(str(error), "Agent Task failed [task_failed]: worker stopped")

    def hosted_check(self, *, nonempty=False, reject_all=False, incomplete=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        repo_root = root / "repo"
        repo_root.mkdir()
        state_path = root / "state" / "run.json"
        helper = root / "cloud_task.py"
        commands = []
        runtime = mock.Mock()
        runtime.CloudError = RuntimeError
        runtime.Options.side_effect = lambda **kwargs: SimpleNamespace(**kwargs)
        runtime.PullRequestSnapshot.side_effect = lambda **kwargs: SimpleNamespace(**kwargs)

        def invoke(command, **kwargs):
            if "--result-file" in command:
                commands.append(command)
                phase = len(commands)
                result = {
                    "status": "success",
                    "task": {"id": f"task-{phase}", "state": "completed"},
                    "generated": {"branch": f"copilot/task-{phase}", "head_sha": str(phase) * 40},
                }
                Path(command[command.index("--result-file") + 1]).write_text(json.dumps(result), encoding="utf-8")
                return MODULE.subprocess.CompletedProcess(command, 0, "", "")
            self.assertEqual(["git", "-C", str(repo_root), "show"], command[:4])
            if command[-1].endswith(MODULE.DISCOVERY_PATH):
                output = {"outcome": "incomplete" if incomplete else "complete", "candidates": [{
                    "path": "src/one.py", "line": 2, "side": "RIGHT",
                    "body": "Wrong result", "evidence": "The changed branch returns the wrong result.",
                }] if nonempty else []}
            else:
                self.assertTrue(command[-1].endswith(MODULE.CRITIQUE_PATH))
                output = {"outcome": "complete", "comments": [] if reject_all else [{
                    "candidate_id": "candidate-001", "body": "This returns the wrong result.",
                }]}
            return MODULE.subprocess.CompletedProcess(command, 0, json.dumps(output), "")

        def verify(result, **kwargs):
            model = kwargs["options"].model
            phase = 1 if model == "gpt-5.6-sol" else 2
            self.assertEqual("gpt-5.6-sol" if len(commands) == 1 else "gpt-6-astra", model)
            self.assertEqual(MODULE.HOSTED_REVIEW_POLICY, kwargs["options"].policy)
            return {
                "task": result["task"], "completion": {"session": {"id": f"session-{phase}"}},
                "candidate": {"phase": phase}, "artifact_commit": {
                    "sha": str(phase) * 40,
                    "changed_paths": [MODULE.DISCOVERY_PATH if phase == 1 else MODULE.CRITIQUE_PATH],
                },
            }
        runtime.verify_candidate_result.side_effect = verify
        with (
            mock.patch.object(MODULE, "preflight", return_value=(
                self.pr, "viewer", MODULE.parse_unified_diff(DIFF), None, None, [], [], DIFF,
            )),
            mock.patch.object(MODULE, "resolve_viewer_permissions", return_value={"login": "viewer"}),
            mock.patch.object(MODULE, "local_identity", return_value=self.identity),
            mock.patch.object(MODULE, "state_path_for", return_value=state_path),
            mock.patch.object(MODULE, "discover_cloud_task", return_value=helper),
            mock.patch.object(MODULE, "load_candidate_runtime", return_value=runtime),
            mock.patch.object(MODULE, "ensure_snapshot_unchanged"),
            mock.patch.object(MODULE, "run", side_effect=invoke),
            mock.patch.object(MODULE, "emit") as emit,
        ):
            args = SimpleNamespace(target=self.pr["pr_url"], model="sol", repo_root=str(repo_root))
            if incomplete:
                with self.assertRaisesRegex(MODULE.WorkflowError, "incomplete") as failure:
                    MODULE.command_check(args)
                self.assertEqual(str(state_path), failure.exception.details["state"])
                payload = None
            else:
                MODULE.command_check(args)
                payload = emit.call_args.args[0]
        return commands, json.loads(state_path.read_text(encoding="utf-8")), payload

    def test_empty_discovery_uses_one_hosted_task(self):
        commands, state, payload = self.hosted_check()
        self.assertEqual(["sol"], [command[command.index("--model") + 1] for command in commands])
        self.assertEqual("no_findings", payload["result"])
        self.assertEqual([], state["hosted_comments"])
        self.assertEqual("not_attempted", state["mutation"]["status"])
        self.assertEqual("completed", state["agent_task"]["status"])
        for phase in state["phases"]:
            self.assertFalse(Path(phase["prompt_file"]).exists())
            self.assertFalse(Path(phase["result_file"]).exists())

    def test_nonempty_discovery_uses_fresh_astra_critique_and_exact_bodies(self):
        commands, state, payload = self.hosted_check(nonempty=True)
        self.assertEqual(["sol", "astra"], [command[command.index("--model") + 1] for command in commands])
        self.assertEqual(2, payload["hosted_task_count"])
        self.assertEqual("ready", payload["result"])
        self.assertNotEqual(state["phases"][0]["task"]["id"], state["phases"][1]["task"]["id"])
        self.assertNotEqual(state["phases"][0]["session_id"], state["phases"][1]["session_id"])
        self.assertEqual("This returns the wrong result.", payload["comments"][0]["body"])
        self.assertEqual(payload["comments"], json.loads(Path(payload["comments_file"]).read_text(encoding="utf-8")))

    def test_astra_rejecting_every_candidate_does_not_create_a_review(self):
        commands, state, payload = self.hosted_check(nonempty=True, reject_all=True)
        self.assertEqual(2, len(commands))
        self.assertEqual("no_findings", payload["result"])
        self.assertEqual([], state["hosted_comments"])
        self.assertEqual("not_attempted", state["mutation"]["status"])

    def test_check_failure_preserves_recovery_state_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo_root = root / "repo"
            repo_root.mkdir()
            state_path = root / "state" / "run.json"
            with (
                mock.patch.object(
                    MODULE,
                    "preflight",
                    return_value=(
                        self.pr,
                        "viewer",
                        MODULE.parse_unified_diff(DIFF),
                        None,
                        None,
                        [],
                        [],
                        DIFF,
                    ),
                ),
                mock.patch.object(MODULE, "fetch_review_threads", return_value=[]),
                mock.patch.object(
                    MODULE,
                    "fetch_changed_paths",
                    return_value=["src/one.py", "docs/two.md"],
                ),
                mock.patch.object(MODULE, "ensure_head_unchanged"),
                mock.patch.object(MODULE, "ensure_snapshot_unchanged"),
                mock.patch.object(
                    MODULE,
                    "resolve_viewer_permissions",
                    return_value={"login": "viewer"},
                ),
                mock.patch.object(MODULE, "local_identity", return_value=self.identity),
                mock.patch.object(MODULE, "state_path_for", return_value=state_path),
                mock.patch.object(
                    MODULE,
                    "discover_cloud_task",
                    side_effect=MODULE.WorkflowError("helper unavailable"),
                ),
                mock.patch.object(MODULE, "run") as run,
            ):
                with self.assertRaisesRegex(MODULE.WorkflowError, "helper unavailable"):
                    MODULE.command_check(
                        SimpleNamespace(
                            target=self.pr["pr_url"],
                            model="sol",
                            repo_root=str(repo_root),
                        )
                    )

            run.assert_not_called()
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["agent_task"]["status"], "failed")
            self.assertEqual(saved["agent_task"]["recovery_files"], [str(state_path)])
            self.assertNotIn("authoritative_diff", saved)
            self.assertNotIn("context", saved)

    def test_incomplete_discovery_stops_without_critique_or_fallback(self):
        commands, state, payload = self.hosted_check(nonempty=True, incomplete=True)
        self.assertEqual(1, len(commands))
        self.assertIsNone(payload)
        self.assertEqual("failed", state["agent_task"]["status"])
        self.assertEqual("not_attempted", state["mutation"]["status"])
        self.assertEqual("task-1", state["agent_task"]["task"]["id"])
        self.assertEqual(3, len(state["agent_task"]["recovery_files"]))
        self.assertTrue(all(Path(path).exists() for path in state["agent_task"]["recovery_files"]))

    def test_main_emits_workflow_error_details(self):
        error = MODULE.WorkflowError(
            "worker failed",
            details={
                "state": "C:/state/run.json",
                "recovery_files": ["C:/state/run.json"],
            },
        )
        args = SimpleNamespace(function=mock.Mock(side_effect=error))
        parser = mock.Mock()
        parser.parse_args.return_value = args

        with (
            mock.patch.object(MODULE.shutil, "which", return_value="gh"),
            mock.patch.object(MODULE, "build_parser", return_value=parser),
            mock.patch.object(MODULE, "emit") as emit,
        ):
            self.assertEqual(MODULE.main(), 1)

        self.assertEqual(
            emit.call_args.args[0],
            {
                "result": "error",
                "error": "worker failed",
                "state": "C:/state/run.json",
                "recovery_files": ["C:/state/run.json"],
            },
        )

    def test_cleanup_failure_names_every_retained_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "prompt.txt"
            second = Path(directory) / "result.json"
            first.write_text("prompt", encoding="utf-8")
            second.write_text("result", encoding="utf-8")

            with mock.patch.object(
                Path,
                "unlink",
                side_effect=OSError("access denied"),
            ):
                with self.assertRaisesRegex(
                    MODULE.WorkflowError,
                    f"{re.escape(str(first))}.*{re.escape(str(second))}",
                ):
                    MODULE.remove_transient_artifacts([first, second])

    def test_independent_hosted_critique_has_no_local_semantic_fallback(self):
        instructions = AGENT.read_text(encoding="utf-8")
        self.assertIn("separate fresh Astra task", instructions)
        self.assertIn("no hosted max-effort attestation", instructions)
        self.assertIn("Do not filter candidates", instructions)
        self.assertIn("comments_file", instructions)
        self.assertNotIn("tools: [execute, agent", instructions)


class GuardedPostingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.pr = {
            "owner": "owner",
            "repo": "repo",
            "repo_name": "owner/repo",
            "number": 42,
            "pr_url": "https://github.com/owner/repo/pull/42",
            "url": "https://github.com/owner/repo/pull/42",
            "title": "Fix the reviewer",
            "body": "Body",
            "state": "open",
            "is_draft": False,
            "base": {
                "repository": "owner/repo",
                "ref": "main",
                "sha": "1" * 40,
            },
            "head": {
                "repository": "owner/repo",
                "ref": "feature",
                "sha": "2" * 40,
            },
            "head_sha": "2" * 40,
            "cross_repository": False,
        }
        self.anchors = MODULE.parse_unified_diff(DIFF)
        self.candidate = {
            "candidate_id": "candidate-1",
            "path": "src/one.py",
            "anchor": {
                "side": "RIGHT",
                "start_line": None,
                "start_side": None,
                "line": 2,
            },
            "severity": "blocking",
            "title": "Wrong result",
            "explanation": "The changed branch returns the wrong result.",
            "evidence": ["evidence"],
            "confidence": 0.99,
            "probes": [
                {"command": "none", "status": "not_run", "outcome": "static proof"}
            ],
        }
        self.state_path = self.directory / "state.json"
        self.comments_path = self.directory / "comments.json"

    def write_state(self, status="not_attempted"):
        self.state_path.write_text(
            json.dumps(
                {
                    "version": MODULE.STATE_VERSION,
                    "run_id": "run-1",
                    "pr": self.pr,
                    "viewer": {"login": "viewer"},
                    "candidates": [self.candidate],
                    "agent_task": {"status": "completed", "model": "gpt-6-astra"},
                    "hosted_comments": [{
                        "candidate_id": "candidate-1", "path": "src/one.py",
                        "line": 2, "side": "RIGHT", "body": "This returns the wrong result.",
                    }],
                    "mutation": {"status": status},
                }
            ),
            encoding="utf-8",
        )

    def args(self):
        return SimpleNamespace(
            target=self.pr["pr_url"],
            expected_head=self.pr["head_sha"],
            state=str(self.state_path),
            run_id="run-1",
            comments=str(self.comments_path),
        )

    def write_comments(self):
        self.comments_path.write_text(
            json.dumps(
                [
                    {
                        "candidate_id": "candidate-1",
                        "path": "src/one.py",
                        "line": 2,
                        "side": "RIGHT",
                        "body": "This returns the wrong result.",
                    }
                ]
            ),
            encoding="utf-8",
        )

    def test_creates_exactly_one_guarded_viewer_owned_pending_review(self):
        self.write_state()
        self.write_comments()
        review = {
            "id": 9,
            "state": "PENDING",
            "user": {"login": "viewer"},
            "commit_id": self.pr["head_sha"],
            "html_url": f"{self.pr['pr_url']}#pullrequestreview-9",
        }
        with (
            mock.patch.object(
                MODULE,
                "preflight",
                return_value=(
                    self.pr,
                    "viewer",
                    self.anchors,
                    None,
                    None,
                    [],
                    [],
                    DIFF,
                ),
            ),
            mock.patch.object(MODULE, "ensure_snapshot_unchanged"),
            mock.patch.object(MODULE, "gh_json", return_value=review) as gh_json,
            mock.patch.object(
                MODULE, "verify_created_review", return_value=review
            ) as verify,
            mock.patch.object(MODULE, "emit") as emit,
        ):
            MODULE.command_post(self.args())

        self.assertEqual(gh_json.call_count, 1)
        payload = gh_json.call_args.kwargs["input_payload"]
        self.assertEqual(set(payload), {"commit_id", "comments"})
        self.assertNotIn("candidate_id", payload["comments"][0])
        verify.assert_called_once()
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["mutation"]["status"], "verified")
        self.assertEqual(emit.call_args.args[0]["result"], "created_pending_review")

    def test_recovers_an_existing_pending_review_without_mutation(self):
        self.write_state(status="attempted")
        pending_url = f"{self.pr['pr_url']}#pullrequestreview-9"
        with (
            mock.patch.object(
                MODULE,
                "preflight",
                return_value=(
                    self.pr,
                    "viewer",
                    {},
                    pending_url,
                    None,
                    [],
                    [],
                    None,
                ),
            ),
            mock.patch.object(MODULE, "gh_json") as gh_json,
            mock.patch.object(MODULE, "emit") as emit,
        ):
            MODULE.command_post(self.args())

        gh_json.assert_not_called()
        self.assertEqual(emit.call_args.args[0]["review_url"], pending_url)

    def test_one_mutation_guard_blocks_a_second_attempt(self):
        self.write_state(status="attempted")
        with mock.patch.object(
            MODULE,
            "preflight",
            return_value=(
                self.pr,
                "viewer",
                self.anchors,
                None,
                None,
                [],
                [],
                DIFF,
            ),
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "one-mutation guard"):
                MODULE.command_post(self.args())

    def test_mutation_claim_is_atomic_and_persistent(self):
        self.write_state()
        _, state = MODULE.load_run_state(str(self.state_path))

        MODULE.claim_mutation(self.state_path, state)

        guard = self.state_path.with_name(f"{self.state_path.name}.mutation-guard")
        self.assertTrue(guard.is_file())
        with self.assertRaisesRegex(MODULE.WorkflowError, "already claimed"):
            MODULE.claim_mutation(self.state_path, state)

    def test_rejects_stale_identity_and_changed_candidate_anchor(self):
        self.write_state()
        self.write_comments()
        changed = {**self.pr, "base": {**self.pr["base"], "sha": "9" * 40}}
        with mock.patch.object(
            MODULE,
            "preflight",
            return_value=(
                changed,
                "viewer",
                self.anchors,
                None,
                None,
                [],
                [],
                DIFF,
            ),
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "live pull request state"):
                MODULE.command_post(self.args())

        self.write_state()
        values = json.loads(self.comments_path.read_text(encoding="utf-8"))
        values[0]["line"] = 4
        self.comments_path.write_text(json.dumps(values), encoding="utf-8")
        with mock.patch.object(
            MODULE,
            "preflight",
            return_value=(
                self.pr,
                "viewer",
                self.anchors,
                None,
                None,
                [],
                [],
                DIFF,
            ),
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "exactly match completed hosted"):
                MODULE.command_post(self.args())

    def test_final_snapshot_recheck_blocks_every_live_identity_change(self):
        changes = {
            "base": {
                **self.pr,
                "base": {**self.pr["base"], "sha": "9" * 40},
            },
            "draft": {**self.pr, "is_draft": True},
            "open": {**self.pr, "state": "closed"},
            "head": {
                **self.pr,
                "head": {**self.pr["head"], "sha": "9" * 40},
                "head_sha": "9" * 40,
            },
        }
        for field, changed in changes.items():
            with self.subTest(field=field):
                self.write_state()
                self.write_comments()
                with (
                    mock.patch.object(
                        MODULE,
                        "preflight",
                        return_value=(
                            self.pr,
                            "viewer",
                            self.anchors,
                            None,
                            None,
                            [],
                            [],
                            DIFF,
                        ),
                    ),
                    mock.patch.object(MODULE, "resolve_pr", return_value=changed),
                    mock.patch.object(MODULE, "claim_mutation") as claim,
                    mock.patch.object(MODULE, "gh_json") as gh_json,
                ):
                    with self.assertRaisesRegex(
                        MODULE.WorkflowError, "live pull request state changed"
                    ):
                        MODULE.command_post(self.args())

                claim.assert_not_called()
                gh_json.assert_not_called()

    def test_created_but_unverified_state_is_persisted(self):
        self.write_state()
        self.write_comments()
        review = {
            "id": 9,
            "html_url": f"{self.pr['pr_url']}#pullrequestreview-9",
        }
        with (
            mock.patch.object(
                MODULE,
                "preflight",
                return_value=(
                    self.pr,
                    "viewer",
                    self.anchors,
                    None,
                    None,
                    [],
                    [],
                    DIFF,
                ),
            ),
            mock.patch.object(MODULE, "ensure_snapshot_unchanged"),
            mock.patch.object(MODULE, "gh_json", return_value=review),
            mock.patch.object(
                MODULE,
                "verify_created_review",
                side_effect=MODULE.WorkflowError("verification failed"),
            ),
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "was created"):
                MODULE.command_post(self.args())

        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["mutation"]["status"], "created_unverified")

    def test_api_failure_leaves_the_one_mutation_guard_claimed(self):
        self.write_state()
        self.write_comments()
        with (
            mock.patch.object(
                MODULE,
                "preflight",
                return_value=(
                    self.pr,
                    "viewer",
                    self.anchors,
                    None,
                    None,
                    [],
                    [],
                    DIFF,
                ),
            ),
            mock.patch.object(MODULE, "ensure_snapshot_unchanged"),
            mock.patch.object(
                MODULE,
                "gh_json",
                side_effect=MODULE.WorkflowError("GitHub API failed"),
            ),
        ):
            with self.assertRaisesRegex(MODULE.WorkflowError, "GitHub API failed"):
                MODULE.command_post(self.args())

        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["mutation"]["status"], "attempted")
        self.assertTrue(
            self.state_path.with_name(
                f"{self.state_path.name}.mutation-guard"
            ).is_file()
        )


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
                    self.pr, "viewer", review["id"], self.comments, self.anchors
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
                    self.pr, "viewer", review["id"], self.comments, self.anchors
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
                    self.pr, "viewer", review["id"], self.comments, self.anchors
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
                self.pr, "viewer", review["id"], self.comments, self.anchors
            )

        self.assertEqual(result, review)
        resolve_pr.assert_called_once_with(self.pr)

    def test_review_verification_accepts_a_multi_line_pending_comment(self):
        review = {
            "id": 9,
            "commit_id": self.pr["head_sha"],
            "state": "PENDING",
            "html_url": f"{self.pr['pr_url']}#pullrequestreview-9",
            "user": {"login": "viewer"},
        }
        comment = {
            "path": "src/one.py",
            "start_line": 2,
            "start_side": "RIGHT",
            "line": 4,
            "side": "RIGHT",
            "body": "Use this.\n```suggestion\nreplacement\n```",
        }
        actual = [{**comment, "position": 5, "original_position": 5}]

        with (
            mock.patch.object(MODULE, "gh_json", return_value=review),
            mock.patch.object(MODULE, "gh_paginated", return_value=actual),
            mock.patch.object(MODULE, "ensure_head_unchanged"),
        ):
            result = MODULE.verify_created_review(
                self.pr, "viewer", review["id"], [comment], self.anchors
            )

        self.assertEqual(result, review)

    def test_enriches_a_legacy_comment_range_from_graphql(self):
        legacy = {
            "id": 17,
            "node_id": "PRRC_test",
            "path": "src/one.py",
            "position": 5,
            "body": "Use this.",
        }
        graphql = {
            "data": {
                "node": {
                    "databaseId": 17,
                    "line": 4,
                    "startLine": 2,
                    "originalLine": 4,
                    "originalStartLine": 2,
                }
            }
        }

        with mock.patch.object(
            MODULE, "gh_json", return_value=graphql
        ) as gh_json:
            result = MODULE.enrich_legacy_comment_location(legacy)

        self.assertEqual(result["line"], 4)
        self.assertEqual(result["start_line"], 2)
        arguments = gh_json.call_args.args[0]
        self.assertEqual(arguments[:2], ["api", "graphql"])
        self.assertIn("id=PRRC_test", arguments)

    def test_review_verification_recovers_a_legacy_multi_line_range(self):
        review = {
            "id": 9,
            "commit_id": self.pr["head_sha"],
            "state": "PENDING",
            "html_url": f"{self.pr['pr_url']}#pullrequestreview-9",
            "user": {"login": "viewer"},
        }
        expected = {
            "path": "src/one.py",
            "start_line": 2,
            "start_side": "RIGHT",
            "line": 4,
            "side": "RIGHT",
            "body": "Use this.\n```suggestion\nreplacement\n```",
        }
        legacy = {
            "id": 17,
            "node_id": "PRRC_test",
            "path": "src/one.py",
            "position": 5,
            "original_position": 5,
            "body": expected["body"],
        }
        graphql = {
            "data": {
                "node": {
                    "databaseId": 17,
                    "line": 4,
                    "startLine": 2,
                    "originalLine": 4,
                    "originalStartLine": 2,
                }
            }
        }

        with (
            mock.patch.object(
                MODULE, "gh_json", side_effect=[review, graphql]
            ),
            mock.patch.object(MODULE, "gh_paginated", return_value=[legacy]),
            mock.patch.object(MODULE, "ensure_head_unchanged"),
        ):
            result = MODULE.verify_created_review(
                self.pr, "viewer", review["id"], [expected], self.anchors
            )

        self.assertEqual(result, review)

    def test_review_verification_rejects_a_different_multi_line_start(self):
        review = {
            "id": 9,
            "commit_id": self.pr["head_sha"],
            "state": "PENDING",
            "html_url": f"{self.pr['pr_url']}#pullrequestreview-9",
            "user": {"login": "viewer"},
        }
        expected = {
            "path": "src/one.py",
            "start_line": 2,
            "start_side": "RIGHT",
            "line": 4,
            "side": "RIGHT",
            "body": "Use this.\n```suggestion\nreplacement\n```",
        }
        actual = [{**expected, "start_line": 3}]

        with (
            mock.patch.object(MODULE, "gh_json", return_value=review),
            mock.patch.object(MODULE, "gh_paginated", return_value=actual),
            mock.patch.object(MODULE, "ensure_head_unchanged"),
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "inline comments failed verification"
            ):
                MODULE.verify_created_review(
                    self.pr, "viewer", review["id"], [expected], self.anchors
                )

    def test_review_verification_accepts_position_only_pending_comments(self):
        # A review's own comments endpoint returns the legacy shape, which omits
        # line and side and locates each comment only by its diff position.
        review = {
            "id": 9,
            "commit_id": self.pr["head_sha"],
            "state": "PENDING",
            "html_url": f"{self.pr['pr_url']}#pullrequestreview-9",
            "user": {"login": "viewer"},
        }
        pending = [
            {
                "path": "src/one.py",
                "position": 3,
                "original_position": 3,
                "body": self.comments[0]["body"],
            }
        ]

        with (
            mock.patch.object(MODULE, "gh_json", return_value=review),
            mock.patch.object(MODULE, "gh_paginated", return_value=pending),
            mock.patch.object(MODULE, "ensure_head_unchanged"),
        ):
            result = MODULE.verify_created_review(
                self.pr, "viewer", review["id"], self.comments, self.anchors
            )

        self.assertEqual(result, review)

    def test_review_verification_rejects_position_pointing_elsewhere(self):
        review = {
            "id": 9,
            "commit_id": self.pr["head_sha"],
            "state": "PENDING",
            "html_url": f"{self.pr['pr_url']}#pullrequestreview-9",
            "user": {"login": "viewer"},
        }
        # Position 5 is RIGHT line 4, not the RIGHT line 2 that was posted.
        pending = [
            {
                "path": "src/one.py",
                "position": 5,
                "body": self.comments[0]["body"],
            }
        ]

        with (
            mock.patch.object(MODULE, "gh_json", return_value=review),
            mock.patch.object(MODULE, "gh_paginated", return_value=pending),
            mock.patch.object(MODULE, "ensure_head_unchanged"),
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "inline comments failed verification"
            ):
                MODULE.verify_created_review(
                    self.pr, "viewer", review["id"], self.comments, self.anchors
                )

    def test_review_verification_falls_back_to_original_position(self):
        review = {
            "id": 9,
            "commit_id": self.pr["head_sha"],
            "state": "PENDING",
            "html_url": f"{self.pr['pr_url']}#pullrequestreview-9",
            "user": {"login": "viewer"},
        }
        pending = [
            {
                "path": "src/one.py",
                "position": None,
                "original_position": 3,
                "body": self.comments[0]["body"],
            }
        ]

        with (
            mock.patch.object(MODULE, "gh_json", return_value=review),
            mock.patch.object(MODULE, "gh_paginated", return_value=pending),
            mock.patch.object(MODULE, "ensure_head_unchanged"),
        ):
            result = MODULE.verify_created_review(
                self.pr, "viewer", review["id"], self.comments, self.anchors
            )

        self.assertEqual(result, review)

    def test_review_verification_rejects_unlocatable_comment(self):
        review = {
            "id": 9,
            "commit_id": self.pr["head_sha"],
            "state": "PENDING",
            "html_url": f"{self.pr['pr_url']}#pullrequestreview-9",
            "user": {"login": "viewer"},
        }
        pending = [{"path": "src/one.py", "body": "x"}]

        with (
            mock.patch.object(MODULE, "gh_json", return_value=review),
            mock.patch.object(MODULE, "gh_paginated", return_value=pending),
            mock.patch.object(MODULE, "ensure_head_unchanged"),
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError,
                "reports neither a line and side nor a diff position",
            ):
                MODULE.verify_created_review(
                    self.pr, "viewer", review["id"], self.comments, self.anchors
                )

    def test_review_verification_rejects_position_off_a_changed_line(self):
        review = {
            "id": 9,
            "commit_id": self.pr["head_sha"],
            "state": "PENDING",
            "html_url": f"{self.pr['pr_url']}#pullrequestreview-9",
            "user": {"login": "viewer"},
        }
        # Position 1 in src/one.py is a context line, never a valid anchor.
        pending = [{"path": "src/one.py", "position": 1, "body": "x"}]

        with (
            mock.patch.object(MODULE, "gh_json", return_value=review),
            mock.patch.object(MODULE, "gh_paginated", return_value=pending),
            mock.patch.object(MODULE, "ensure_head_unchanged"),
        ):
            with self.assertRaisesRegex(
                MODULE.WorkflowError, "which is not a changed line"
            ):
                MODULE.verify_created_review(
                    self.pr, "viewer", review["id"], self.comments, self.anchors
                )

    def test_review_verification_ignores_body_line_ending_differences(self):
        review = {
            "id": 9,
            "commit_id": self.pr["head_sha"],
            "state": "PENDING",
            "html_url": f"{self.pr['pr_url']}#pullrequestreview-9",
            "user": {"login": "viewer"},
        }
        expected = [{**self.comments[0], "body": "First line.\nSecond line."}]
        pending = [
            {
                "path": "src/one.py",
                "position": 3,
                "body": "First line.\r\nSecond line.",
            }
        ]

        with (
            mock.patch.object(MODULE, "gh_json", return_value=review),
            mock.patch.object(MODULE, "gh_paginated", return_value=pending),
            mock.patch.object(MODULE, "ensure_head_unchanged"),
        ):
            result = MODULE.verify_created_review(
                self.pr, "viewer", review["id"], expected, self.anchors
            )

        self.assertEqual(result, review)

    def test_review_verification_still_rejects_meaningful_whitespace_changes(self):
        # Trailing spaces are Markdown hard breaks and leading indentation can
        # define a code block, so neither may be normalized away.
        review = {
            "id": 9,
            "commit_id": self.pr["head_sha"],
            "state": "PENDING",
            "html_url": f"{self.pr['pr_url']}#pullrequestreview-9",
            "user": {"login": "viewer"},
        }
        for posted, returned in (
            ("Hard break.  \nNext line.", "Hard break.\nNext line."),
            ("    indented code", "indented code"),
            ("Body.\n", "Body."),
        ):
            with self.subTest(posted=posted):
                expected = [{**self.comments[0], "body": posted}]
                pending = [
                    {"path": "src/one.py", "position": 3, "body": returned}
                ]
                with (
                    mock.patch.object(MODULE, "gh_json", return_value=review),
                    mock.patch.object(MODULE, "gh_paginated", return_value=pending),
                    mock.patch.object(MODULE, "ensure_head_unchanged"),
                ):
                    with self.assertRaisesRegex(
                        MODULE.WorkflowError, "inline comments failed verification"
                    ):
                        MODULE.verify_created_review(
                            self.pr, "viewer", review["id"], expected, self.anchors
                        )


if __name__ == "__main__":
    unittest.main()
